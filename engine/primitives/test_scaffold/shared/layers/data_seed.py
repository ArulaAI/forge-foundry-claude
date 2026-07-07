"""Data-seed layer renderer — deterministic, seeded, idempotent test-data generation.

Emits two files per resolved data backend, dispatching by kind:

**SQL** (postgres/mysql/mssql/oracle):

1. ``tests/container/seed_data[_<backend>].py`` — a deterministic seed-data generator
   module (stdlib-only at top level): ``make_rows(seed, n)`` using ``random.Random(seed)``
   produces a stable, sorted list of dicts (same seed → identical output), and ``load()``
   shows the idempotent upsert skeleton in ``# Replace with ...`` comments.
2. ``tests/container/test_seed_data[_<backend>].py`` — ``test_seed_is_reproducible``
   (pure-Python, Docker-independent) + ``test_seed_load_is_idempotent`` (``@pytest.mark.container``,
   reuses ``db_container[_<backend>]`` fixture).

**MongoDB** (kind == ``"mongodb"``):

3+4. Same two files via pymongo; ``load()`` shows upsert-by-``_id``.

**DynamoDB** (kind == ``"dynamodb"``):

5+6. Same two files via boto3; idempotency test uses ``get_container_host_ip()`` +
     ``get_exposed_port(<port>)`` (no ``get_connection_url``).

All emitted files are collectable-green: the seed module imports only ``random`` at top
level; the test file imports only stdlib + pytest at module scope, using a ``sys.path``
injection to import ``make_rows`` from the sibling seed module. ``test_seed_is_reproducible``
is Docker-independent (pure Python). ``test_seed_load_is_idempotent`` uses the container
fixture but carries a cheap-true assertion so it passes without Docker.

The ``multi`` flag is computed on the **full** resolved backend set so file names and
fixture names match the container layer exactly.
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
# Seed-data generator modules — seed_data[_<backend>].py (stdlib-only at top)
# ---------------------------------------------------------------------------


def _py_seed_module_sql() -> str:
    """Deterministic seed-data generator module for SQL backends."""
    return (
        '"""Deterministic seed-data generator for the SQL test database'
        " (data-seed layer).\n\n"
        "Generate stable, reproducible rows and load them idempotently into the\n"
        "test database so every test run starts from a known state:\n\n"
        "    from seed_data import make_rows, load\n\n"
        "    rows = make_rows(seed=1234, n=50)   # same seed -> identical output\n"
        "    load(connection, rows)              # re-run is a no-op (idempotent upsert)\n\n"
        "Imports only the stdlib at top level so this module is always importable\n"
        "(collectable-green) without installing any DB driver.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import random\n\n\n"
        "def make_rows(seed: int = 1234, n: int = 50) -> list[dict]:\n"
        "    rng = random.Random(seed)\n"
        "    rows = [\n"
        "        {\n"
        '            "id": i + 1,\n'
        '            "value": rng.randint(1, 10_000),\n'
        '            "category": rng.choice(["alpha", "beta", "gamma"]),\n'
        '            "score": round(rng.uniform(0.0, 1.0), 4),\n'
        "        }\n"
        "        for i in range(n)\n"
        "    ]\n"
        '    return sorted(rows, key=lambda r: r["id"])\n\n\n'
        "def load(connection: object, rows: list[dict]) -> None:\n"
        "    # Replace with your SQL driver's idempotent upsert, e.g. (psycopg3):\n"
        "    # import psycopg  # noqa: PLC0415\n"
        "    # with connection.cursor() as cur:  # type: ignore[union-attr]\n"
        "    #     for row in rows:\n"
        "    #         cur.execute(\n"
        '    #             "INSERT INTO seed_table (id, value, category, score)"\n'
        '    #             " VALUES (%(id)s, %(value)s, %(category)s, %(score)s)"\n'
        '    #             " ON CONFLICT (id) DO NOTHING",\n'
        "    #             row,\n"
        "    #         )\n"
        "    #     connection.commit()  # type: ignore[union-attr]\n"
        "    pass\n"
    )


