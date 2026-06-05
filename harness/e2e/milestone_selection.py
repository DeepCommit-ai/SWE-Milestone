"""Select a dependency-respecting prefix of milestones (by count or percentage).

Used by ``run_e2e.py --milestones <N|P%>`` to run only the first N (or P%) milestones
of a workspace's DAG instead of the whole itinerary.

The prefix is taken in **topological order** (ascending-ID tiebreak), so it is always
**dependency-closed**: if a milestone is selected, every milestone it (transitively)
depends on is selected too. This matters because milestone numbering is NOT a valid
execution order on its own — e.g. an edge ``M007.2 -> M002`` means M002 depends on
M007.2, so a naive "first N by number" would orphan a prerequisite. Weak dependencies
are included in the ordering (same edges DAGManager uses).

The chosen IDs are written to a NEW file (default ``<trial_root>/milestone_selection.txt``),
never modifying the dataset's ``selected_milestone_ids.txt``. The orchestrator reads this
new file (taking precedence over ``selected_milestone_ids.txt``) via DAGManager's
``selected_ids_file``.
"""
import csv
import math
from pathlib import Path
from typing import Optional


def parse_milestone_spec(spec: str, total: int) -> int:
    """Parse a ``--milestones`` value into an absolute count, capped at ``total``.

    ``"10"``  -> 10                         (a count)
    ``"50%"`` -> ceil(0.50 * total)         (a percentage; rounded UP)
    """
    spec = str(spec).strip()
    if spec.endswith("%"):
        pct = float(spec[:-1])
        count = math.ceil(pct / 100.0 * total)
    else:
        count = int(spec)
    return max(0, min(count, total))


def read_base_ids(selected_ids_file: Path) -> Optional[set]:
    """Read a selected_milestone_ids.txt-style file into a set, or None if absent/empty.

    Read-only; used to scope the prefix to a pre-curated base set when one exists.
    """
    p = Path(selected_ids_file)
    if not p.exists():
        return None
    ids = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line)
    return ids or None


def load_graph(dependencies_csv: Path, milestones_csv: Optional[Path] = None,
               base_ids: Optional[set] = None) -> tuple:
    """Return ``(nodes, edges)`` for the milestone DAG.

    nodes: full milestone set — from milestones.csv ``id`` column if given, unioned with
           every id appearing in dependencies.csv (so isolated, dependency-free milestones
           are still counted); intersected with ``base_ids`` when provided.
    edges: ``(prereq, dependent)`` pairs = (source_id, target_id) rows whose BOTH endpoints
           are in ``nodes``. All strengths are kept (weak dependencies count).
    """
    dependencies_csv = Path(dependencies_csv)
    nodes = set()
    if milestones_csv and Path(milestones_csv).exists():
        with open(milestones_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = (row.get("id") or "").strip()
                if mid:
                    nodes.add(mid)
    edges_all = []
    with open(dependencies_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            s = (row.get("source_id") or "").strip()
            t = (row.get("target_id") or "").strip()
            if s and t:
                edges_all.append((s, t))
                nodes.update((s, t))
    if base_ids is not None:
        nodes &= set(base_ids)
    edges = [(s, t) for (s, t) in edges_all if s in nodes and t in nodes]
    return nodes, edges


def topological_order(nodes: set, edges: list) -> list:
    """Kahn topological sort with ascending-ID tiebreak (deterministic).

    Edge ``(s, t)`` means s is a prerequisite of t (s comes before t). Raises ValueError
    if the graph has a cycle.
    """
    indeg = {n: 0 for n in nodes}
    succ = {n: [] for n in nodes}
    for s, t in edges:
        if s in indeg and t in indeg:
            succ[s].append(t)
            indeg[t] += 1
    ready = sorted(n for n in nodes if indeg[n] == 0)
    order = []
    while ready:
        n = ready.pop(0)  # smallest ID among currently-ready nodes
        order.append(n)
        newly = []
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                newly.append(m)
        if newly:
            ready = sorted(ready + newly)
    if len(order) != len(nodes):
        raise ValueError(
            f"Milestone DAG has a cycle: ordered {len(order)} of {len(nodes)} nodes"
        )
    return order


def select_prefix(dependencies_csv: Path, spec: str,
                  milestones_csv: Optional[Path] = None,
                  base_ids: Optional[set] = None) -> list:
    """Return the dependency-closed topological prefix of ``spec`` milestones."""
    nodes, edges = load_graph(dependencies_csv, milestones_csv, base_ids)
    order = topological_order(nodes, edges)
    count = parse_milestone_spec(spec, len(order))
    return order[:count]


def write_selection(ids: list, out_path: Path) -> Path:
    """Write selected milestone IDs (one per line, topo order) to a new file."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
    return out_path
