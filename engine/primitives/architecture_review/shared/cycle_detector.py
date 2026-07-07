"""C8 /architecture-review — Stage D: cyclic dependency detector.

Uses Tarjan's SCC algorithm to find all strongly-connected components of size >= 2.
Self-loops (A -> A) are size-1 SCCs and are NOT returned.
Bidirectional edges (A -> B and B -> A) form a size-2 SCC and ARE returned.
"""

from __future__ import annotations

from primitives.architecture_review.shared.models import CyclicDependency, DepGraph


def detect_cycles(dep_graph: DepGraph) -> list[CyclicDependency]:
    """Detect cyclic dependencies using Tarjan's SCC algorithm.

    Args:
        dep_graph: The static dependency graph.

    Returns:
        List of CyclicDependency objects (size >= 2). Empty if no cycles.
    """
    # Build adjacency list from edges (only between known modules)
    module_names = {m.name for m in dep_graph.modules}

    adjacency: dict[str, list[str]] = {name: [] for name in module_names}
    for from_mod, to_mod in dep_graph.edges:
        if from_mod in module_names and to_mod in module_names:
            adjacency[from_mod].append(to_mod)

    # Tarjan's SCC — recursive implementation. Note: very deep graphs (~1000 modules)
    # may need sys.setrecursionlimit. v0.12.0+ may switch to iterative work-stack.
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in adjacency.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        # If v is a root node, pop the SCC
        if lowlink[v] == index[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for node in module_names:
        if node not in index:
            strongconnect(node)

    # Return only SCCs of size >= 2 (self-loops are size 1 and excluded)
    _min_cycle_size = 2
    result: list[CyclicDependency] = []
    for scc in sccs:
        if len(scc) >= _min_cycle_size:
            result.append(CyclicDependency(modules=sorted(scc), size=len(scc)))

    return result
