"""Data-quality layer renderer — value-level quality assertions and ETL / pipeline-correctness.

Emits two test skeletons per resolved data backend, dispatching by kind:

**SQL** (postgres/mysql/mssql/oracle):

1. ``tests/container/test_quality[_<backend>].py`` — a pandera ``DataFrameSchema``
   skeleton covering the six value-level check families against a sample DataFrame
   pulled from the live containerised database.
2. ``tests/container/test_etl[_<backend>].py`` — ETL / pipeline-correctness probes:
   idempotency (re-run yields identical output) + source↔target reconciliation
   (row-count + key-set between source and target tables).

**MongoDB** (kind == ``"mongodb"``):

3. ``tests/container/test_quality[_<backend>].py`` — a generic document-query
   skeleton covering the same six families via pymongo queries. Pandera is a
   tabular-data library; document/key-value stores use their native query APIs.
4. ``tests/container/test_etl[_<backend>].py`` — ETL / pipeline-correctness probes
   via pymongo: document-count idempotency + ``_id`` key-set reconciliation.

**DynamoDB** (kind == ``"dynamodb"``):

5. ``tests/container/test_quality[_<backend>].py`` — a generic item-query skeleton
   covering the six families via boto3, using ``get_container_host_ip()`` +
   ``get_exposed_port(<port>)`` (no ``get_connection_url``).
6. ``tests/container/test_etl[_<backend>].py`` — ETL / pipeline-correctness probes
   via boto3: item-count idempotency + primary-key set reconciliation.

Six value-level check families covered across all backends (quality tests):

1. **null-rate**    — required / nullable columns stay within acceptable null proportions
2. **ranges**       — numeric values fall within expected bounds
3. **allowed-sets** — categorical columns contain only known values
4. **uniqueness**   — identity / key columns have no duplicates
5. **row counts**   — table / collection item totals match expected lower / upper bounds
6. **freshness**    — timestamp columns contain recent-enough records

All files share the container layer's ``db_container[_<backend>]`` fixture so
``--data-tests`` (which implies ``container``) is the typical entry point. The
``multi`` flag is computed on the **full** resolved backend set so fixture names
match the container layer exactly.

All emitted tests are collectable-green: module-level imports are ``pytest``-only;
the real DB interaction is shown as ``# Replace with ...`` comments; a cheap-and-true
assertion makes the test pass when Docker is present and skip (via the fixture) otherwise.
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


# ---------------------------------------------------------------------------
# Python skeletons — SQL (pandera DataFrameSchema)
# ---------------------------------------------------------------------------


def _py_sql(fixture_name: str, fn_name: str) -> str:
    """Pandera DataFrameSchema quality skeleton for SQL backends."""
    return (
        '"""Value-level data-quality test (data-quality layer, /test-scaffold).\n\n'
        "Checks the six quality assertion families against a sample read from the\n"
        "live containerised database:\n"
        "  1. null-rate    — nullable columns stay within acceptable null proportions\n"
        "  2. ranges       — numeric values fall within expected bounds\n"
        "  3. allowed-sets — categorical columns contain only known values\n"
        "  4. uniqueness   — identity / key columns have no duplicates\n"
        "  5. row counts   — table row totals match expected lower / upper bounds\n"
        "  6. freshness    — timestamp columns contain recent-enough records\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green). Pandera is a tabular-data quality\n"
        "library suited to SQL/columnar sources.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        "    assert url  # container is live (or the fixture skipped)\n"
        "    # Replace with a real pandera DataFrameSchema check, e.g.:\n"
        "    # import pandas as pd  # noqa: PLC0415\n"
        "    # import pandera as pa  # noqa: PLC0415\n"
        "    # import sqlalchemy  # noqa: PLC0415\n"
        "    #\n"
        "    # engine = sqlalchemy.create_engine(url)\n"
        '    # df = pd.read_sql("SELECT * FROM your_table LIMIT 1000", engine)\n'
        "    #\n"
        "    # schema = pa.DataFrameSchema(\n"
        "    #     {\n"
        "    #         # 1. null-rate: column allows nulls within an acceptable proportion\n"
        '    #         "your_nullable_col": pa.Column(nullable=True),\n'
        "    #         # 2. ranges: numeric values fall within expected bounds\n"
        '    #         "your_numeric_col": pa.Column(\n'
        "    #             checks=pa.Check.in_range(0, 10_000)\n"
        "    #         ),\n"
        "    #         # 3. allowed-sets: only known categorical values are present\n"
        '    #         "your_status_col": pa.Column(\n'
        '    #             checks=pa.Check.isin(["active", "inactive", "pending"])\n'
        "    #         ),\n"
        "    #         # 4. uniqueness: no duplicate identity values\n"
        '    #         "your_id_col": pa.Column(unique=True),\n'
        "    #     }\n"
        "    # )\n"
        "    # schema.validate(df)\n"
        "    #\n"
        "    # # 5. row counts: table has records within expected bounds\n"
        '    # count_df = pd.read_sql("SELECT COUNT(*) AS n FROM your_table", engine)\n'
        '    # assert count_df["n"].iloc[0] >= 1, "table appears empty"\n'
        "    #\n"
        "    # # 6. freshness: most recent record is within acceptable age\n"
        "    # import datetime  # noqa: PLC0415\n"
        "    # fresh_df = pd.read_sql(\n"
        '    #     "SELECT MAX(updated_at) AS latest FROM your_table", engine\n'
        "    # )\n"
        '    # latest = fresh_df["latest"].iloc[0]\n'
        "    # cutoff = datetime.datetime.now() - datetime.timedelta(days=7)\n"
        '    # assert latest >= cutoff, "no records updated in the last 7 days"\n'
    )