def _py_seed_module_mongo() -> str:
    """Deterministic seed-data generator module for MongoDB."""
    return (
        '"""Deterministic seed-data generator for the MongoDB test database'
        " (data-seed layer).\n\n"
        "Generate stable, reproducible documents and load them idempotently into\n"
        "the test collection so every test run starts from a known state:\n\n"
        "    from seed_data import make_rows, load\n\n"
        "    rows = make_rows(seed=1234, n=50)   # same seed -> identical output\n"
        "    load(client, rows)                  # re-run is a no-op (upsert by _id)\n\n"
        "Imports only the stdlib at top level so this module is always importable\n"
        "(collectable-green) without installing pymongo.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import random\n\n\n"
        "def make_rows(seed: int = 1234, n: int = 50) -> list[dict]:\n"
        "    rng = random.Random(seed)\n"
        "    rows = [\n"
        "        {\n"
        '            "_id": f"doc-{i + 1:04d}",\n'
        '            "value": rng.randint(1, 10_000),\n'
        '            "category": rng.choice(["alpha", "beta", "gamma"]),\n'
        '            "score": round(rng.uniform(0.0, 1.0), 4),\n'
        "        }\n"
        "        for i in range(n)\n"
        "    ]\n"
        '    return sorted(rows, key=lambda r: r["_id"])\n\n\n'
        "def load(client: object, rows: list[dict]) -> None:\n"
        "    # Replace with your pymongo idempotent upsert, e.g.:\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # db = client.get_default_database()  # type: ignore[union-attr]\n"
        "    # col = db['seed_collection']\n"
        "    # for row in rows:\n"
        "    #     col.update_one(\n"
        "    #         {'_id': row['_id']},\n"
        "    #         {'$setOnInsert': row},\n"
        "    #         upsert=True,\n"
        "    #     )\n"
        "    pass\n"
    )


def _py_seed_module_dynamo() -> str:
    """Deterministic seed-data generator module for DynamoDB."""
    return (
        '"""Deterministic seed-data generator for the DynamoDB test table'
        " (data-seed layer).\n\n"
        "Generate stable, reproducible items and load them idempotently into the\n"
        "test table so every test run starts from a known state:\n\n"
        "    from seed_data import make_rows, load\n\n"
        "    rows = make_rows(seed=1234, n=50)   # same seed -> identical output\n"
        "    load(client, rows)                  # re-run is a no-op (put_item)\n\n"
        "Imports only the stdlib at top level so this module is always importable\n"
        "(collectable-green) without installing boto3.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import random\n\n\n"
        "def make_rows(seed: int = 1234, n: int = 50) -> list[dict]:\n"
        "    rng = random.Random(seed)\n"
        "    rows = [\n"
        "        {\n"
        '            "id": f"item-{i + 1:04d}",\n'
        '            "value": rng.randint(1, 10_000),\n'
        '            "category": rng.choice(["alpha", "beta", "gamma"]),\n'
        '            "score": str(round(rng.uniform(0.0, 1.0), 4)),\n'
        "        }\n"
        "        for i in range(n)\n"
        "    ]\n"
        '    return sorted(rows, key=lambda r: r["id"])\n\n\n'
        "def load(client: object, rows: list[dict]) -> None:\n"
        "    # Replace with your boto3 idempotent put_item, e.g.:\n"
        "    # TABLE = 'seed_table'  # must exist in DynamoDB Local\n"
        "    # for row in rows:\n"
        "    #     client.put_item(  # type: ignore[union-attr]\n"
        "    #         TableName=TABLE,\n"
        "    #         Item={\n"
        "    #             'id': {'S': row['id']},\n"
        "    #             'value': {'N': str(row['value'])},\n"
        "    #             'category': {'S': row['category']},\n"
        "    #             'score': {'N': row['score']},\n"
        "    #         },\n"
        "    #     )\n"
        "    pass\n"
    )


# ---------------------------------------------------------------------------
# Test files — test_seed_data[_<backend>].py
# ---------------------------------------------------------------------------


