"""M5 Phase B flavor pack stub: JCL batch (M5-FLAVOR-2, #447).

This module reserves the public surface for the v1.1+ JCL-batch flavor
of the M5 ``--mainframe-flavor`` dispatcher. Per the design spec
(``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
§"Open question 2"), v1 ships *only* the ``cics_cobol`` flavor wired;
``jcl_batch`` and ``ims_db`` land here as ``NotImplementedError`` stubs
so the public Python API + the ``FLAVORS`` registry shape are locked
in before the v1.1 implementation arrives. v1.1 work — and the issue
that lands the real JCL parser — is tracked under the parent EPIC
**#439** (``[M-EPIC-2] BankDemo playbook + synthetic-log primitives``);
sub-issues filed off that EPIC will replace this stub with the actual
parser pack.

## Deferred scope (what the v1.1 parser will cover)

The JCL flavor parses **z/OS JCL job streams** — the batch-side
companion to the v1 CICS / COBOL online-transaction flavor. The
following extractions are deferred from v1 to v1.1:

* **Job inventory.** ``//JOBNAME JOB (acct), 'descr', CLASS=…`` headers
  produce ``JCLJobRecord`` rows (one per job card). The job name is the
  natural key consumers cite in playbook §9 (Batch).
* **Step-level program calls.** ``//STEPNAME EXEC PGM=PROG`` and
  ``//STEPNAME EXEC PROC=PROCNAME`` rows produce per-step program
  invocation records — the batch-side analogue of v1's CICS LINK /
  XCTL edges. Procedure references (``PROC=``) resolve against in-stream
  PROC libraries when the source tree includes them; otherwise they're
  flagged ``resolved=False`` for the Phase B aggregator to chase.
* **DD statement extraction.** ``//DDNAME DD DSN=DATASET,DISP=(…)``
  records map each step's dataset I/O. The mode (``OLD`` / ``SHR`` /
  ``MOD`` / ``NEW``) plus DSN feeds playbook §6 (Data Architecture)
  showing which jobs touch which VSAM / sequential / GDG datasets.
  Output disposition (``CATLG`` / ``KEEP`` / ``DELETE``) flows into
  §13 (Operational Patterns).
* **Conditional flow.** ``COND=`` and ``IF / THEN / ENDIF`` constructs
  produce step-dependency edges so playbook §9 can render the actual
  batch-execution DAG (not just the linear step order).
* **Symbolic-parameter substitution.** ``&PARM`` references resolve
  against ``//*SET`` directives and JCL symbolic library overrides;
  unresolved references are surfaced for §14 (Tech Debt).
* **In-stream SYSIN data.** ``//SYSIN DD *`` heredoc bodies are
  preserved verbatim (the batch programs often consume control cards
  whose payload defines job-specific behavior).

## Why a stub, not a thin implementation

The v1.1 surface needs careful design alongside its e2e fixture
(z/OS JCL with embedded PROCs and GDG references). Shipping a
partial implementation that handles 30% of the verbs is *worse* than
shipping a clean NotImplementedError, because consumers might write
playbook content against an extraction that silently drops critical
DD statements. The cics_cobol pack ships honest precision precisely
because Phase D's self-audit can flag missing recall — but recall
verification needs a real fixture corpus, and that corpus is the
v1.1 work the deferred-scope EPIC tracks.

The dispatch surface is locked by the :data:`FLAVORS` registry in
``primitives/_shared/discovery/flavor/__init__.py``; the
signature is ``(path: Path, content: str) -> dict[str, list[object]]``
exactly matching :func:`cics_cobol.parse_cobol_source`. The v1.1
implementation only swaps the body — no caller breakage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def parse_jcl_batch(path: Path, content: str) -> dict[str, list[object]]:
    """Parse one JCL source file's content into category-keyed lists.

    **Stub — not yet implemented (M5-FLAVOR-2, v1 of M5).**

    Per the design spec
    (``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
    §"Open question 2"), the v1.1 implementation is deferred to a
    sub-issue of EPIC #439. Until then, callers selecting
    ``--mainframe-flavor jcl-batch`` get this typed, traceable error
    pointing at the tracking issue instead of a silent empty
    extraction.

    Args:
        path: Source file path (placeholder for the v1.1 signature).
        content: Raw file content (placeholder for the v1.1 signature).

    Raises:
        NotImplementedError: Always, with a message pointing at the
            parent EPIC #439 for the v1.1 deferred-scope work.

    Returns:
        Never returns; the function is a stub. The annotated return
        type matches :func:`cics_cobol.parse_cobol_source` so the
        :data:`FLAVORS` registry's ``FlavorParser`` type is satisfied.
    """
    del path, content  # placeholder for v1.1 implementation
    raise NotImplementedError(
        "M5 jcl_batch flavor not yet implemented — "
        "tracked under parent EPIC #439 for v1.1 (deferred scope: "
        "JCL job inventory, step-level program calls, DD statement "
        "extraction, conditional-flow analysis). See "
        "primitives/_shared/discovery/flavor/jcl_batch.py "
        "module docstring for the full deferred-scope contract."
    )


__all__ = ["parse_jcl_batch"]
