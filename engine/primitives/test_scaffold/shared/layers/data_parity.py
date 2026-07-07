"""Data-parity layer renderer — three-level checksums for legacy↔modernized parity.

Scaffolds a real, working checksum harness (``tests/container/parity_checksums.py``)
that compares a source (legacy) and target (modernized) data run at three
granularities (DT-1 doc §8, #1141):

1. **Row checksum** — a stable per-row hash over the row's *canonicalized* column
   values (sorted keys, normalized number scale / timestamp tz / string padding /
   NULL sentinel), so two rows are equal iff their checksums match.
2. **Data-table checksum** — an order-independent, duplicate-sensitive aggregate
   over a table's row checksums (sorted row hashes + row count), so a whole
   table's equivalence is one compare insensitive to engine row-ordering.
3. **Dataset checksum** — a roll-up over all table checksums (sorted by table
   name), giving one top-line "the whole migration matches" signal.

The checksum core is **real, runnable code** (stdlib only). Only the DB extraction
(pulling rows from the legacy + modernized engines) is left as a ``# Replace with
...`` skeleton. Dialect-aware canonicalization handles the type/encoding/collation
differences between engines so equivalent values hash equal across them.

DT-6a (#1173) emits the harness + its property self-test. DT-6b (#1174) extends
with the three-level compare report, tolerance allowlist, sampled deep-compare,
and java/node skeletons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._sdet.stacks import data_backend_meta
from primitives.test_scaffold.shared.layers._data_select import resolve_backends
from primitives.test_scaffold.shared.layers._emphasis import with_emphasis
from primitives.test_scaffold.shared.registry import (
    ScaffoldFile,
    ScaffoldResult,
    register_scaffold,
)

if TYPE_CHECKING:
    from primitives.test_scaffold.shared.registry import ScaffoldContext


# The checksum harness is backend-agnostic (it operates on already-extracted row
# dicts), so it is emitted once per scaffold, not per backend. ``HARNESS`` is a
# complete, runnable module; only the row-extraction is a consumer ``# Replace``.
_HARNESS = '''"""Three-level checksum harness for legacy↔modernized data parity.

Scaffolded by /test-scaffold (data-parity layer). Compares two data runs at row,
data-table, and dataset granularity. The checksum core is complete + deterministic;
plug your legacy/modernized row extraction into the ``# Replace with ...`` seams.

- ``row_checksum(row)`` — stable hash of one row's canonicalized values.
- ``table_checksum(rows)`` — order-independent, duplicate-sensitive table digest.
- ``dataset_checksum(tables)`` — roll-up over all tables (one top-line signal).
- ``compare(source, target)`` — three-level compare report with tolerance + samples.
- ``format_report(report)`` — deterministic drill-down text for CI output.

``canonicalize`` normalizes the type/encoding/collation differences between
engines (number scale, timestamp tz, CHAR/VARCHAR trailing padding, unicode form,
NULL) so equivalent values hash equal across the legacy and modernized databases.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

# Field/record/table separators (control chars unlikely to appear in data).
_FIELD_SEP = "\\x1f"
_ROW_SEP = "\\x1e"
_TABLE_SEP = "\\x1d"
_NULL = "\\x00NULL"


def canonicalize(value: Any) -> str:
    """Normalize *value* to a stable string equal across legacy/modernized engines.

    Handles the cross-engine equivalences that would otherwise break a naive
    compare: ``Decimal("1.50") == 1.5``, ``CHAR`` trailing padding vs ``VARCHAR``,
    tz-aware vs naive timestamps (assumed UTC), unicode normal form, and NULL.
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):  # before int (bool is an int subclass)
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        # Normalize numeric scale so 1.50 == 1.5; ``f`` avoids exponent form.
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        # tz-aware → UTC; naive → assume UTC. Equal instants canonicalize equal.
        as_utc = (
            value.astimezone(timezone.utc)
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
        )
        return as_utc.isoformat()
    if isinstance(value, str):
        # Strip trailing whitespace (CHAR padding) + NFC-normalize unicode.
        return unicodedata.normalize("NFC", value.rstrip())
    return str(value)


def row_checksum(row: Mapping[str, Any]) -> str:
    """A stable SHA-256 over *row*\'s canonicalized values, sorted by column name."""
    parts = [f"{key}={canonicalize(row[key])}" for key in sorted(row)]
    return hashlib.sha256(_FIELD_SEP.join(parts).encode("utf-8")).hexdigest()


