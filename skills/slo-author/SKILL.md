---
name: slo-author
description: >
  Draft SLOs/SLIs and error-budget policies for a service using the Google SRE Book approach.
  Parses the service spec, discovers SLIs across the 7 canonical kinds (availability,
  latency_p50/p95/p99, freshness, correctness, throughput), drafts target percentages and
  windows by criticality tier (tier-1 99.9-99.99%/30d, tier-2 99.5-99.9%/28d, tier-3
  99-99.5%/7d), computes error budgets in minutes plus multi-window multi-burn-rate alerts in
  pure Python, and exports to Markdown, Prometheus rules, Grafana JSON, or OpenSLO YAML. Use
  when the user asks to "define SLOs", "draft SLIs", "create error-budget policies", "export
  Prometheus alerting rules", "generate a Grafana SLO dashboard", or when establishing
  reliability targets for a new or existing service.
---

# /slo-author

Draft SLOs/SLIs and error-budget policies for a service, directly in this session: parse the
spec and load context, discover SLIs from the 7 canonical kinds, draft a target and window per
SLI by criticality tier, compute the error budget and burn-rate alerts in pure Python, then
export to the requested format(s).

## When to invoke

- When launching a new service — define SLOs before going to production.
- After a reliability incident — revise SLOs based on real failure data.
- During a Well-Architected review — document reliability targets and error budgets.
- When updating SLOs to match a spec change, or when error-budget policy needs refreshing.
- When the user asks to "define SLOs", "draft SLIs", "create error-budget policies",
  "export Prometheus rules", or "generate a Grafana SLO dashboard".

## When invoked

1. **Parse the spec and resolve context (Stage A).** Read the service spec at the path the user
   gives (a missing spec is a hard error → exit 2). Load `.foundry/slo-context.yaml` — explicit
   `--context` path, else auto-discovered from the spec's directory then the current working
   directory — carrying **service_name** (required), **criticality** (`tier-1`/`tier-2`/`tier-3`,
   optional), **dependencies**, **traffic_patterns**, and **user_facing** (default true); a file
   that exists but is malformed is a hard error. Derive the **service_name** from the context
   else the spec filename stem, and resolve **criticality** from the `--criticality` flag (wins),
   else the context yaml — if neither supplies it, stop and ask the user for one of tier-1,
   tier-2, tier-3 (exit 3).
2. **Discover SLIs from the 7 canonical kinds (Stage B, LLM).** Propose an ordered list of SLIs
   for the service, each drawn from exactly the 7 canonical kinds — **availability**,
   **latency_p50**, **latency_p95**, **latency_p99**, **freshness**, **correctness**,
   **throughput** — with a name, metric definition, and good/total event definitions. Apply the
   anti-hallucination guard: **drop any proposed SLI whose `kind` is not one of the 7** (warn on
   stderr), and drop any non-dict or malformed item.
3. **Draft an SLO target and window per SLI by criticality tier (Stage C, judgment only).**
   Propose a `target_percent` and `window_days` for each SLI, calibrated to the criticality tier
   — **tier-1** 99.9-99.99% over 30 days, **tier-2** 99.5-99.9% over 28 days, **tier-3** 99-99.5%
   over 7 days. You only supply the raw proposals; the renderer script deterministically clamps
   `target_percent` to 0.0-100.0, snaps `window_days` to {1, 7, 28, 30} (nearest), and fills any
   SLI you leave uncovered with the tier default (tier-1 99.9%/30d, tier-2 99.5%/28d, tier-3
   99.0%/7d) — do not do that arithmetic yourself.
