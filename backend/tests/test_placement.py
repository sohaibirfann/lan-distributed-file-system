from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import example, given, strategies as st

from shared.placement import (
    DOWN_GRACE_PERIOD,
    GB,
    SUSPECT_THRESHOLD,
    Node,
    NodeState,
    build_ring,
    placement_candidates,
    state_from_heartbeat,
)


def make_node(node_id, capacity_gb=10, used_gb=0, state=NodeState.UP, free_disk_gb=None):
    if free_disk_gb is None:
        free_disk_gb = capacity_gb
    return Node(
        node_id=node_id,
        capacity_budget_bytes=capacity_gb * GB,
        free_disk_bytes=free_disk_gb * GB,
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


def test_placement_excludes_given_node_ids():
    nodes = [make_node(f"node-{i}") for i in range(5)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    first = placement_candidates(ring, nodes_by_id, "chunk-abc123", replication_factor=3)
    retry = placement_candidates(
        ring, nodes_by_id, "chunk-abc123", replication_factor=3, exclude=frozenset(first)
    )

    assert len(retry) == 2  # only 2 nodes left once the first 3 are excluded
    assert set(retry).isdisjoint(first)


def test_placement_returns_empty_for_non_positive_replication_factor():
    nodes = [make_node("node-a"), make_node("node-b")]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    assert placement_candidates(ring, nodes_by_id, "chunk-xyz", replication_factor=0) == []


def test_effective_capacity_is_capped_by_free_disk():
    node = make_node("node-a", capacity_gb=100, free_disk_gb=10)
    assert node.effective_capacity_bytes == 10 * GB


def test_effective_capacity_is_capped_by_budget():
    node = make_node("node-a", capacity_gb=10, free_disk_gb=100)
    assert node.effective_capacity_bytes == 10 * GB


def test_partly_used_node_with_free_disk_is_not_full():
    node = make_node("node-a", capacity_gb=10, used_gb=6, free_disk_gb=3)

    assert node.remaining_bytes == 3 * GB
    assert not node.is_full


def test_node_is_full_when_budget_is_exhausted():
    node = make_node("node-a", capacity_gb=10, used_gb=10, free_disk_gb=50)

    assert node.remaining_bytes == 0
    assert node.is_full


def test_node_is_full_when_disk_is_out_of_space():
    node = make_node("node-a", capacity_gb=100, used_gb=5, free_disk_gb=0)

    assert node.remaining_bytes == 0
    assert node.is_full


def test_node_over_its_budget_is_full_not_negative():
    node = make_node("node-a", capacity_gb=10, used_gb=12, free_disk_gb=50)

    assert node.remaining_bytes == 0
    assert node.is_full


def test_placement_uses_a_partly_used_node_that_still_has_room():
    nodes = [make_node("node-a", capacity_gb=10, used_gb=6, free_disk_gb=3)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    assert placement_candidates(ring, nodes_by_id, "chunk-xyz", 1) == ["node-a"]


@pytest.mark.parametrize(
    "age, expected",
    [
        (timedelta(0), NodeState.UP),
        (SUSPECT_THRESHOLD, NodeState.UP),
        (SUSPECT_THRESHOLD + timedelta(seconds=1), NodeState.SUSPECT),
        (SUSPECT_THRESHOLD + DOWN_GRACE_PERIOD, NodeState.SUSPECT),
        (SUSPECT_THRESHOLD + DOWN_GRACE_PERIOD + timedelta(seconds=1), NodeState.DOWN),
    ],
)
def test_state_from_heartbeat_boundaries(age, expected):
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    assert state_from_heartbeat(now - age, now) is expected


def test_draining_ignores_heartbeat_age():
    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    assert state_from_heartbeat(now, now, draining=True) is NodeState.DRAINING
    assert (
        state_from_heartbeat(now - timedelta(days=1), now, draining=True) is NodeState.DRAINING
    )


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

    # Order of magnitude, not exact ratio — ring hashing is randomized by design.
    ratio = counts["large"] / counts["small"]
    assert 5 < ratio < 20


def test_placement_is_deterministic_for_same_ring_and_chunk():
    nodes = [make_node(f"node-{i}") for i in range(4)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    first = placement_candidates(ring, nodes_by_id, "chunk-stable", replication_factor=2)
    second = placement_candidates(ring, nodes_by_id, "chunk-stable", replication_factor=2)

    assert first == second


node_specs = st.lists(
    st.fixed_dictionaries(
        {
            "capacity_gb": st.integers(min_value=1, max_value=500),
            "free_disk_gb": st.integers(min_value=0, max_value=500),
            "used_gb": st.integers(min_value=0, max_value=500),
            "state": st.sampled_from(list(NodeState)),
        }
    ),
    min_size=0,
    max_size=8,
)


@given(
    specs=node_specs,
    chunk_id=st.text(min_size=1, max_size=40),
    replication_factor=st.integers(min_value=0, max_value=5),
)
def test_placement_invariants_hold_for_any_ring(specs, chunk_id, replication_factor):
    nodes = [make_node(f"node-{i}", **spec) for i, spec in enumerate(specs)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}

    chosen = placement_candidates(ring, nodes_by_id, chunk_id, replication_factor)

    eligible = [n.node_id for n in nodes if n.is_eligible]
    assert len(chosen) == min(replication_factor, len(eligible))
    assert len(chosen) == len(set(chosen))
    assert set(chosen) <= set(eligible)
    assert chosen == placement_candidates(ring, nodes_by_id, chunk_id, replication_factor)


@example(
    specs=[
        {"capacity_gb": 10, "free_disk_gb": 10, "used_gb": 0, "state": NodeState.UP}
        for _ in range(4)
    ],
    chunk_id="chunk-with-several-eligible-nodes",
    replication_factor=3,
    exclude_every_other=True,
)
@given(
    specs=node_specs,
    chunk_id=st.text(min_size=1, max_size=40),
    replication_factor=st.integers(min_value=0, max_value=5),
    exclude_every_other=st.booleans(),
)
def test_placement_exclude_invariants_hold_for_any_ring(
    specs, chunk_id, replication_factor, exclude_every_other
):
    nodes = [make_node(f"node-{i}", **spec) for i, spec in enumerate(specs)]
    ring = build_ring(nodes)
    nodes_by_id = {n.node_id: n for n in nodes}
    eligible = [n.node_id for n in nodes if n.is_eligible]
    excluded = frozenset(eligible[::2]) if exclude_every_other else frozenset()

    chosen = placement_candidates(
        ring, nodes_by_id, chunk_id, replication_factor, exclude=excluded
    )

    still_eligible = [n for n in eligible if n not in excluded]
    assert len(chosen) == min(replication_factor, len(still_eligible))
    assert set(chosen).isdisjoint(excluded)
    assert set(chosen) <= set(still_eligible)
