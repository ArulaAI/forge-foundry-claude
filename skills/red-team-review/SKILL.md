---
name: red-team-review
description: >
  Adversarial security review that composes S11 /security-review and S10 /threat-model
  into exploit chains, attack trees, abuse cases (per-role when a /functional-map is
  given), and a ranked remediation plan — then assembles a SARIF-ready report. Use when
  the user asks to "red-team the repo", "find exploit chains", "build attack trees", or
  "schedule an unattended security scan in CI".
---

# /red-team-review

The adversarial security review ORCHESTRATOR: composes S11 `/security-review` and
S10 `/threat-model` to surface viable exploit chains, Mermaid attack trees, per-role
abuse cases (enhanced by an optional S61 `/functional-map`), and a reachability ×
blast-radius ranked remediation plan. The in-session harness does the adversarial
synthesis and assembles the final report.

S12 exists so consumers do not have to learn the S11→S10→synthesis→assemble sequence,
hand-thread the findings between tools, or wire the SARIF upload themselves. S12 owns
the composition contract; S11 and S10 stay focused on their own concerns.

## When to invoke

- When the user wants a deep adversarial pass on a codebase — not just a static scan
  but a chain-of-exploitation analysis that reasons about what an attacker can do
  end-to-end.
- When the user asks to "red-team the repo", "find exploit chains", "build attack
  trees", "chain S11 and S10", or "run an unattended security scan in CI".
- When a `/functional-map` (S61) is available and per-role abuse cases are wanted.
- To produce SARIF for upload to the GitHub Security tab (`security-events: write`
  permission required).

## When invoked

