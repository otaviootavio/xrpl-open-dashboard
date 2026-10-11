"""Readable views (E7-E10): layout, disparity filter, community names, bundles. Known answers on small graphs.

    uv run pytest graph/tests -q
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backbone  # noqa: E402
import layout  # noqa: E402
import names  # noqa: E402
import views  # noqa: E402
from load import MonthGraph  # noqa: E402


def circle(n):
    a = 2 * np.pi * np.arange(n) / n
    return np.column_stack([np.cos(a), np.sin(a)])


# ---------------------------------------------------------------- E7 layout

def test_layout_is_deterministic():
    rng = np.random.default_rng(7)
    src, dst, w = rng.integers(0, 80, 300), rng.integers(0, 80, 300), rng.random(300)
    a = layout.force_layout(80, src, dst, w, circle(80))
    b = layout.force_layout(80, src, dst, w, circle(80))
    assert np.array_equal(a, b)


def test_star_has_no_overlaps_after_removal():
    n = 300
    src, dst = np.zeros(n - 1, int), np.arange(1, n)
    sizes = layout.node_sizes(np.r_[1000.0, np.linspace(1, 50, n - 1)])
    assert sizes.min() >= 3 and sizes.max() <= 18
    pos, sz, left = layout.place(n, src, dst, np.ones(n - 1), sizes, circle(n))
    assert left == 0 and np.array_equal(sz, sizes)
    view = {"nodes": [{"key": str(i), "attributes": {"x": float(x), "y": float(y), "size": float(s)}}
                      for i, ((x, y), s) in enumerate(zip(pos, sz))]}
    assert layout.check_view(view)["n_overlaps"] == 0


def test_sizes_shrink_when_discs_need_more_room_than_the_stage():
    n = 200
    pos, sz, left = layout.remove_overlaps(circle(n) * 0.1, np.full(n, 30.0))
    assert left == 0 and sz.max() < 30.0


def test_label_check_counts_touching_labels():
    view = {"nodes": [
        {"key": "a", "attributes": {"x": 0.0, "y": 0.0, "size": 3, "label": "rAAAAAAA", "label_rank": 0}},
        {"key": "b", "attributes": {"x": 0.01, "y": 0.01, "size": 3, "label": "rBBBBBBB", "label_rank": 1}},
        {"key": "c", "attributes": {"x": 1.0, "y": 1.0, "size": 3, "label": "rCCCCCCC", "label_rank": 2}},
        {"key": "d", "attributes": {"x": -1.0, "y": -1.0, "size": 3, "label": "rDDDDDDD", "label_rank": 40}}]}
    assert layout.check_view(view)["n_label_collisions"] == 1


def test_label_ranks_largest_first_ties_by_key():
    assert layout.label_ranks(np.array([5.0, 9.0, 5.0]), ["b", "c", "a"]) == [2, 0, 1]


# ---------------------------------------------------------------- E8 disparity filter

def test_disparity_known_answer():
    # star 0-{1,2,3,4} with a heavy 0-1 edge, plus 1-2. Hand-computed alphas: 0-1 (3/13)^3 = 0.012 kept;
    # 0-3 and 0-4 kept because 3 and 4 have one edge; 0-2 (0.5) and 1-2 (0.91) fail, but 0-2 is node 2's
    # strongest edge (tie with 1-2, lower index) so it stays; 1-2 goes.
    src, dst = np.array([0, 0, 0, 0, 1]), np.array([1, 2, 3, 4, 2])
    w = np.array([10.0, 1, 1, 1, 1])
    a = backbone.significance(src, dst, w, 5)
    assert abs(a[0] - (3 / 13) ** 3) < 1e-12
    mask, thr = backbone.keep_mask(src, dst, w, cap=100)
    assert mask.tolist() == [True, True, True, True, False] and thr == 0.05


def test_no_node_left_without_an_edge():
    src, dst = np.zeros(50, int), np.arange(1, 51)
    mask, _ = backbone.keep_mask(src, dst, np.linspace(1, 2, 50), cap=1)
    kept = set(src[mask]) | set(dst[mask])
    assert kept == set(range(51))


def test_cap_is_respected_on_a_dense_graph():
    rng = np.random.default_rng(3)
    src, dst = rng.integers(0, 300, 5000), rng.integers(0, 300, 5000)
    keep = src != dst
    mask, thr = backbone.keep_mask(src[keep], dst[keep], rng.random(keep.sum()), cap=1000, n=300)
    assert mask.sum() <= 1000


# ---------------------------------------------------------------- E9 names

def test_names_follow_the_top_member_role_and_get_a_suffix_when_repeated():
    m = {
        0: [{"entity_id": "rLHzPsX6oXkzU2qL12kHCH8G8cnZv1rBJh", "role": "likely_custodial", "pagerank": 0.4},
            {"entity_id": "rAAAAAAAAAAAAAAAAAAAAAAAAA", "role": "issuer", "pagerank": 0.1}],
        1: [{"entity_id": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De", "role": "issuer", "pagerank": 0.2}],
        2: [{"entity_id": "rZZZZZZZZZZZZZZZZZZZZZZZZZ", "role": None, "pagerank": 0.3},
            {"entity_id": "rYYYYYYYYYYYYYYYYYYYYYYYYY", "role": "bot_pair", "pagerank": 0.3}],
        3: [{"entity_id": "rLHzPsX6zzzzzzzzzzzzzzzzzz", "role": "likely_custodial", "pagerank": 0.5}],
    }
    got = names.community_names(m)
    assert got[1] == "Issuer · rMxCKbED"
    assert got[2] == "Bot pair · rYYYYYYY"                       # PageRank tie: lower entity id wins
    assert got[0] == "Exchange hub · rLHzPsX6 (C0)" and got[3] == "Exchange hub · rLHzPsX6 (C3)"


def test_summary_line():
    s = names.community_summary(1204, 810, 1000.0, [{"to": 7, "n": 120}, {"to": 1, "n": 410}], {1: "Issuer · rMxCKbED"})
    assert s == "1,204 entities · 81% of their transfers stay inside · most outflow to Issuer · rMxCKbED (41%)"
    assert names.community_summary(25, 0, 0.0, [], {}) == "25 entities"


# ---------------------------------------------------------------- E10 bundles

def test_grow_stays_connected_and_follows_strong_ties():
    # 0-1 strong, 1-2 strong, 0-3 weak, 4-5 is a separate pair
    src, dst = np.array([0, 1, 0, 4]), np.array([1, 2, 3, 5])
    w = np.array([5.0, 4.0, 1.0, 9.0])
    assert views.grow(0, src, dst, w, 3) == [0, 1, 2]
    assert views.grow(0, src, dst, w, 10) == [0, 1, 2, 3]


def test_sub_view_is_a_valid_graph():
    edges = [(i, (i * 7 + 3) % 40, 1.0 + i % 5) for i in range(40)] + [(0, i, 2.0) for i in range(1, 40)]
    edges = [e for e in edges if e[0] != e[1]]
    mg = MonthGraph("2026-01", "XRP", np.array([f"r{i:03d}" for i in range(40)], object),
                    np.array([e[0] for e in edges], np.int32), np.array([e[1] for e in edges], np.int32),
                    np.full(len(edges), 3, np.int64), np.array([e[2] for e in edges]))
    nodes = {f"r{i:03d}": {"entity_id": f"r{i:03d}", "role": "unknown", "community": 0} for i in range(40)}
    sub = views.Sub(mg, mg.weight, range(40), 30, lambda ids: layout.node_sizes(np.ones(len(ids))),
                    lambda nm: views.start_positions(nm, {}))
    v = sub.view("2026-01", "XRP", "community", nodes)
    keys = {n["key"] for n in v["nodes"]}
    assert all({"x", "y", "size", "label_rank"} <= set(n["attributes"]) for n in v["nodes"])
    assert all(e["source"] in keys and e["target"] in keys for e in v["edges"])
    assert sum(e["attributes"]["bb"] for e in v["edges"]) == v["attributes"]["n_backbone_edges"] <= 39 + 30
    assert v["attributes"]["n_overlaps"] == 0


def test_filter_keeps_a_connected_graph_connected():
    # two dense triangles joined by one weak edge: the bridge is not significant but must stay
    src = np.array([0, 1, 2, 3, 4, 5, 2])
    dst = np.array([1, 2, 0, 4, 5, 3, 3])
    w = np.array([9.0, 9, 9, 9, 9, 9, 0.1])
    mask, _ = backbone.keep_mask(src, dst, w, cap=100)
    assert mask[6]