# ---------------------------------------------------------------------------
# Python skeletons — SQL ETL / pipeline-correctness
# ---------------------------------------------------------------------------


def _py_etl_sql(fixture_name: str) -> str:
    """ETL / pipeline-correctness skeleton for SQL backends."""
    return (
        '"""ETL / pipeline-correctness tests (data-quality layer, /test-scaffold).\n\n'
        "Checks two pipeline invariants against the live containerised database:\n"
        "  1. idempotency        — re-running the transform/load yields identical output\n"
        "  2. source↔target reconcile — row-count + key-set between source and target\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def test_pipeline_is_idempotent({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        "    assert url  # container is live (or the fixture skipped)\n"
        "    # Replace with your pipeline's idempotency check, e.g.:\n"
        "    # import sqlalchemy  # noqa: PLC0415\n"
        "    # engine = sqlalchemy.create_engine(url)\n"
        "    # run_transform_load(engine)  # first run\n"
        "    # count_1 = engine.execute('SELECT COUNT(*) FROM target_table').scalar()\n"
        "    # run_transform_load(engine)  # second run (re-run must be a no-op)\n"
        "    # count_2 = engine.execute('SELECT COUNT(*) FROM target_table').scalar()\n"
        "    # assert count_1 == count_2, 'pipeline is not idempotent'\n\n\n"
        "@pytest.mark.container\n"
        f"def test_source_target_reconcile({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        "    assert url  # container is live (or the fixture skipped)\n"
        "    # Replace with your pipeline's source↔target reconciliation, e.g.:\n"
        "    # import sqlalchemy  # noqa: PLC0415\n"
        "    # engine = sqlalchemy.create_engine(url)\n"
        "    # run_transform_load(engine)\n"
        "    # src_n = engine.execute('SELECT COUNT(*) FROM source_table').scalar()\n"
        "    # tgt_n = engine.execute('SELECT COUNT(*) FROM target_table').scalar()\n"
        "    # assert src_n == tgt_n, f'row-count mismatch: {src_n} vs {tgt_n}'\n"
        "    # # key-set reconciliation:\n"
        "    # src_keys = {r[0] for r in engine.execute('SELECT id FROM source_table')}\n"
        "    # tgt_keys = {r[0] for r in engine.execute('SELECT id FROM target_table')}\n"
        "    # missing = src_keys - tgt_keys\n"
        "    # assert not missing, f'{len(missing)} source keys missing from target'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — MongoDB
# ---------------------------------------------------------------------------


def _py_mongo(fixture_name: str, fn_name: str) -> str:
    """Generic document-query quality skeleton for MongoDB."""
    return (
        '"""Value-level data-quality test (data-quality layer, /test-scaffold).\n\n'
        "Checks the six quality assertion families against document samples from\n"
        "the live MongoDB container:\n"
        "  1. null-rate    — required fields are present in an acceptable proportion\n"
        "  2. ranges       — numeric fields fall within expected bounds\n"
        "  3. allowed-sets — enumeration fields contain only known values\n"
        "  4. uniqueness   — identity fields have no duplicates across the collection\n"
        "  5. row counts   — collection document totals match expected bounds\n"
        "  6. freshness    — date fields contain recent-enough documents\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green). Pandera is for tabular sources; use\n"
        "pymongo document queries for MongoDB (see inline comments).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        '    assert url.startswith("mongodb")  # container is live (or the fixture skipped)\n'
        "    # Replace with real pymongo quality checks, e.g.:\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # import datetime  # noqa: PLC0415\n"
        "    # client = MongoClient(url)\n"
        "    # db = client.get_default_database()\n"
        "    # col = db['your_collection']\n"
        "    #\n"
        "    # # 1. null-rate: required field present in >= 99% of documents\n"
        "    # total = col.count_documents({})\n"
        "    # missing = col.count_documents({'required_field': {'$exists': False}})\n"
        "    # assert missing / max(total, 1) < 0.01, f'{missing} docs missing field'\n"
        "    #\n"
        "    # # 2. ranges: numeric field within expected bounds\n"
        "    # out_of_range = col.count_documents({'amount': {'$lt': 0}})\n"
        "    # assert out_of_range == 0, f'{out_of_range} docs with negative amount'\n"
        "    #\n"
        "    # # 3. allowed-sets: status field contains only known values\n"
        "    # bad = col.count_documents(\n"
        "    #     {'status': {'$nin': ['active', 'inactive', 'pending']}}\n"
        "    # )\n"
        "    # assert bad == 0, f'{bad} docs with unexpected status values'\n"
        "    #\n"
        "    # # 4. uniqueness: external_id has no duplicates in the collection\n"
        "    # pipeline = [\n"
        "    #     {'$group': {'_id': '$external_id', 'count': {'$sum': 1}}},\n"
        "    #     {'$match': {'count': {'$gt': 1}}},\n"
        "    # ]\n"
        "    # dupes = list(col.aggregate(pipeline))\n"
        "    # assert not dupes, f'{len(dupes)} duplicate external_id values found'\n"
        "    #\n"
        "    # # 5. row counts: collection has at least the expected number of docs\n"
        "    # assert total >= 1, 'collection appears empty'\n"
        "    #\n"
        "    # # 6. freshness: most recent document is within acceptable age\n"
        "    # latest_doc = col.find_one({}, sort=[('updated_at', -1)])\n"
        "    # latest = latest_doc['updated_at'] if latest_doc else None\n"
        "    # cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)\n"
        "    # assert latest and latest >= cutoff, 'no docs updated in the last 7 days'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — MongoDB ETL / pipeline-correctness
# ---------------------------------------------------------------------------


def _py_etl_mongo(fixture_name: str) -> str:
    """ETL / pipeline-correctness skeleton for MongoDB."""
    return (
        '"""ETL / pipeline-correctness tests (data-quality layer, /test-scaffold).\n\n'
        "Checks two pipeline invariants against the live MongoDB container:\n"
        "  1. idempotency        — re-running the transform/load yields identical doc count\n"
        "  2. source↔target reconcile — doc-count + _id key-set between collections\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def test_pipeline_is_idempotent({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        '    assert url.startswith("mongodb")  # container is live (or the fixture skipped)\n'
        "    # Replace with your pipeline's idempotency check, e.g.:\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # client = MongoClient(url)\n"
        "    # db = client.get_default_database()\n"
        "    # run_transform_load(db)  # first run\n"
        "    # count_1 = db['target_collection'].count_documents({})\n"
        "    # run_transform_load(db)  # second run (re-run must be a no-op)\n"
        "    # count_2 = db['target_collection'].count_documents({})\n"
        "    # assert count_1 == count_2, 'pipeline is not idempotent'\n\n\n"
        "@pytest.mark.container\n"
        f"def test_source_target_reconcile({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        '    assert url.startswith("mongodb")  # container is live (or the fixture skipped)\n'
        "    # Replace with your pipeline's source↔target reconciliation, e.g.:\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # client = MongoClient(url)\n"
        "    # db = client.get_default_database()\n"
        "    # run_transform_load(db)\n"
        "    # src_n = db['source_collection'].count_documents({})\n"
        "    # tgt_n = db['target_collection'].count_documents({})\n"
        "    # assert src_n == tgt_n, f'doc-count mismatch: {src_n} vs {tgt_n}'\n"
        "    # # _id key-set reconciliation:\n"
        "    # src_ids = {d['_id'] for d in db['source_collection'].find({}, {'_id': 1})}\n"
        "    # tgt_ids = {d['_id'] for d in db['target_collection'].find({}, {'_id': 1})}\n"
        "    # missing = src_ids - tgt_ids\n"
        "    # assert not missing, f'{len(missing)} source _ids missing from target'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — DynamoDB
# ---------------------------------------------------------------------------


def _py_dynamo(fixture_name: str, fn_name: str, port: int) -> str:
    """Generic item-query quality skeleton for DynamoDB."""
    return (
        '"""Value-level data-quality test (data-quality layer, /test-scaffold).\n\n'
        "Checks the six quality assertion families against item samples from the\n"
        "live DynamoDB Local container:\n"
        "  1. null-rate    — required attributes are present in an acceptable proportion\n"
        "  2. ranges       — numeric attributes fall within expected bounds\n"
        "  3. allowed-sets — enumeration attributes contain only known values\n"
        "  4. uniqueness   — primary-key attributes have no duplicates\n"
        "  5. row counts   — table item totals match expected lower / upper bounds\n"
        "  6. freshness    — timestamp attributes contain recent-enough items\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green). Pandera is for tabular sources; use\n"
        "boto3 DynamoDB queries for DynamoDB (see inline comments).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def {fn_name}({fixture_name}) -> None:\n"
        f"    host = {fixture_name}.get_container_host_ip()\n"
        f"    port = {fixture_name}.get_exposed_port({port})\n"
        "    assert int(port) > 0  # container is live (or the fixture skipped)\n"
        "    # Replace with real boto3 quality checks, e.g.:\n"
        "    # import boto3  # noqa: PLC0415\n"
        "    # import datetime  # noqa: PLC0415\n"
        "    # client = boto3.client(\n"
        '    #     "dynamodb",\n'
        f'    #     endpoint_url=f"http://{{host}}:{{port}}",\n'
        '    #     region_name="us-east-1",\n'
        '    #     aws_access_key_id="test",\n'
        '    #     aws_secret_access_key="test",\n'
        "    # )\n"
        '    # tables = client.list_tables()["TableNames"]\n'
        "    # assert tables, 'no tables found'\n"
        "    # items = client.scan(TableName=tables[0]).get('Items', [])\n"
        "    #\n"
        "    # # 1. null-rate: required attribute present in >= 99% of items\n"
        "    # missing = [i for i in items if 'required_attr' not in i]\n"
        "    # assert len(missing) / max(len(items), 1) < 0.01, 'too many items missing attr'\n"
        "    #\n"
        "    # # 2. ranges: numeric attribute within expected bounds\n"
        "    # out_of_range = [i for i in items if int(i.get('amount', {}).get('N', '0')) < 0]\n"
        "    # assert not out_of_range, f'{len(out_of_range)} items with negative amount'\n"
        "    #\n"
        "    # # 3. allowed-sets: status attribute contains only known values\n"
        "    # known = {'active', 'inactive', 'pending'}\n"
        "    # bad = [i for i in items if i.get('status', {}).get('S') not in known]\n"
        "    # assert not bad, f'{len(bad)} items with unexpected status values'\n"
        "    #\n"
        "    # # 4. uniqueness: DynamoDB enforces primary-key uniqueness by design\n"
        "    #\n"
        "    # # 5. row counts: table has items within expected bounds\n"
        "    # desc = client.describe_table(TableName=tables[0])\n"
        '    # assert desc["Table"]["ItemCount"] >= 1, "table appears empty"\n'
        "    #\n"
        "    # # 6. freshness: most recent item is within acceptable age\n"
        "    # import time  # noqa: PLC0415\n"
        "    # ts_vals = [float(i['updated_at']['N']) for i in items if 'updated_at' in i]\n"
        "    # if ts_vals:\n"
        "    #     assert max(ts_vals) >= time.time() - 7 * 86400, 'items may be stale'\n"
    )


# ---------------------------------------------------------------------------
# Python skeletons — DynamoDB ETL / pipeline-correctness
# ---------------------------------------------------------------------------


def _py_etl_dynamo(fixture_name: str, port: int) -> str:
    """ETL / pipeline-correctness skeleton for DynamoDB."""
    return (
        '"""ETL / pipeline-correctness tests (data-quality layer, /test-scaffold).\n\n'
        "Checks two pipeline invariants against the live DynamoDB Local container:\n"
        "  1. idempotency        — re-running the transform/load yields identical item count\n"
        "  2. source↔target reconcile — item-count + primary-key set between tables\n\n"
        "Requires Docker + the container layer's db_container fixture; self-skips\n"
        "without Docker (collectable-green).\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n\n"
        "@pytest.mark.container\n"
        f"def test_pipeline_is_idempotent({fixture_name}) -> None:\n"
        f"    host = {fixture_name}.get_container_host_ip()\n"
        f"    port = {fixture_name}.get_exposed_port({port})\n"
        "    assert int(port) > 0  # container is live (or the fixture skipped)\n"
        "    # Replace with your pipeline's idempotency check, e.g.:\n"
        "    # import boto3  # noqa: PLC0415\n"
        "    # client = boto3.client(\n"
        '    #     "dynamodb",\n'
        f'    #     endpoint_url=f"http://{{host}}:{{port}}",\n'
        '    #     region_name="us-east-1",\n'
        '    #     aws_access_key_id="test",\n'
        '    #     aws_secret_access_key="test",\n'
        "    # )\n"
        "    # run_transform_load(client)  # first run\n"
        "    # count_1 = client.scan(TableName='target_table')['Count']\n"
        "    # run_transform_load(client)  # second run (re-run must be a no-op)\n"
        "    # count_2 = client.scan(TableName='target_table')['Count']\n"
        "    # assert count_1 == count_2, 'pipeline is not idempotent'\n\n\n"
        "@pytest.mark.container\n"
        f"def test_source_target_reconcile({fixture_name}) -> None:\n"
        f"    host = {fixture_name}.get_container_host_ip()\n"
        f"    port = {fixture_name}.get_exposed_port({port})\n"
        "    assert int(port) > 0  # container is live (or the fixture skipped)\n"
        "    # Replace with your pipeline's source↔target reconciliation, e.g.:\n"
        "    # import boto3  # noqa: PLC0415\n"
        "    # client = boto3.client(\n"
        '    #     "dynamodb",\n'
        f'    #     endpoint_url=f"http://{{host}}:{{port}}",\n'
        '    #     region_name="us-east-1",\n'
        '    #     aws_access_key_id="test",\n'
        '    #     aws_secret_access_key="test",\n'
        "    # )\n"
        "    # run_transform_load(client)\n"
        "    # src_n = client.scan(TableName='source_table')['Count']\n"
        "    # tgt_n = client.scan(TableName='target_table')['Count']\n"
        "    # assert src_n == tgt_n, f'item-count mismatch: {src_n} vs {tgt_n}'\n"
        "    # # primary-key set reconciliation:\n"
        "    # src_items = client.scan(TableName='source_table').get('Items', [])\n"
        "    # tgt_items = client.scan(TableName='target_table').get('Items', [])\n"
        "    # src_keys = {i['pk']['S'] for i in src_items}\n"
        "    # tgt_keys = {i['pk']['S'] for i in tgt_items}\n"
        "    # missing = src_keys - tgt_keys\n"
        "    # assert not missing, f'{len(missing)} source keys missing from target'\n"
    )


# ---------------------------------------------------------------------------
# Python renderer — dispatches all backends by kind
# ---------------------------------------------------------------------------


def _render_python(
    _context: ScaffoldContext, backends: list[str], *, multi: bool
) -> tuple[ScaffoldFile, ...]:
    files: list[ScaffoldFile] = []
    for backend in backends:
        meta = data_backend_meta(backend)
        assert meta is not None
        fixture_name = f"db_container_{backend}" if multi else "db_container"
        quality_path = (
            f"tests/container/test_quality_{backend}.py"
            if multi
            else "tests/container/test_quality.py"
        )
        etl_path = (
            f"tests/container/test_etl_{backend}.py" if multi else "tests/container/test_etl.py"
        )
        if meta.kind == "sql":
            quality_content = _py_sql(fixture_name, "test_data_quality_assertions")
            etl_content = _py_etl_sql(fixture_name)
        elif meta.kind == "mongodb":
            quality_content = _py_mongo(fixture_name, "test_data_quality_assertions")
            etl_content = _py_etl_mongo(fixture_name)
        elif meta.kind == "dynamodb":
            quality_content = _py_dynamo(fixture_name, "test_data_quality_assertions", meta.port)
            etl_content = _py_etl_dynamo(fixture_name, meta.port)
        else:
            continue
        files.append(ScaffoldFile(quality_path, quality_content))
        files.append(ScaffoldFile(etl_path, etl_content))
    return tuple(files)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_data_quality(context: ScaffoldContext) -> ScaffoldResult:
    """Emit quality + ETL tests for every resolved data backend, dispatching by kind.

    Per backend, emits two files:

    - ``test_quality[_<backend>].py`` — value-level quality assertions (six check
      families: null-rate, ranges, allowed-sets, uniqueness, row counts, freshness).
      SQL → pandera ``DataFrameSchema``; MongoDB → pymongo queries; DynamoDB → boto3.
    - ``test_etl[_<backend>].py`` — ETL / pipeline-correctness probes
      (``test_pipeline_is_idempotent`` + ``test_source_target_reconcile``).

    The ``multi`` flag is computed on the **full** resolved backend set so fixture
    names match the container layer exactly.
    """
    backends = resolve_backends(context)

    if not backends:
        files: tuple[ScaffoldFile, ...] = ()
        note = "data-quality: no backends resolved — skipped"
        return ScaffoldResult(files=files, note=note)

    # `multi` is computed on the FULL resolved backend set (not any kind-filtered subset)
    # so the db_container[_<backend>] fixture names match the container layer exactly.
    multi = len(backends) > 1

    files = _render_python(context, backends, multi=multi) if context.stack == "python" else ()

    files = with_emphasis(files, context)
    shown = "/".join(backends)
    note = f"data-quality + ETL tests ({shown})"
    return ScaffoldResult(files=files, note=note)


register_scaffold("python", "data-quality", render_data_quality)