def table_checksum(rows: Sequence[Mapping[str, Any]]) -> str:
    """Order-independent, duplicate-sensitive digest of a table/collection.

    Row checksums are sorted (so engine row-ordering doesn\'t matter) and the row
    count is folded in (so a missing/extra row always changes the digest).
    """
    checks = sorted(row_checksum(r) for r in rows)
    joined = f"{len(checks)}{_ROW_SEP}" + _ROW_SEP.join(checks)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def dataset_checksum(tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """Roll-up over every table checksum, sorted by table name (one signal)."""
    parts = [f"{name}={table_checksum(tables[name])}" for name in sorted(tables)]
    return hashlib.sha256(_TABLE_SEP.join(parts).encode("utf-8")).hexdigest()


def extract_table(connection_url: str, table: str) -> list[dict[str, Any]]:
    """Pull every row of *table* as a list of dicts. REPLACE with real extraction.

    Example (SQLAlchemy):
        # import sqlalchemy as sa
        # engine = sa.create_engine(connection_url)
        # with engine.connect() as conn:
        #     rows = conn.execute(sa.text(f"SELECT * FROM {table}")).mappings().all()
        #     return [dict(r) for r in rows]
    """
    raise NotImplementedError("Replace extract_table with your engine\'s row extraction")


# ---------------------------------------------------------------------------
# Tolerance, compare report, and format_report (DT-6b, #1174)
# ---------------------------------------------------------------------------


@dataclass
class ParityTolerance:
    """Known-acceptable deltas applied BEFORE checksumming (prevent false fails)."""

    ignore_tables: frozenset = field(default_factory=frozenset)
    ignore_columns: dict = field(default_factory=dict)  # table -> frozenset[col]
    ignore_row_keys: dict = field(default_factory=dict)  # table -> frozenset[key]


@dataclass
class TableResult:
    """Per-table compare outcome with row-level detail for mismatched tables."""

    table: str
    match: bool
    source_count: int
    target_count: int
    only_in_source: list
    only_in_target: list
    mismatched: list
    samples: list  # list[dict] — {"key": k, "source": {...}, "target": {...}}


@dataclass
class ParityReport:
    """Top-level compare result: dataset match + per-table breakdown."""

    dataset_match: bool
    table_results: list  # list[TableResult]
    matches: bool


def _row_key(row: Mapping[str, Any], key: str | Sequence[str]) -> Any:
    if isinstance(key, str):
        return row[key]
    return tuple(row[k] for k in key)


def _apply_tolerance(
    data: Mapping[str, Sequence[Mapping[str, Any]]],
    tolerance: ParityTolerance | None,
    key: str | Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    """Drop ignored tables/columns/rows from *data*; return a clean copy."""
    if tolerance is None:
        return {t: [dict(r) for r in rows] for t, rows in data.items()}
    out: dict[str, list[dict[str, Any]]] = {}
    for table, rows in data.items():
        if table in tolerance.ignore_tables:
            continue
        drop_cols = tolerance.ignore_columns.get(table, frozenset())
        drop_keys = tolerance.ignore_row_keys.get(table, frozenset())
        clean: list[dict[str, Any]] = []
        for row in rows:
            try:
                pk = _row_key(row, key)
            except KeyError:
                pk = None
            if pk in drop_keys:
                continue
            clean.append({k: v for k, v in row.items() if k not in drop_cols})
        out[table] = clean
    return out


def compare(
    source: Mapping[str, Sequence[Mapping[str, Any]]],
    target: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    key: str | Sequence[str] = "id",
    tolerance: ParityTolerance | None = None,
    sample_limit: int = 5,
) -> ParityReport:
    """Three-level compare: dataset → table → row with tolerance + samples.

    1. Apply tolerance (drop ignored tables/columns/row-keys from both sides).
    2. Dataset level: ``dataset_checksum`` equality → ``dataset_match``.
    3. Per table (union of names): ``table_checksum``; on mismatch, index rows
       by *key* and compute only_in_source / only_in_target / mismatched, then
       collect up to *sample_limit* side-by-side samples.
    4. ``matches = dataset_match and all(t.match for t in table_results)``.
    """
    src = _apply_tolerance(source, tolerance, key)
    tgt = _apply_tolerance(target, tolerance, key)
    ds_match = dataset_checksum(src) == dataset_checksum(tgt)
    results: list[TableResult] = []
    for table in sorted(set(src) | set(tgt)):
        s_rows = src.get(table, [])
        t_rows = tgt.get(table, [])
        t_match = table_checksum(s_rows) == table_checksum(t_rows)
        if t_match:
            results.append(
                TableResult(
                    table=table,
                    match=True,
                    source_count=len(s_rows),
                    target_count=len(t_rows),
                    only_in_source=[],
                    only_in_target=[],
                    mismatched=[],
                    samples=[],
                )
            )
            continue
        src_idx = {_row_key(r, key): r for r in s_rows}
        tgt_idx = {_row_key(r, key): r for r in t_rows}
        only_src = [k for k in src_idx if k not in tgt_idx]
        only_tgt = [k for k in tgt_idx if k not in src_idx]
        mismatch = [
            k
            for k in src_idx
            if k in tgt_idx and row_checksum(src_idx[k]) != row_checksum(tgt_idx[k])
        ]
        candidate_keys = (mismatch + only_src + only_tgt)[:sample_limit]
        samples: list[dict[str, Any]] = []
        for k in candidate_keys:
            entry: dict[str, Any] = {"key": k}
            if k in src_idx:
                entry["source"] = dict(src_idx[k])
            if k in tgt_idx:
                entry["target"] = dict(tgt_idx[k])
            samples.append(entry)
        results.append(
            TableResult(
                table=table,
                match=False,
                source_count=len(s_rows),
                target_count=len(t_rows),
                only_in_source=only_src,
                only_in_target=only_tgt,
                mismatched=mismatch,
                samples=samples,
            )
        )
    return ParityReport(
        dataset_match=ds_match,
        table_results=results,
        matches=ds_match and all(r.match for r in results),
    )


def format_report(report: ParityReport) -> str:
    """Deterministic three-level drill-down: dataset → table → row."""
    status = "MATCH" if report.matches else "MISMATCH"
    lines: list[str] = [
        f"=== Parity {status} (dataset_match={report.dataset_match}) ==="
    ]
    for r in report.table_results:
        s = "ok" if r.match else "FAIL"
        lines.append(f"  [{s}] {r.table}: src={r.source_count} tgt={r.target_count}")
        if not r.match:
            if r.only_in_source:
                lines.append(f"    only_in_source: {r.only_in_source[:5]!r}")
            if r.only_in_target:
                lines.append(f"    only_in_target: {r.only_in_target[:5]!r}")
            if r.mismatched:
                lines.append(f"    mismatched:     {r.mismatched[:5]!r}")
            for sample in r.samples:
                lines.append(f"    sample key={sample[\'key\']!r}")
                if "source" in sample:
                    lines.append(f"      src: {sample[\'source\']!r}")
                if "target" in sample:
                    lines.append(f"      tgt: {sample[\'target\']!r}")
    return "\\n".join(lines)
'''


_SELFTEST = '''"""Property self-tests for the parity checksum harness (pure-Python, no Docker).

Proves the three-level checksums are deterministic, order-independent (table +
dataset), duplicate-sensitive, and canonicalize cross-engine-equivalent values
equal. The compare/tolerance/format_report tests (DT-6b) also run here without
a container — the entire checksum + compare core is pure Python.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from parity_checksums import (
    ParityTolerance,
    canonicalize,
    compare,
    dataset_checksum,
    format_report,
    row_checksum,
    table_checksum,
)


def test_row_checksum_is_stable() -> None:
    row = {"id": 1, "name": "ada"}
    assert row_checksum(row) == row_checksum(dict(row))


def test_row_checksum_changes_on_value_change() -> None:
    assert row_checksum({"id": 1}) != row_checksum({"id": 2})


def test_canonicalize_numeric_scale_equivalent() -> None:
    # NUMERIC(10,2) 1.50 in legacy vs 1.5 in modernized → equal.
    assert row_checksum({"v": Decimal("1.50")}) == row_checksum({"v": 1.5})


def test_canonicalize_char_padding_equivalent() -> None:
    # CHAR(5) "ada  " vs VARCHAR "ada" → equal (trailing padding stripped).
    assert row_checksum({"name": "ada  "}) == row_checksum({"name": "ada"})


def test_canonicalize_timestamp_tz_equivalent() -> None:
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1, 12, 0)  # assumed UTC
    assert row_checksum({"ts": aware}) == row_checksum({"ts": naive})


def test_table_checksum_is_order_independent() -> None:
    a = [{"id": 1}, {"id": 2}, {"id": 3}]
    b = [{"id": 3}, {"id": 1}, {"id": 2}]
    assert table_checksum(a) == table_checksum(b)


def test_table_checksum_is_duplicate_sensitive() -> None:
    one = [{"id": 1}, {"id": 2}]
    dup = [{"id": 1}, {"id": 1}, {"id": 2}]
    assert table_checksum(one) != table_checksum(dup)


def test_table_checksum_detects_missing_row() -> None:
    full = [{"id": 1}, {"id": 2}]
    missing = [{"id": 1}]
    assert table_checksum(full) != table_checksum(missing)


def test_dataset_checksum_is_table_order_independent() -> None:
    rows_a = [{"id": 1}]
    rows_b = [{"id": 2}]
    assert dataset_checksum({"t1": rows_a, "t2": rows_b}) == dataset_checksum(
        {"t2": rows_b, "t1": rows_a}
    )


def test_dataset_checksum_changes_when_a_table_diverges() -> None:
    base = {"t1": [{"id": 1}], "t2": [{"id": 2}]}
    diverged = {"t1": [{"id": 1}], "t2": [{"id": 99}]}
    assert dataset_checksum(base) != dataset_checksum(diverged)


def test_canonicalize_handles_null() -> None:
    assert canonicalize(None) == canonicalize(None)
    assert row_checksum({"v": None}) != row_checksum({"v": ""})


# ---------------------------------------------------------------------------
# Compare report + tolerance (DT-6b, #1174)
# ---------------------------------------------------------------------------


def test_compare_identical_datasets_matches() -> None:
    data = {"orders": [{"id": 1, "amount": 100}, {"id": 2, "amount": 200}]}
    report = compare(data, data)
    assert report.matches is True
    assert report.dataset_match is True
    for tr in report.table_results:
        assert tr.match is True
        assert tr.only_in_source == []
        assert tr.only_in_target == []
        assert tr.mismatched == []


def test_compare_diverged_row_flags_mismatch() -> None:
    src = {"orders": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]}
    tgt = {"orders": [{"id": 1, "v": 1}, {"id": 2, "v": 99}]}
    report = compare(src, tgt)
    assert report.matches is False
    tbl = next(r for r in report.table_results if r.table == "orders")
    assert tbl.match is False
    assert 2 in tbl.mismatched
    assert any(s["key"] == 2 for s in tbl.samples)


def test_compare_only_in_source_detected() -> None:
    src = {"orders": [{"id": 1}, {"id": 2}]}
    tgt = {"orders": [{"id": 1}]}
    report = compare(src, tgt)
    tbl = report.table_results[0]
    assert 2 in tbl.only_in_source
    assert tbl.only_in_target == []


def test_compare_only_in_target_detected() -> None:
    src = {"orders": [{"id": 1}]}
    tgt = {"orders": [{"id": 1}, {"id": 2}]}
    report = compare(src, tgt)
    tbl = report.table_results[0]
    assert 2 in tbl.only_in_target
    assert tbl.only_in_source == []


def test_tolerance_ignore_column_makes_compare_pass() -> None:
    src = {"orders": [{"id": 1, "seq": 100}]}
    tgt = {"orders": [{"id": 1, "seq": 999}]}
    assert compare(src, tgt).matches is False
    tol = ParityTolerance(ignore_columns={"orders": frozenset({"seq"})})
    assert compare(src, tgt, tolerance=tol).matches is True


def test_tolerance_ignore_table_makes_compare_pass() -> None:
    src = {"orders": [{"id": 1}], "audit": [{"id": 1, "ts": "old"}]}
    tgt = {"orders": [{"id": 1}], "audit": [{"id": 1, "ts": "new"}]}
    tol = ParityTolerance(ignore_tables=frozenset({"audit"}))
    assert compare(src, tgt, tolerance=tol).matches is True


def test_tolerance_ignore_row_key_makes_compare_pass() -> None:
    src = {"orders": [{"id": 1, "v": 1}, {"id": 2, "v": 2}]}
    tgt = {"orders": [{"id": 1, "v": 1}, {"id": 2, "v": 99}]}
    tol = ParityTolerance(ignore_row_keys={"orders": frozenset({2})})
    assert compare(src, tgt, tolerance=tol).matches is True


def test_format_report_drills_down() -> None:
    src = {"orders": [{"id": 1, "v": 1}]}
    tgt = {"orders": [{"id": 1, "v": 2}]}
    report = compare(src, tgt)
    text = format_report(report)
    assert "orders" in text
    assert len(text) > 0
'''


#: Backends whose Testcontainers-Java class is NOT self-typed generic.
_JAVA_NONGENERIC: frozenset[str] = frozenset({"mongodb"})


def _java_parity(java_class: str, image: str, *, nongeneric: bool) -> str:
    decl = (
        f'static final {java_class} DB = new {java_class}("{image}");'
        if nongeneric
        else f'static final {java_class}<?> DB = new {java_class}<>("{image}");'
    )
    return f"""package parity;

import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.testcontainers.containers.{java_class};
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Three-level data-parity checksum IT, scaffolded by /test-scaffold (DT-6b).
 * Compares legacy vs modernised datasets at row, table, and dataset granularity
 * via JDBC extraction. Requires Docker; Testcontainers skips when unavailable.
 *
 * Levels:
 *   1. Row checksum  — stable hash over a canonicalised row dict.
 *   2. Table checksum — order-independent, duplicate-sensitive aggregate.
 *   3. Dataset checksum — roll-up over all tables (one top-line signal).
 */
@Testcontainers
class ParityChecksumIT {{

    @Container
    {decl}

    @Test
    void datasetsMatch() {{
        assertTrue(DB.isRunning());
        // Replace with real JDBC extraction and compare:
        // String sourceUrl = DB.getJdbcUrl(); // legacy connection
        // String targetUrl = ...; // modernised connection
        //
        // Row checksum (Replace):
        // Map<String, Object> row = extractRow(sourceUrl, "orders", 1);
        // String rowHash = rowChecksum(row);
        //
        // Table checksum (Replace):
        // List<Map<String, Object>> rows = extractTable(sourceUrl, "orders");
        // String tableHash = tableChecksum(rows);
        //
        // Dataset compare (Replace):
        // ParityReport report = compare(
        //     extractDataset(sourceUrl), extractDataset(targetUrl));
        // assertTrue(report.matches(), formatReport(report));
    }}
}}
"""


_NODEJS_PARITY = """\
import { describe, it, expect } from 'vitest';

// Data-parity checksum skeleton, scaffolded by /test-scaffold (DT-6b).
// Compares legacy vs modernised datasets at row, table, and dataset granularity.
// Replace the placeholder assertions with real DB extraction + checksum calls.

describe('data parity: three-level checksums', () => {
  it('row checksums match across legacy and modernised', async () => {
    // Replace with real row extraction:
    // const srcRow = await extractRow(legacyDb, 'orders', 1);
    // const tgtRow = await extractRow(modernDb, 'orders', 1);
    // expect(rowChecksum(srcRow)).toBe(rowChecksum(tgtRow));
    expect(true).toBe(true); // collectable-green placeholder
  });

  it('table checksums match across datasets', async () => {
    // Replace with real table extraction:
    // const srcRows = await extractTable(legacyDb, 'orders');
    // const tgtRows = await extractTable(modernDb, 'orders');
    // expect(tableChecksum(srcRows)).toBe(tableChecksum(tgtRows));
    expect(true).toBe(true);
  });

  it('full dataset compare passes tolerance check', async () => {
    // Replace with real dataset extraction + compare:
    // const report = compare(sourceDataset, targetDataset, { key: 'id' });
    // expect(report.matches).toBe(true);
    // if (!report.matches) console.log(formatReport(report));
    expect(true).toBe(true);
  });
});
"""


def _render_java_parity(backends: list[str]) -> tuple[ScaffoldFile, ...]:
    sql = [b for b in backends if b not in ("mongodb", "dynamodb")]
    bname = sql[0] if sql else "postgres"
    meta = data_backend_meta(bname)
    assert meta is not None
    nongeneric = bname in _JAVA_NONGENERIC
    return (
        ScaffoldFile(
            "src/test/java/parity/ParityChecksumIT.java",
            _java_parity(meta.java_class, meta.default_image, nongeneric=nongeneric),
        ),
    )


def _render_nodejs_parity() -> tuple[ScaffoldFile, ...]:
    return (ScaffoldFile("tests/integration/parity.checksum.test.ts", _NODEJS_PARITY),)


def render_data_parity(context: ScaffoldContext) -> ScaffoldResult:
    """Emit the three-level parity checksum harness + its property self-test.

    Python: a full stdlib-only checksum harness with three-level compare report,
    tolerance allowlist, sampled deep-compare, and a property self-test.
    Java/Node: a collectable-green skeleton documenting the three checksum levels
    and the compare API via ``// Replace with ...`` seams.
    """
    backends = resolve_backends(context)
    shown = "/".join(backends)
    if context.stack == "python":
        files: tuple[ScaffoldFile, ...] = (
            ScaffoldFile("tests/container/parity_checksums.py", _HARNESS),
            ScaffoldFile("tests/container/test_parity_checksums.py", _SELFTEST),
        )
        note = f"Three-level parity checksum harness (row/table/dataset) for {shown}"
    elif context.stack == "java":
        files = _render_java_parity(backends)
        note = f"Data-parity JDBC skeleton (row/table/dataset compare) for {shown}"
    elif context.stack == "nodejs":
        files = _render_nodejs_parity()
        note = f"Data-parity vitest skeleton (row/table/dataset compare) for {shown}"
    else:
        return ScaffoldResult(
            files=(),
            note=f"data-parity: {context.stack} stack not yet supported",
        )
    files = with_emphasis(files, context)
    return ScaffoldResult(files=files, note=note)


for _stack in ("python", "java", "nodejs"):
    register_scaffold(_stack, "data-parity", render_data_parity)
