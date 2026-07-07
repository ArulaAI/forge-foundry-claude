---
name: cloud-arch-review
description: >
  Run an AWS Well-Architected review across the six pillars for Infrastructure-as-Code
  (CloudFormation, SAM, CDK Python/TS, Terraform, Kubernetes manifests, docker-compose),
  grounding each finding in a WAF question ID, then optionally aggregating C10/C11
  AI-quality findings with dedup discipline. Detects the IaC format by content sniff and
  parses resources; for Kubernetes/compose targets also extracts workload resource attributes
  (limits, requests, securityContext, probes, NetworkPolicy) for ground-truth validation.
  Loads workload context from .foundry/cloud-arch.yaml (type, criticality, RTO/RPO, regions),
  reviews each pillar with severity + WAF question ID under four anti-hallucination guards,
  filters by confidence, annotates compliance/security cross-refs, and renders a Markdown,
  JSON, or SARIF report.
  Use when the user asks to "review cloud infrastructure", "run a Well-Architected review",
  "check IaC for best practices", "validate CloudFormation, Terraform, Kubernetes, or
  docker-compose", or when preparing infrastructure for production deployment.
---

# /cloud-arch-review

AWS Well-Architected review across the six pillars for any IaC format, directly in this
session: load workload context, emit per-pillar WAF judgment as compact JSON findings, then
invoke the bundled `cloud_arch_render.py` script for all deterministic work — IaC format
detection and resource parsing, confidence filter, C10/C11 dedup join, compliance/security ref
validation, and report rendering.

There is no installed foundry CLI — you invoke the script directly with `python3`.

## Token-offload model

The model is responsible for exactly **one** deterministic task: reading each resource's config
and judging whether it violates a WAF best practice (the per-pillar WAF judgment). Everything
else is offloaded to `scripts/cloud_arch_render.py` (stdlib-only, bare `python3`):

- IaC format detection (content-sniff heuristics — AWS formats via regex, Kubernetes/compose
  via `yaml.safe_load`/`yaml.safe_load_all`) and resource-list extraction.
- **Kubernetes/compose ground-truth attribute extraction** — per-workload booleans for
  `resources.limits`, `resources.requests`, `securityContext` (pod + container),
  `livenessProbe`, `readinessProbe`, and `NetworkPolicy` presence.
- **Anti-hallucination guard 4** — for k8s/compose targets: a finding claiming an attribute
  absent that the manifest shows present is marked `contradicted=True`, severity and
  confidence_level are downgraded to `low`, and the report annotates the finding with
  `⚠️ CONTRADICTED BY MANIFEST`.
- Confidence mapping (`> 0.7` high, `0.3-0.7` medium, `< 0.3` low) + threshold filter.
- C10/C11 dedup join (FM6/FM7/FM8 findings at matching `(file, line)` moved to
  `ai_quality.deduplicated_findings`).
- Compliance/security ref validation against allowlists.
- Markdown, JSON, and SARIF rendering (SARIF uses two runs: S20 + C10/C11).

## When to invoke

- Before architecture sign-off or a major infrastructure change — validate IaC against the WAF.
- When a workload is approaching production and needs WAF-aligned validation.
- When compliance evidence is required for reliability, security, or cost-optimization posture.
- When reviewing IaC for a multi-region or tier-1 criticality workload.
- When the user asks to "review cloud infrastructure", "run a Well-Architected review", or
  "validate CloudFormation or Terraform".

## When invoked

1. **Load workload context and resolve the IaC path.** Resolve the target IaC path the user
   gives (default: the current working directory; a missing path is a hard error). Load
   `.foundry/cloud-arch.yaml` (explicit `--arch-yaml` path, else auto-discovered at
   `<iac_path>/.foundry/cloud-arch.yaml`) into the workload context — **workload_type**
   (`web-app`/`api`/`batch`/`streaming`), **criticality** (`tier-1`/`tier-2`/`tier-3`),
   **rto_minutes**, **rpo_minutes**, and **regions** — falling back to defaults (`api`,
   `tier-2`, `rto=60`, `rpo=60`, `[us-east-1]`) when the file is absent and hard-erroring when
   it is malformed. Collect the list of IaC file paths to review (`iac_files`).

2. **Review each pillar and emit compact JSON findings.** Walk the six Well-Architected pillars
   — **operational_excellence** (OPS), **security** (SEC), **reliability** (REL),
   **performance_efficiency** (PERF), **cost_optimization** (COST), **sustainability** (SUS) —
   restricting to any pillars named via `--pillar`. For each file, judge each resource's config
   against WAF best practices calibrated by the workload context. Emit findings as a compact
   JSON payload. The top-level findings array MUST be named **`pillar_findings`** (a naive
   `"findings"` key is read as zero findings and produces an empty report). Each entry carries:
   `pillar`, `file`, `line`, `resource_id`, `severity` (`low`/`medium`/`high`/`critical`),
   `confidence` (0.0–1.0 float), `title`, `description`, `suggestion`, `waf_question` (e.g.
   `"SEC-8"`), `compliance_refs`, and `security_refs`. Optionally run C10 and C11 sub-calls
   (unless `--skip-ai-quality`) and include their raw findings as `c10_findings` and
   `c11_findings`. Include any allowlist files (`compliance_allowlist`, `security_allowlist`)
   when present. Exact top-level shape:
   ```json
   {
     "pillar_findings": [
       {
         "pillar": "reliability",
         "file": "k8s/web.yaml",
         "line": 12,
         "resource_id": "web",
         "severity": "high",
         "confidence": 0.9,
         "title": "Missing livenessProbe on the web Deployment",
         "description": "...",
         "suggestion": "...",
         "waf_question": "REL-5",
         "compliance_refs": [],
         "security_refs": []
       }
     ],
     "c10_findings": [],
     "c11_findings": []
   }
   ```