def _py_seed_test_sql(fixture_name: str, mod_name: str) -> str:
    """Seed-data test skeleton for SQL backends."""
    return (
        '"""Seeded test-data tests (data-seed layer, /test-scaffold).\n\n'
        "Tests two invariants of the generated seed data:\n"
        "  1. reproducibility  -- make_rows(seed=N) called twice returns identical output\n"
        "  2. load idempotency -- loading rows twice does not create duplicates\n\n"
        "test_seed_is_reproducible is Docker-independent (pure Python property check).\n"
        "test_seed_load_is_idempotent requires Docker via the db_container fixture.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        f"from {mod_name} import make_rows  # noqa: E402\n\n\n"
        "def test_seed_is_reproducible() -> None:\n"
        "    assert make_rows(seed=42) == make_rows(seed=42)\n"
        "    assert make_rows(seed=7) != make_rows(seed=8)\n\n\n"
        "@pytest.mark.container\n"
        f"def test_seed_load_is_idempotent({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        "    assert url  # container is live (or the fixture skipped)\n"
        "    rows = make_rows()\n"
        "    # Replace with the real load + verify, e.g. (psycopg3):\n"
        f"    # from {mod_name} import load  # noqa: PLC0415\n"
        "    # import psycopg  # noqa: PLC0415\n"
        "    # conn = psycopg.connect(url)\n"
        "    # load(conn, rows)\n"
        "    # load(conn, rows)  # second call must be a no-op (idempotent upsert)\n"
        "    # with conn.cursor() as cur:\n"
        '    #     cur.execute("SELECT COUNT(*) FROM seed_table")\n'
        "    #     assert cur.fetchone()[0] == len(rows), 'duplicates after second load'\n"
        "    assert len(rows) > 0  # seed module produced rows (cheap-true placeholder)\n"
    )


def _py_seed_test_mongo(fixture_name: str, mod_name: str) -> str:
    """Seed-data test skeleton for MongoDB."""
    return (
        '"""Seeded test-data tests (data-seed layer, /test-scaffold).\n\n'
        "Tests two invariants of the generated seed data:\n"
        "  1. reproducibility  -- make_rows(seed=N) called twice returns identical output\n"
        "  2. load idempotency -- loading docs twice does not create duplicates\n\n"
        "test_seed_is_reproducible is Docker-independent (pure Python property check).\n"
        "test_seed_load_is_idempotent requires Docker via the db_container fixture.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        f"from {mod_name} import make_rows  # noqa: E402\n\n\n"
        "def test_seed_is_reproducible() -> None:\n"
        "    assert make_rows(seed=42) == make_rows(seed=42)\n"
        "    assert make_rows(seed=7) != make_rows(seed=8)\n\n\n"
        "@pytest.mark.container\n"
        f"def test_seed_load_is_idempotent({fixture_name}) -> None:\n"
        f"    url = {fixture_name}.get_connection_url()\n"
        '    assert url.startswith("mongodb")  # container is live (or the fixture skipped)\n'
        "    rows = make_rows()\n"
        "    # Replace with the real load + verify, e.g. (pymongo):\n"
        f"    # from {mod_name} import load  # noqa: PLC0415\n"
        "    # from pymongo import MongoClient  # noqa: PLC0415\n"
        "    # client = MongoClient(url)\n"
        "    # load(client, rows)\n"
        "    # load(client, rows)  # second call must be a no-op (upsert by _id)\n"
        "    # db = client.get_default_database()\n"
        "    # count = db['seed_collection'].count_documents({})\n"
        "    # assert count == len(rows), 'duplicate documents after second load'\n"
        "    assert len(rows) > 0  # seed module produced rows (cheap-true placeholder)\n"
    )


