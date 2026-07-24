from collections import Counter

import pytest
from hypothesis import given, strategies as st

from shared.placement import (
    GB,
    Node,
    NodeState,
    build_ring,
    placement_candidates,
)


def make_node(node_id, capacity_gb=10, used_gb=0, state=NodeState.UP):
    capacity_bytes = capacity_gb * GB
    return Node(
        node_id=node_id,
        capacity_budget_bytes=capacity_bytes,
        free_disk_bytes=capacity_bytes,
        used_bytes=used_gb * GB,
        state=state,
    )


def test_placement_returns_rf_distinct_nodes():
    nodes = [make_node(f"node-{i}") for i in range(5)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, "chunk-abc123", replication_factor=3)

    assert len(chosen) == 3
    assert len(set(chosen)) == 3


@pytest.mark.parametrize("state", [NodeState.SUSPECT, NodeState.DOWN, NodeState.DRAINING])
def test_placement_skips_ineligible_states(state):
    nodes = [
        make_node("node-excluded", state=state),
        make_node("node-a"),
        make_node("node-b"),
    ]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, "chunk-xyz", replication_factor=2)

    assert "node-excluded" not in chosen
    assert set(chosen) == {"node-a", "node-b"}


def test_placement_skips_full_nodes():
    nodes = [
        make_node("node-full", capacity_gb=10, used_gb=10),
        make_node("node-a"),
        make_node("node-b"),
    ]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, "chunk-xyz", replication_factor=2)

    assert "node-full" not in chosen


def test_placement_degrades_when_fewer_eligible_nodes_than_rf():
    nodes = [make_node("node-a"), make_node("node-b")]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, "chunk-xyz", replication_factor=3)

    assert len(chosen) == 2


def test_placement_returns_empty_with_no_nodes():
    chosen = placement_candidates([], {}, "chunk-xyz", replication_factor=3)
    assert chosen == []


def test_virtual_node_weighting_is_capacity_proportional():
    small = make_node("small", capacity_gb=10)
    large = make_node("large", capacity_gb=100)
    nodes = [small, large]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    counts = Counter()
    for i in range(3000):
        chosen = placement_candidates(ring, nodes_by_id, f"chunk-{i}", replication_factor=1)
        counts[chosen[0]] += 1

    # Large node has 10x the capacity, so it should receive roughly 10x the
    # chunks. Assert direction and rough order of magnitude rather than an
    # exact ratio, since ring hashing is randomized by design.
    ratio = counts["large"] / counts["small"]
    assert 5 < ratio < 20


def test_placement_is_deterministic_for_same_ring_and_chunk():
    nodes = [make_node(f"node-{i}") for i in range(4)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    first = placement_candidates(ring, nodes_by_id, "chunk-stable", replication_factor=2)
    second = placement_candidates(ring, nodes_by_id, "chunk-stable", replication_factor=2)

    assert first == second


node_lists = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=500),  # capacity_gb
        st.sampled_from(list(NodeState)),
    ),
    min_size=0,
    max_size=8,
    unique_by=lambda pair: id(pair),
)


@given(
    node_specs=node_lists,
    chunk_id=st.text(min_size=1, max_size=40),
    replication_factor=st.integers(min_value=1, max_value=5),
)
def test_placement_invariants_hold_for_any_ring(node_specs, chunk_id, replication_factor):
    nodes = [
        make_node(f"node-{i}", capacity_gb=capacity_gb, state=state)
        for i, (capacity_gb, state) in enumerate(node_specs)
    ]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, chunk_id, replication_factor)

    # Never more than requested, never a repeated node, always an eligible node,
    # and re-running with the same inputs is a pure, deterministic operation.
    assert len(chosen) <= replication_factor
    assert len(chosen) == len(set(chosen))
    assert all(nodes_by_id[node_id].is_eligible for node_id in chosen)
    assert chosen == placement_candidates(ring, nodes_by_id, chunk_id, replication_factor)