3. **Invoke the render script** (`<skill-dir>` is this skill's own deployed directory — the
   folder containing this SKILL.md; substitute its path, or `cd` there and drop the prefix).
   Write the JSON payload to a temp file and invoke:

   ```bash
   python3 <skill-dir>/scripts/cloud_arch_render.py \
       --findings-json /tmp/findings.json \
       --format markdown \
       --confidence-threshold medium \
       --output report.md
   ```

   The script handles: IaC format detection + resource parsing for anti-hallucination ground
   truth, confidence mapping + threshold filter, C10/C11 dedup (FM6/FM7/FM8 at matching
   `(file, line)` moved to `ai_quality.deduplicated_findings`), compliance/security ref
   validation against any provided allowlists, and final Markdown/JSON/SARIF rendering. The
   script exits non-zero on bad input (exit 2). Respect `--output` / `--dry-run` flags.

4. **Summarize and gate.** Present the rendered report to the user. Surface any finding at or
   above `--max-severity`. Stop the user on critical findings: exit 5 when sub-call critical
   findings remain after dedup, exit 4 when findings exceed the severity gate.

## How to invoke

Invoke as `/cloud-arch-review`. The user passes the IaC path (default: current working
directory); options refine the run: `--arch-yaml PATH` (workload context; auto-discovered from
`.foundry/cloud-arch.yaml`), `--compliance-yaml PATH` and `--security-yaml PATH` (cross-ref
sources), `--pillar NAME` (one of the six pillar names, repeatable; default all six),
`--iac-format cfn|sam|cdk_python|cdk_ts|terraform` (explicit format override),
`--format markdown|json|sarif` (default `markdown`), `--confidence-threshold low|medium|high`
(default `medium`), `--max-severity high|critical` (gate: exit 4 if exceeded),
`--skip-ai-quality` (skip the C10/C11 sub-calls), and `--output PATH` (write to a file; default
stdout). The render script (`scripts/cloud_arch_render.py`) does all deterministic
processing; you supply only the per-pillar WAF judgment as compact JSON findings.

## Output

A Well-Architected review report — Markdown, JSON, or SARIF — carrying the workload context
(type, criticality, RTO/RPO, regions), findings per pillar each with a severity and a WAF
question ID, compliance/security cross-reference annotations, and the aggregated C10/C11
AI-Quality findings with their deduplicated entries. The authoritative output contract (the six
pillars, the severity levels, the WAF-question-ID scheme, the dedup discipline, and the
confidence filter) lives in `## Invariants` below.

## Invariants

- Findings are produced across exactly these six Well-Architected pillars: **operational_excellence**
  (OPS), **security** (SEC), **reliability** (REL), **performance_efficiency** (PERF),
  **cost_optimization** (COST), **sustainability** (SUS).
- Every finding carries a severity of **low**, **medium**, **high**, or **critical**, and an
  optional WAF question ID in the canonical format `^(OPS|SEC|REL|PERF|COST|SUS)-\d+$`; a
  `waf_question` that fails this regex is cleared to None (the finding is retained), and findings
  are kept only if their `file` and `resource_id` ground-truth against the reviewed files and
  parsed resources.
- Confidence is mapped from the finding's score (`> 0.7` high, `0.3-0.7` medium, `< 0.3` low) and
  findings below the `--confidence-threshold` (default `medium`) are dropped.
- C10 (FM1-FM5) and C11 (FM6-FM10) AI-quality findings are produced unless `--skip-ai-quality`
  is set; under the dedup discipline a C11 FM6/FM7/FM8 finding at the same (file, line) as an
  S20 finding is MOVED to `ai_quality.deduplicated_findings` (not double-counted), leaving the
  S20 finding canonical.
- IaC format is detected by content sniff in priority order sam > cfn > cdk_python > cdk_ts >
  terraform > **kubernetes** > **compose**; an unresolvable format stops the run (use
  `--iac-format`).
- For **kubernetes** and **compose** targets, the render script applies anti-hallucination guard 4:
  if a finding **claims** a validated resource dimension is absent (an absence word bound to that
  dimension, e.g. "missing livenessProbe" — a bare mention does not count) but the parsed manifest
  shows it present, the finding is marked `contradicted=True`, severity and confidence_level are
  downgraded to `low`, and the report annotates it with `⚠️ CONTRADICTED BY MANIFEST`. So a valid
  "missing livenessProbe" finding whose description also mentions the present readinessProbe is
  **not** downgraded on the readinessProbe token (#1196). The validated dimensions are:
  `resources.limits`, `resources.requests`, `securityContext` (pod + container), `livenessProbe`,
  `readinessProbe` (k8s), and `deploy.resources.limits`/`reservations` (compose).
- `--max-severity` gates the run, and critical sub-call findings remaining after dedup stop the
  user.