def _py_seed_test_dynamo(fixture_name: str, mod_name: str, port: int) -> str:
    """Seed-data test skeleton for DynamoDB."""
    return (
        '"""Seeded test-data tests (data-seed layer, /test-scaffold).\n\n'
        "Tests two invariants of the generated seed data:\n"
        "  1. reproducibility  -- make_rows(seed=N) called twice returns identical output\n"
        "  2. load idempotency -- loading items twice does not create duplicates\n\n"
        "test_seed_is_reproducible is Docker-independent (pure Python property check).\n"
        "test_seed_load_is_idempotent requires Docker via the db_container fixture.\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        f"from {mod_name} import make_rows  # noqa: E402\n\n\n"
        "def test_seed_is_reproducible() -> None:\n"
        "    assert make_rows(seed=42) == make_rows(seed=42)\n"
        "    assert make_rows(seed=7) != make_rows(seed=8)\n\n\n"
        "@pytest.mark.container\n"
        f"def test_seed_load_is_idempotent({fixture_name}) -> None:\n"
        f"    host = {fixture_name}.get_container_host_ip()\n"
        f"    port = {fixture_name}.get_exposed_port({port})\n"
        "    assert int(port) > 0  # container is live (or the fixture skipped)\n"
        "    rows = make_rows()\n"
        "    # Replace with the real load + verify, e.g. (boto3):\n"
        f"    # from {mod_name} import load  # noqa: PLC0415\n"
        "    # import boto3  # noqa: PLC0415\n"
        "    # client = boto3.client(\n"
        '    #     "dynamodb",\n'
        f'    #     endpoint_url=f"http://{{host}}:{{port}}",\n'
        '    #     region_name="us-east-1",\n'
        '    #     aws_access_key_id="test",\n'
        '    #     aws_secret_access_key="test",\n'
        "    # )\n"
        "    # load(client, rows)\n"
        "    # load(client, rows)  # second call must be a no-op (put_item is idempotent)\n"
        "    # count = client.scan(TableName='seed_table')['Count']\n"
        "    # assert count == len(rows), 'unexpected item count after second load'\n"
        "    assert len(rows) > 0  # seed module produced rows (cheap-true placeholder)\n"
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
        mod_name = f"seed_data_{backend}" if multi else "seed_data"
        seed_path = (
            f"tests/container/seed_data_{backend}.py" if multi else "tests/container/seed_data.py"
        )
        test_path = (
            f"tests/container/test_seed_data_{backend}.py"
            if multi
            else "tests/container/test_seed_data.py"
        )
        if meta.kind == "sql":
            seed_content = _py_seed_module_sql()
            test_content = _py_seed_test_sql(fixture_name, mod_name)
        elif meta.kind == "mongodb":
            seed_content = _py_seed_module_mongo()
            test_content = _py_seed_test_mongo(fixture_name, mod_name)
        elif meta.kind == "dynamodb":
            seed_content = _py_seed_module_dynamo()
            test_content = _py_seed_test_dynamo(fixture_name, mod_name, meta.port)
        else:
            continue
        files.append(ScaffoldFile(seed_path, seed_content))
        files.append(ScaffoldFile(test_path, test_content))
    return tuple(files)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_data_seed(context: ScaffoldContext) -> ScaffoldResult:
    """Emit a seed-data generator + reproducibility/idempotency tests per resolved backend.

    Per backend, emits two files:

    - ``seed_data[_<backend>].py`` — stdlib-only generator module: ``make_rows(seed, n)``
      (seeded ``random.Random``, stable sorted output) + ``load()`` skeleton for the
      backend's idempotent upsert driver (in ``# Replace with ...`` comments).
    - ``test_seed_data[_<backend>].py`` — ``test_seed_is_reproducible`` (Docker-independent,
      pure Python) + ``test_seed_load_is_idempotent`` (``@pytest.mark.container``).

    The ``multi`` flag is computed on the **full** resolved backend set so file names
    and fixture names match the container layer exactly.
    """
    backends = resolve_backends(context)

    if not backends:
        files: tuple[ScaffoldFile, ...] = ()
        note = "data-seed: no backends resolved — skipped"
        return ScaffoldResult(files=files, note=note)

    # `multi` is computed on the FULL resolved backend set (not any kind-filtered subset)
    # so seed_data[_<backend>].py and db_container[_<backend>] fixture names are in sync.
    multi = len(backends) > 1

    files = _render_python(context, backends, multi=multi) if context.stack == "python" else ()

    files = with_emphasis(files, context)
    shown = "/".join(backends)
    note = f"data-seed generator + reproducibility/idempotency tests ({shown})"
    return ScaffoldResult(files=files, note=note)


register_scaffold("python", "data-seed", render_data_seed)
