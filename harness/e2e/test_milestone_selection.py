"""Tests for milestone_selection (count/percentage prefix, dependency-closed).

Run with pytest, or standalone:  python harness/e2e/test_milestone_selection.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from milestone_selection import (  # noqa: E402
    parse_milestone_spec, topological_order, load_graph, select_prefix, read_base_ids,
)


def test_parse_count_and_percent():
    assert parse_milestone_spec("10", 20) == 10
    assert parse_milestone_spec("50%", 20) == 10
    assert parse_milestone_spec("50%", 21) == 11      # ceil(10.5)
    assert parse_milestone_spec("100%", 7) == 7
    assert parse_milestone_spec("200%", 7) == 7        # capped at total
    assert parse_milestone_spec("999", 7) == 7         # capped at total
    assert parse_milestone_spec("0", 7) == 0


def test_topo_order_respects_backward_numbered_dependency():
    # M002 depends on M007.2 (edge M007.2 -> M002): a low number depending on a high one.
    nodes = {"M001", "M002", "M007.1", "M007.2", "M010"}
    edges = [("M001", "M002"), ("M007.1", "M007.2"), ("M007.2", "M002")]
    order = topological_order(nodes, edges)
    # prerequisites land before dependents; ascending-ID tiebreak otherwise
    assert order == ["M001", "M007.1", "M007.2", "M002", "M010"]


def test_prefix_is_dependency_closed_not_numeric():
    nodes = {"M001", "M002", "M007.1", "M007.2", "M010"}
    edges = [("M001", "M002"), ("M007.1", "M007.2"), ("M007.2", "M002")]
    order = topological_order(nodes, edges)
    first3 = order[:3]
    # NOT the naive numeric ["M001","M002","M007.1"] — that would orphan M002's prereq M007.2
    assert first3 == ["M001", "M007.1", "M007.2"]
    # and the chosen set is dependency-closed: every prereq of a chosen node is chosen
    chosen = set(first3)
    for s, t in edges:
        if t in chosen:
            assert s in chosen, f"{t} chosen but its prerequisite {s} is missing"


def test_cycle_raises():
    try:
        topological_order({"A", "B"}, [("A", "B"), ("B", "A")])
    except ValueError:
        return
    raise AssertionError("expected ValueError on cycle")


def test_select_prefix_end_to_end(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tmp_path or tempfile.mkdtemp())
    deps = d / "dependencies.csv"
    deps.write_text(
        "source_id,target_id,type,strength,rationale,confidence_score\n"
        "M001,M002,FUNC,Strong,x,0.9\n"
        "M007.1,M007.2,FUNC,Strong,x,0.9\n"
        "M007.2,M002,FUNC,Weak,x,0.9\n"      # weak dep still counts
        "M002,M010,FUNC,Strong,x,0.9\n",     # M010 depends on M002 (5 nodes total)
        encoding="utf-8",
    )
    # topo order = [M001, M007.1, M007.2, M002, M010]; 5 nodes -> 50% = ceil(2.5) = 3
    sel = select_prefix(deps, "50%")
    assert sel == ["M001", "M007.1", "M007.2"]
    assert select_prefix(deps, "2") == ["M001", "M007.1"]


def test_base_ids_scopes_the_universe(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tmp_path or tempfile.mkdtemp())
    deps = d / "dependencies.csv"
    deps.write_text(
        "source_id,target_id,type,strength,rationale,confidence_score\n"
        "M001,M002,FUNC,Strong,x,0.9\n"
        "M002,M003,FUNC,Strong,x,0.9\n"
        "M003,M004,FUNC,Strong,x,0.9\n",
        encoding="utf-8",
    )
    # base set excludes M004 -> universe is {M001,M002,M003}; 100% = all 3 in order
    sel = select_prefix(deps, "100%", base_ids={"M001", "M002", "M003"})
    assert sel == ["M001", "M002", "M003"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\nAll {len(fns)} tests passed.")