1. **Produce `security.json` via S11 `/security-review`.** S11 is a Python-load-bearing
   primitive — invoke it with a shared `--state-dir` so the output persists automatically
   and the dir is preserved after the run (#1292). Dispatch a fresh subagent that runs
   `/security-review <code-path> --state-dir <state-dir>`, following the S11
   extract→judge→continue protocol (extract-only emits `round-1-prompts.json`; after
   authoring judgments, continue with `--judgments <path> --state-dir <state-dir>`). The
   `--continue` leg writes `<state-dir>/security.json` and prunes only the transient
   round files — the caller-supplied dir (and `security.json`) are preserved. The S11
   JSON carries a `"findings"` array of objects with `id`, `severity`, `title`, `file`,
   `line` keys. Persist the artifact path before proceeding.

2. **Produce `threat.json` via S10 `/threat-model` in-session.** S10 is a skill-first
   primitive (no Python CLI). Dispatch a fresh in-session subagent running
   `/threat-model <spec-or-code>`, writing its JSON threats report to
   `<state-dir>/threat.json`. The S10 JSON must carry a `"threats"` array with `id`,
   `severity`, `title`, `file`, `line` keys. Persist the artifact before proceeding.

3. **Adversarial synthesis in-session.** Read both sub-call artifacts plus any
   `/functional-map` JSON sidecar (`<state-dir>/functional_map.json`, when present).
   Reason adversarially — as a skilled attacker — to produce:
   - **Exploit chains**: sequences of S11/S10 findings (each step must cite a real
     `finding_id`, `file`, and `line` from the ingested reports) that combine into a
     viable end-to-end attack path. Assign each chain an `EC-NNN` id, an objective,
     and a closed `impact` value (`info-disclosure`, `privilege-escalation`,
     `account-takeover`, `data-exfiltration`, `denial-of-service`, `rce`).
   - **Attack trees**: Mermaid `graph TD` diagrams (one per exploit objective) showing
     the decision tree an attacker would walk.
   - **Abuse cases**: per-role scenarios (drawn from the functional-map roles when
     available, otherwise inferred from the code) with the finding ids each scenario
     exploits. Assign each abuse case an `AB-NNN` id.
   - **Ranked remediation**: items ordered by `rank` (1 = fix first), each with a
     `fix` description, the chain/finding ids it addresses, `reachability` severity,
     and `blast_radius` severity.
   Write the synthesis output to `<state-dir>/synthesis.json`. The assembly step
   validates it against **strict (`extra="forbid"`, `frozen`) models — any unknown or
   missing key is a hard error**. Use these EXACT key names (see
   [`references/schemas/synthesis.json`](references/schemas/synthesis.json) for the full
   JSON Schema):

   - **`chains[]`** (`ExploitChain`): `id` (`EC-NNN`), `objective` (**not** `title`; and
     there is **no** `severity` field), `impact` (closed enum above), `steps[]`.
     - each **step** (`ChainStep`): `order` (int), `finding_id`, `technique`, `file`,
       `line` (int), `effect` (**not** `action`).
   - **`trees[]`** (`AttackTree`): `objective`, `mermaid` (a **pre-rendered** `graph TD`
     string — the pipeline does not render it for you).
   - **`abuse_cases[]`** (`AbuseCase`): `id` (`AB-NNN`), `role`, `scenario`,
     `finding_ids` (**plural**, an array of ids).
   - **`remediation[]`** (`RemediationItem`): `rank` (int, **not** `priority`), `fix`,
     `addresses` (array of chain/finding ids), `reachability`, `blast_radius`
     (each a severity: `low`|`medium`|`high`|`critical`).

   Worked example:
   ```json
   {
     "chains": [
       {
         "id": "EC-001",
         "objective": "Take over an admin account",
         "impact": "account-takeover",
         "steps": [
           {"order": 1, "finding_id": "S11-3", "technique": "SQLi", "file": "api/auth.py", "line": 42, "effect": "dump the admin password hash"}
         ]
       }
     ],
     "trees": [
       {"objective": "Remote code execution", "mermaid": "graph TD\n  A[Unauth request] --> B[SSTI in template]\n  B --> C[RCE]"}
     ],
     "abuse_cases": [
       {"id": "AB-001", "role": "anonymous", "scenario": "Enumerate valid accounts via the login timing side-channel", "finding_ids": ["S11-3"]}
     ],
     "remediation": [
       {"rank": 1, "fix": "Parameterize the auth query", "addresses": ["EC-001"], "reachability": "high", "blast_radius": "critical"}
     ]
   }
   ```

   Anti-hallucination: every `finding_id` in a chain step MUST resolve to an ingested
   `ThreatNode` id — drop or warn on unresolvable citations.

4. **Assemble + gate.** Invoke `/red-team-review --state-dir <state-dir>` (the
   deterministic assembly step, which the CI emitter renders as a Python CLI call).
   Read the three on-disk artifacts (`security.json`, `threat.json`, `synthesis.json`)
   from `<state-dir>` and render the assembled report in the requested format (markdown
   default; SARIF for GitHub Security tab upload). Apply the severity gate: if any
   inherited finding is at or above `--max-severity`, surface a gate failure alongside
   the report. Surface the assembled report, the state-dir location, and the gate
   outcome to the user. In CI, the emitted workflow runs the deterministic assembly step
   against these same artifacts.

## How to invoke

Invoke as `/red-team-review <code-path>` with the path to the repository or source
directory to red-team. Optional flags:

- `--functional-map <path>` — path to an S61 `/functional-map` JSON sidecar for
  per-role abuse cases. If omitted, abuse cases are inferred from the code.
- `--state-dir <dir>` — where to persist sub-call artifacts and synthesis output
  (default `.foundry/red-team/<timestamp>/`). Pass an existing state-dir to resume
  after a partial run.
- `--format sarif|markdown|json` — output format for the assembled report
  (default `markdown`; use `sarif` for GitHub Security tab upload).
- `--max-severity none|high|critical` — severity gate for CI (exit 4 if any finding
  at or above this level; default `none` = no gate).

YOU (the harness LLM) drive the sub-calls via dispatched subagents and perform the
adversarial synthesis in-session — there is no Python control-plane for the synthesis
step.

## Output

A 6-section report (`Red-Team Review`) covering:

1. **Executive Summary** — chain count, inherited finding count, top impact.
2. **Exploit Chains** — each chain with ordered steps, technique, file:line, effect,
   and impact label.
3. **Attack Trees** — Mermaid `graph TD` diagrams, one per exploit objective.
4. **Abuse Cases** — per-role scenarios with linked finding ids.
5. **Prioritized Remediation** — ranked items with reachability × blast-radius
   context.
6. **Inherited Findings** — the deduplicated S11+S10 node list (sorted by severity
   descending, deduped by file:line).

In `--format sarif`, the report is a SARIF 2.1.0 document with `codeFlows` for each
exploit chain (one thread-flow per chain) suitable for upload to the GitHub Security
tab (`security-events: write` permission required).

Run state is persisted under `<state-dir>/` (`security.json`, `threat.json`,
`synthesis.json`); a partially-completed run can be resumed by passing the same
`--state-dir`.

## Invariants

- **§4.1 orchestrator carve-out applies.** S12 is a composition primitive. It declares
  no §3.4 sub-call rules of its own — it **inherits** the §3.4 passes that S11 and S10
  may run internally (S11 may invoke C10/C11; S10 may invoke C10). S12 never invokes
  C10 or C11 directly.
- Every exploit-chain step must cite a `finding_id` that resolves to an ingested
  `ThreatNode` (from `security.json` or `threat.json`). Unresolvable citations are
  dropped with a stderr warning — no hallucinated finding references.
- The deterministic assembly + gate step is Python-only — it never calls an LLM. The
  adversarial synthesis happens in-session (step 3 above).
- No payload execution: this is analysis-only. The review identifies attack paths; it
  does not run exploit code.
- The `--format sarif` output is a valid SARIF 2.1.0 document with `codeFlows` for
  each chain. The consumer uploads it with `github/codeql-action/upload-sarif`.
- Sub-call artifacts are persisted before synthesis begins. If a sub-call fails, halt
  and surface the failure rather than synthesizing over incomplete data.
