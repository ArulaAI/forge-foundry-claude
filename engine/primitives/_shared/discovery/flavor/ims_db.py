"""M5 Phase B flavor pack stub: IMS DB (M5-FLAVOR-2, #447).

This module reserves the public surface for the v1.1+ IMS-DB flavor
of the M5 ``--mainframe-flavor`` dispatcher. Per the design spec
(``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
§"Open question 2" + §"Non-goals" item 2), v1 ships *only* the
``cics_cobol`` flavor wired; ``jcl_batch`` and ``ims_db`` land here as
``NotImplementedError`` stubs so the public Python API + the
``FLAVORS`` registry shape are locked in before the v1.1
implementation arrives. v1.1 work — and the issue that lands the real
IMS DBD / PSB parser — is tracked under the parent EPIC **#439**
(``[M-EPIC-2] BankDemo playbook + synthetic-log primitives``);
sub-issues filed off that EPIC will replace this stub with the actual
parser pack.

## Deferred scope (what the v1.1 parser will cover)

The IMS DB flavor parses **IMS Database Description (DBD) and Program
Specification Block (PSB) source** — the hierarchical-database
companion to the v1 relational / VSAM access layer. The following
extractions are deferred from v1 to v1.1:

* **DBD inventory.** ``DBD NAME=DBNAME,ACCESS=HDAM`` headers produce
  ``IMSDatabaseRecord`` rows (one per DBD). The access method
  (``HDAM`` / ``HIDAM`` / ``HISAM`` / ``SHISAM`` / ``DEDB`` / ``MSDB``)
  feeds playbook §6 (Data Architecture) — the IMS-side analogue of
  v1's VSAM access matrix.
* **Segment hierarchy.** ``SEGM NAME=SEG,PARENT=PARENT,BYTES=N``
  statements build the hierarchical-tree structure each DBD defines.
  The parent/child edges power playbook §6's segment-tree Mermaid
  diagram (an IMS-only render not present in the v1 CICS pack).
* **Field definitions.** ``FIELD NAME=(FLD,SEQ,U),BYTES=N,START=N``
  inside each segment captures the field layout — particularly
  ``SEQ`` (sequence field / key) which determines how application
  programs traverse the hierarchy.
* **Logical relationships.** ``LCHILD NAME=(SEG,DBD),PAIR=SEG``
  declarations capture cross-database logical pointers — IMS's
  bidirectional relationship mechanism. These produce cross-DBD edges
  the playbook §6 renders alongside the per-DBD tree.
* **PSB / PCB analysis.** ``PCB TYPE=DB,DBDNAME=DBNAME,KEYLEN=N``
  inside ``PSB NAME=PROG`` declarations build the per-program database
  access view. ``PROCOPT=`` codes (``G`` get, ``R`` replace, ``I``
  insert, ``D`` delete, ``A`` all) feed the per-program / per-database
  access matrix in §6, plus the security surface in §10 (programs with
  ``A`` access are surfaced for review).
* **SENSEG / SENFLD.** Segment- and field-level sensitivity declarations
  inside PCBs determine which segments a program may access. These
  drive playbook §10 (Security Architecture) — IMS-level access
  control independent of CICS / RACF.
* **DL/I call extraction (in COBOL).** ``CALL 'CBLTDLI' USING …`` and
  ``CALL 'AIBTDLI' USING …`` from COBOL programs produce the
  application-level IMS access edges. (This crosses flavor boundaries
  — IMS DBD/PSB parsing here, but the call-site extraction lives in
  the CICS/COBOL pack and references PCB names from the PSB output.)

## Why a stub, not a thin implementation

IMS DBD / PSB grammar is positional and operand-rich; a partial parser
that handles ``SEGM`` but skips ``LCHILD`` would silently drop the
cross-DBD edges the playbook §6 render depends on — exactly the
hallucination pattern Phase D's self-audit is designed to catch. Per
the design spec §"Non-goals" item 2, IMS DB is explicitly out of
scope for v1; consumers reaching for this flavor before v1.1 ships
need a typed error pointing at the tracking issue, not a partial
extraction whose gaps surface only when their playbook §6 looks
wrong.

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


def parse_ims_db(path: Path, content: str) -> dict[str, list[object]]:
    """Parse one IMS DBD/PSB source file's content into category-keyed lists.

    **Stub — not yet implemented (M5-FLAVOR-2, v1 of M5).**

    Per the design spec
    (``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
    §"Open question 2" + §"Non-goals" item 2), the v1.1 implementation
    is deferred to a sub-issue of EPIC #439. Until then, callers
    selecting ``--mainframe-flavor ims-db`` get this typed, traceable
    error pointing at the tracking issue instead of a silent empty
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
        "M5 ims_db flavor not yet implemented — "
        "tracked under parent EPIC #439 for v1.1 (deferred scope: "
        "DBD inventory, segment hierarchy, field definitions, "
        "logical relationships, PSB/PCB analysis, SENSEG/SENFLD "
        "access control, DL/I call extraction). See "
        "primitives/_shared/discovery/flavor/ims_db.py "
        "module docstring for the full deferred-scope contract."
    )


__all__ = ["parse_ims_db"]