4. **Hand off to the deterministic renderer (Stages C-snap, D, E — pure Python, zero model
   tokens).** Assemble your Stage A/B/C judgment into one compact JSON payload —
   `{"service_name": ..., "criticality": "tier-N", "slos": [{"name", "kind", "metric", "good",
   "total", "target_percent", "window_days"}, ...]}` — write it to a temp file, then run the
   bundled script:
   ```bash
   python3 <skill-dir>/scripts/slo_render.py --slos-json <payload.json> \
       --export markdown,prometheus --output-dir ./slo/
   ```
   The script does the deterministic remainder: it **drops** any SLI whose `kind` is not one of
   the 7 canonical kinds (stderr warning), clamps/snaps the targets+windows, computes
   `budget_minutes = (1 - target/100) × window_days × 24 × 60` (99.9%/30d = 43.2 min;
   99.99%/30d = 4.32 min) and the three Google SRE Workbook Ch.5 multi-window multi-burn-rate
   alerts — **high** (14.4×, 5% in 1h), **medium** (6×, 10% in 6h), **low** (1×, sustained) — and
   renders each `--export` format (**markdown** `.md`, **prometheus** recording+alerting rules
   `.yaml`, **grafana** dashboard `.json`, **openslo** v1alpha `.yaml`) to `--output-dir` (default
   `./slo/`, files `slo-<service>-<format>.<ext>`), or with `--dry-run` prints the primary format
   and writes nothing. It needs only `python3` — no installed foundry CLI, no PyYAML. Do **not**
   compute budgets or hand-render any export format yourself.
5. **Relay the summary.** Read the script's JSON summary (service, criticality, per-SLI budget
   minutes, written artifacts) and present it to the user; surface any SLI the script dropped
   (non-canonical-kind warning on stderr).

## How to invoke

Invoke as `/slo-author`. The user passes the spec path; options refine the run: `--context PATH`
(`.foundry/slo-context.yaml`, auto-detected), `--code-path PATH` (optional extra context),
`--criticality tier-1|tier-2|tier-3` (overrides the context yaml; required when the yaml omits
it), `--export markdown|prometheus|grafana|openslo` (repeatable; default markdown),
`--output-dir PATH` (default `./slo/`), and `--dry-run` (print the primary format, write
nothing). The model performs only the judgment + file-reading stages (spec parse, context load,
SLI discovery, target/window drafting); the deterministic remainder — clamp/snap, error-budget
math, burn-rate alerts, and the four export renderers — is offloaded to the bundled
`scripts/slo_render.py` (stdlib-only; runs under bare `python3`, no installed foundry CLI). The
script's flags mirror the options above (`--export`, `--output-dir`, `--dry-run`).

## Output

An SLO document plus the chosen export artifacts: a Markdown rationale doc and/or Prometheus
recording+alerting rules, a Grafana dashboard JSON, or an OpenSLO v1alpha YAML, each written to
`--output-dir` (or printed under `--dry-run`), carrying the per-SLI targets, windows, error
budgets in minutes, and the three burn-rate alerts. The authoritative output contract (the 7 SLI
kinds, the 3 criticality tiers with their target ranges and windows, the error-budget formula,
and the export formats) lives in `## Invariants` below.

## Invariants

- SLIs are drawn from exactly these 7 canonical kinds: **availability**, **latency_p50**,
  **latency_p95**, **latency_p99**, **freshness**, **correctness**, **throughput**. A proposed
  SLI whose `kind` is not one of the 7 is dropped (anti-hallucination, warn on stderr).
- Targets and windows are calibrated to 3 criticality tiers: **tier-1** 99.9-99.99% / 30d,
  **tier-2** 99.5-99.9% / 28d, **tier-3** 99-99.5% / 7d. `target_percent` is clamped to
  0.0-100.0 and `window_days` is snapped to the valid set {1, 7, 28, 30}; uncovered SLIs take the
  tier default (tier-1 99.9%/30d, tier-2 99.5%/28d, tier-3 99.0%/7d).
- The error budget is computed deterministically (pure Python, no LLM) as
  `budget_minutes = (1 - target/100) × window_days × 24 × 60`, with the three Google SRE Workbook
  Ch.5 multi-window multi-burn-rate alerts — high (14.4×, 5% in 1h), medium (6×, 10% in 6h), low
  (1×, sustained).
- Export formats are exactly **markdown** (`.md`), **prometheus** (recording+alerting rules
  `.yaml`), **grafana** (dashboard `.json`), and **openslo** (v1alpha `.yaml`); default markdown,
  repeatable. Artifacts land in `--output-dir` (default `./slo/`) unless `--dry-run`.
- A missing spec exits 2; an unresolvable criticality (no flag and none in the context yaml)
  exits 3.
