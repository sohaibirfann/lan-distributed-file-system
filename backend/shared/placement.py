from __future__ import annotations

import hashlib
import bisect
from dataclasses import dataclass
from enum import Enum

VIRTUAL_NODES_PER_GB = 10
MIN_VIRTUAL_NODES = 10
GB = 1024**3


class NodeState(Enum):
    UP = "up"
    SUSPECT = "suspect"
    DOWN = "down"
    DRAINING = "draining"


@dataclass(frozen=True)
class Node:
    node_id: str
    capacity_budget_bytes: int
    free_disk_bytes: int
    used_bytes: int
    state: NodeState

    @property
    def effective_capacity_bytes(self) -> int:
        # A total, for ring weighting — not how much space is left right now.
        return min(self.capacity_budget_bytes, self.free_disk_bytes)

    @property
    def remaining_bytes(self) -> int:
        budget_headroom = self.capacity_budget_bytes - self.used_bytes
        return max(0, min(budget_headroom, self.free_disk_bytes))

    @property
    def is_full(self) -> bool:
        return self.remaining_bytes == 0

    @property
    def is_eligible(self) -> bool:
        return self.state is NodeState.UP and not self.is_full


def ring_position(key: str) -> int:
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big")


def virtual_node_count(effective_capacity_bytes: int) -> int:
    capacity_gb = effective_capacity_bytes / GB
    return max(MIN_VIRTUAL_NODES, round(capacity_gb * VIRTUAL_NODES_PER_GB))


def build_ring(nodes: list[Node]) -> list[tuple[int, str]]:
    entries = []
    for node in nodes:
        for vnode_index in range(virtual_node_count(node.effective_capacity_bytes)):
            position = ring_position(f"{node.node_id}:{vnode_index}")
            entries.append((position, node.node_id))
    entries.sort(key=lambda entry: entry[0])
    return entries


def placement_candidates(
    ring: list[tuple[int, str]],
    nodes_by_id: dict[str, Node],
    chunk_id: str,
    replication_factor: int,
) -> list[str]:
    if not ring or replication_factor <= 0:
        return []

    start_position = ring_position(chunk_id)
    positions = [position for position, _ in ring]
    start_index = bisect.bisect_left(positions, start_position) % len(ring)

    chosen: list[str] = []
    seen: set[str] = set()
    for offset in range(len(ring)):
        _, node_id = ring[(start_index + offset) % len(ring)]
        if node_id in seen:
            continue
        seen.add(node_id)
        node = nodes_by_id.get(node_id)
        if node is None or not node.is_eligible:
            continue
        chosen.append(node_id)
        if len(chosen) == replication_factor:
            break
    return chosen
