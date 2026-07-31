# LAN Distributed File System

A self-hosted, LAN-only distributed file system for a small group. Files are chunked,
end-to-end encrypted in the browser, and replicated across the group's own machines —
no cloud service, and no server, ever holds a file's plaintext or the encryption key.

## Contents

- [How it works](#how-it-works)
- [Setup: quick demo (one machine)](#setup-quick-demo-one-machine)
- [Setup: real deployment (multiple machines)](#setup-real-deployment-multiple-machines)
- [Using the dashboard](#using-the-dashboard)
- [Configuration reference](#configuration-reference)
- [Running the tests](#running-the-tests)
- [Tech stack](#tech-stack)
- [Future improvements](#future-improvements)

## How it works

**Roles**

- **Coordinator** — one process, run by one member. It's the control plane: accounts,
  node registry, chunk-placement metadata, replication health, and the repair/GC
  background loops. It serves the dashboard too. It never holds file bytes or the
  encryption key.
- **Storage nodes** — one process per member who opts in to contribute disk space. A
  node stores encrypted chunks under a capacity budget it declares at registration, and
  sends periodic heartbeats so the coordinator knows it's alive.
- **Dashboard** — the React app served by the coordinator and rendered in each member's
  browser. Every account can see and manage every file in the namespace (there's no
  per-file access control — it's one shared trust boundary, not a permissions system).

**Accounts vs. the namespace passphrase**

These are two different secrets with two different jobs:

- An **account** (username + password) is how you log in and is checked against the
  coordinator's database — ordinary authentication, unrelated to encryption.
- The **namespace passphrase** is shared by the whole group. It does two things: it
  gates new account registration (so a stranger on the LAN can't just sign up), and it
  deterministically derives the AEAD encryption key in the browser via Argon2id. The
  coordinator can verify a namespace passphrase without ever storing the passphrase
  itself or the key it derives.

**Upload path**

1. The browser splits the file into fixed-size chunks and encrypts each one
   independently with ChaCha20-Poly1305, using a key derived from the namespace
   passphrase. The chunk's on-the-wire identifier is the hash of its *ciphertext*
   (content-addressed), so any tampering or corruption is detectable without decrypting.
2. The coordinator picks which nodes should hold each chunk using **consistent hashing**
   with capacity-weighted virtual nodes — a node with more declared capacity gets
   proportionally more of the ring, and adding or removing a node only reshuffles the
   chunks that actually need to move.
3. The browser uploads each chunk directly to the chosen nodes (not through the
   coordinator) and waits for acknowledgement from at least **W** (write quorum) of the
   **RF** (replication factor) targets before considering the write durable.

**Download path**

The browser fetches the coordinator's placement metadata (which nodes hold which
chunks), downloads chunks directly from nodes with automatic failover to another
replica on a miss, verifies each chunk's ciphertext hash, decrypts it locally, and
streams the result straight to disk (no full-file buffering in memory).

**Failure handling**

Nodes heartbeat to the coordinator on an interval. A node that misses heartbeats is
marked **suspect**, then **down** after a grace period (long enough that a closed
laptop or a brief Wi-Fi drop doesn't trigger a repair storm). A background repair loop
periodically finds chunks below their replication factor and re-replicates them from a
surviving copy onto a spare node. A separate GC sweep reclaims chunks a node holds that
nothing references anymore. All of this — state transitions, repairs, GC — is written
to an event log visible in the dashboard.

## Setup: quick demo (one machine)

Requires Python 3.11+ and Node 18+.

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build              # the coordinator serves this build

cd ../backend
python scripts/demo.py     # starts one coordinator + three storage nodes locally
```

The script prints a dashboard URL and a demo login. Open it, log in, upload a file,
then kill one of the printed node processes to watch the dashboard mark it down and
repair the affected chunks onto the remaining nodes.

## Setup: real deployment (multiple machines)

All machines must be on the same LAN. Do this once:

**1. Build the frontend** (on any machine, or just once and copy `frontend/dist`
alongside the backend on the coordinator's machine):

```bash
cd frontend
npm install
npm run build
```

**2. Start the coordinator** (one machine, one process). Pick a namespace passphrase for
the group — this is required only the first time the coordinator ever starts:

```bash
cd backend
python -m venv .venv && pip install -r requirements.txt   # first time only
NAMESPACE_PASSPHRASE="a shared group passphrase" python -m coordinator
```

By default it listens on `0.0.0.0:8000`, reachable at `http://<coordinator-ip>:8000`
from any machine on the LAN.

**3. Create the first account.** Open `http://<coordinator-ip>:8000` in a browser,
choose "Create an account", and enter the same namespace passphrase from step 2 plus a
username/password for yourself. Every teammate registers the same way, with the same
namespace passphrase.

**4. Unlock the namespace passphrase in your browser.** After logging in, go to
Settings and enter the namespace passphrase again — this is what derives your local
encryption key. It's stored only in that browser (IndexedDB), never sent anywhere,
and needs to be re-entered on any new browser/device.

**5. Start a storage node on each contributing machine.** Each node needs its own
storage directory and its own address reachable by other machines on the LAN — the
coordinator hands this address to browsers, which then talk to the node directly:

```bash
cd backend
python -m venv .venv && pip install -r requirements.txt   # first time only
STORAGE_DIRECTORY=/path/to/storage \
CAPACITY_BUDGET_GB=50 \
NODE_ADDRESS=<this-machine-ip>:9000 \
OWNER_USERNAME=alice \
OWNER_PASSWORD=hunter2 \
python -m node
```

`OWNER_USERNAME`/`OWNER_PASSWORD` must be a real account created in step 3 — a node
authenticates to the coordinator as its owner. Run multiple nodes on one machine by
giving each a distinct `STORAGE_DIRECTORY`, `NODE_ADDRESS` port, and `NODE_PORT`.

A node finds the coordinator automatically via mDNS
(`_dfs-coordinator._tcp.local`), which works as long as all machines share the same LAN
segment (mDNS typically doesn't cross routers/VLANs). If discovery isn't reliable on
your network, set it explicitly instead:

```bash
COORDINATOR_ADDRESS=http://<coordinator-ip>:8000
```

**6. Firewall/ports.** Open TCP 8000 on the coordinator's machine, and whatever
`NODE_PORT` each node uses (default 9000) on each node's machine.

That's the whole deployment — no shared filesystem, database, or other shared
infrastructure required beyond the coordinator being reachable.

## Using the dashboard

- **Overview** — node health, at-risk chunk counts, and the event log (state
  transitions, repairs, GC).
- **Files** — upload, download, rename, delete. Uploading/downloading requires the
  namespace passphrase to be unlocked (Settings).
- **Settings** — account info, log out, and the namespace-passphrase unlock. The node
  owner can also drain their own node from the Overview page, which migrates its chunks
  off before it's safe to stop the process.

## Configuration reference

All variables are environment variables read at process startup; defaults apply if
unset.

**Coordinator**

| Variable | Default | Notes |
|---|---|---|
| `NAMESPACE_PASSPHRASE` | *(required on first run)* | Only needed once, to seed the namespace. Ignored on later restarts. |
| `COORDINATOR_HOST` | `0.0.0.0` | |
| `COORDINATOR_PORT` | `8000` | |
| `COORDINATOR_DB_PATH` | `coordinator.db` | SQLite file path. |
| `REPLICATION_FACTOR` | `3` | Re-read on every restart. |
| `WRITE_QUORUM` | `2` | Must be `<= REPLICATION_FACTOR`. |
| `MAX_FILE_SIZE_BYTES` | `10737418240` (10 GiB) | |
| `REPAIR_INTERVAL_SECONDS` | `60` | Background repair-cycle interval. |
| `REPAIR_CONCURRENCY` | `3` | Concurrent chunk repairs per cycle. |
| `GC_SWEEP_INTERVAL_SECONDS` | `300` | Background orphan-chunk sweep interval. |
| `GC_ORPHAN_GRACE_SECONDS` | `600` | How long an unreferenced chunk survives before GC reclaims it. |
| `SESSION_LIFETIME_DAYS` | `7` | Login session cookie lifetime. |
| `MDNS_ADVERTISE` | `true` | Set `false` to disable the coordinator's own mDNS broadcast. |
| `DASHBOARD_DIST_DIR` | `frontend/dist` | Where the built dashboard is served from. |

**Storage node**

| Variable | Default | Notes |
|---|---|---|
| `STORAGE_DIRECTORY` | *(required)* | Created if missing. |
| `CAPACITY_BUDGET_GB` | *(required)* | Whole GB, must be positive. |
| `NODE_ADDRESS` | *(required)* | The `host:port` other machines use to reach this node. |
| `OWNER_USERNAME` / `OWNER_PASSWORD` | *(required)* | An existing coordinator account. |
| `COORDINATOR_ADDRESS` | *(auto-discovered via mDNS)* | Set explicitly if mDNS is unavailable. |
| `NODE_HOST` | `0.0.0.0` | |
| `NODE_PORT` | `9000` | |
| `HEARTBEAT_INTERVAL_SECONDS` | `10` | Must be less than the coordinator's suspect threshold. |
| `CHUNK_TOKEN` | *(random per process)* | Overrides the auto-generated bearer token used to authenticate chunk requests — mainly useful for tests. |

## Running the tests

```bash
# backend
cd backend && python -m pytest tests/ -q

# frontend
cd frontend && npm test && npx tsc -b --noEmit
```

## Tech stack

Python, FastAPI, SQLAlchemy, Argon2id, PyJWT, zeroconf (mDNS) on the backend; React,
TypeScript, Vite, libsodium-wrappers on the frontend.

## Future improvements

This was scoped deliberately tight for v1, so plenty of good ideas got left on the table.
Roughly in order of how much they'd actually matter:

The coordinator is a single point of failure today — not just for the dashboard, but for
every upload and download, since both need a live round trip to it for placement/metadata.
Making that redundant (Raft-replicated metadata, or similar) would be the biggest
structural improvement, and the one I'd tackle first given more time.

A few reliability gaps follow from that same "trust the happy path" starting point:
uploads can't resume after a dropped connection, nothing periodically re-checks that a
node still actually has the bytes it said it stored, and nothing scrubs chunks at rest
to catch quiet disk corruption before someone tries to read a broken file. None of these
break the demo — they're the kind of thing that bites you months in, on real hardware.

Security-wise, the obvious next step is TLS between the browser and the coordinator —
right now it's plain HTTP, fine for a trusted LAN, not fine beyond that. Passphrase
rotation and per-file keys (for actually sharing a subset of files instead of everyone
seeing everything) would be the next layer after that.

The rest is more about flexibility than robustness: per-account storage quotas instead
of one global file-size cap, erasure coding as a cheaper alternative to full replication,
real conflict resolution instead of last-write-wins, and letting one coordinator host
more than one namespace instead of one shared trust boundary per deployment.
