# LAN Distributed File System

A self-hosted, LAN-only distributed file system for a small group. Files are chunked,
end-to-end encrypted on the client, and replicated across the group's own machines —
no cloud service ever holds the files or the encryption key.

## What it demonstrates

- **Consistent hashing** for chunk placement, with capacity-weighted virtual nodes.
- **Quorum-acknowledged writes** and replica failover on reads.
- **Failure detection and automatic re-replication** driven by heartbeats.
- **Client-side end-to-end encryption** (ChaCha20-Poly1305) with per-chunk AEAD and
  content-addressed integrity verification.

## Architecture

- **Coordinator** (`backend/coordinator`) — a FastAPI service that owns accounts, node
  registration, chunk placement, replication health, and repair/GC background loops. It
  also serves the built dashboard.
- **Storage nodes** (`backend/node`) — FastAPI processes that store opaque encrypted
  chunks on local disk, authenticate chunk access with a per-node bearer token, and send
  heartbeats to the coordinator.
- **Dashboard** (`frontend`) — a React/TypeScript SPA. All chunk encryption/decryption
  happens in the browser; the coordinator and nodes never see plaintext or the
  encryption key.

Nodes are discovered automatically via mDNS on the LAN, with a configurable coordinator
address as fallback.

## Quick start (local demo)

Requires Python 3.11+ and Node 18+.

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cd ../frontend
npm install
npm run build             # coordinator serves this build

cd ../backend
python scripts/demo.py    # starts one coordinator + three storage nodes
```

Open the printed dashboard URL and log in with the printed demo credentials. Kill a node
process to see failure detection and repair in the event log.

## Running it for real (multiple machines)

**Coordinator** (one machine, one process):

```bash
NAMESPACE_PASSPHRASE="a shared group passphrase" python -m coordinator
```

**Each storage node** (one process per machine contributing storage):

```bash
STORAGE_DIRECTORY=/path/to/storage \
CAPACITY_BUDGET_GB=50 \
OWNER_USERNAME=alice \
OWNER_PASSWORD=hunter2 \
python -m node
```

A node auto-discovers the coordinator on the LAN; set `COORDINATOR_ADDRESS` explicitly if
mDNS is unavailable. See `backend/coordinator/settings.py` and `backend/node/config.py`
for the full set of configuration options (replication factor, write quorum, max file
size, repair/GC intervals, etc.) and their defaults.

## Tests

```bash
# backend
cd backend && python -m pytest tests/ -q

# frontend
cd frontend && npm test && npx tsc -b --noEmit
```

## Tech stack

Python, FastAPI, SQLAlchemy, Argon2id, PyJWT, zeroconf (mDNS) on the backend; React,
TypeScript, Vite, libsodium-wrappers on the frontend.
