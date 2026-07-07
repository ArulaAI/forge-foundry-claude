"""C8 /architecture-review — Stage C: coupling metrics.

Computes afferent (in-degree), efferent (out-degree), and instability for each module.

Instability = efferent / (afferent + efferent)
  - 0.0 means fully stable (nothing it imports depends on it)
  - 1.0 means fully unstable (everything depends on it; it depends on nothing)
  - 0.0 when both are 0 (isolated module)

A module is flagged as "fragile junction" when instability > 0.8 AND efferent > 5.
"""

from __future__ import annotations

from primitives.architecture_review.shared.models import CouplingMetric, DepGraph


def compute_coupling_metrics(dep_graph: DepGraph) -> dict[str, CouplingMetric]:
    """Compute coupling metrics for all modules in *dep_graph*.

    Args:
        dep_graph: The static dependency graph (Stage A output).

    Returns:
        Dict mapping module_name -> CouplingMetric.
    """
    # Initialize counters
    module_names = {m.name for m in dep_graph.modules}
    afferent: dict[str, int] = {name: 0 for name in module_names}
    efferent: dict[str, int] = {name: 0 for name in module_names}

    for from_mod, to_mod in dep_graph.edges:
        # Only count edges between known modules
        if from_mod in module_names:
            efferent[from_mod] = efferent.get(from_mod, 0) + 1
        if to_mod in module_names:
            afferent[to_mod] = afferent.get(to_mod, 0) + 1

    metrics: dict[str, CouplingMetric] = {}
    for name in module_names:
        a = afferent.get(name, 0)
        e = efferent.get(name, 0)
        total = a + e
        instability = e / total if total > 0 else 0.0
        metrics[name] = CouplingMetric(
            module=name,
            afferent=a,
            efferent=e,
            instability=instability,
        )

    return metrics


_FRAGILE_INSTABILITY_THRESHOLD = 0.8
_FRAGILE_EFFERENT_THRESHOLD = 5


def is_fragile_junction(metric: CouplingMetric) -> bool:
    """Return True if the module is a 'fragile junction' (instability > 0.8 AND efferent > 5)."""
    return (
        metric.instability > _FRAGILE_INSTABILITY_THRESHOLD
        and metric.efferent > _FRAGILE_EFFERENT_THRESHOLD
    )
