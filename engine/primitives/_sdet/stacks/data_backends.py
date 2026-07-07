"""Data-backend (database engine) coordinate catalog (#1124, DT-2.2).

Self-registers all six built-in data backends via :func:`register_data_backend`
so :func:`~primitives._sdet.stacks.all_data_backends` and
:func:`~primitives._sdet.stacks.data_backend_meta` are populated for any
consumer that imports ``primitives._sdet.stacks`` (the package). Mirrors the
self-registration pattern of the per-stack language modules.

The six backends cover the SQL, MongoDB, and DynamoDB families that the S4
``test_scaffold`` container layer targets (design spec
``docs/superpowers/specs/2026-06-29-dt2-data-backend-registry-container-design.md``).
Signal tuples must not be changed without a corresponding update to the DT-1.1
detector — these are the verbatim substrings matched (lowercased) against a
project's manifest text.
"""

from __future__ import annotations

from primitives._sdet.stacks import register_data_backend
from primitives._sdet.stacks.contract import DataBackendMeta

# ---------------------------------------------------------------------------
# Backend catalog — module-level constants, one per engine.
# ---------------------------------------------------------------------------

POSTGRES = DataBackendMeta(
    name="postgres",
    signals=("psycopg", "asyncpg", "org.postgresql", "npgsql", '"pg"'),
    kind="sql",
    default_image="postgres:16",
    port=5432,
    py_module="testcontainers.postgres.PostgresContainer",
    java_class="PostgreSQLContainer",
)

MYSQL = DataBackendMeta(
    name="mysql",
    signals=("pymysql", "mysqlclient", "mysql-connector", "mysql2", "mysqlconnector"),
    kind="sql",
    default_image="mysql:8",
    port=3306,
    py_module="testcontainers.mysql.MySqlContainer",
    java_class="MySQLContainer",
)

MSSQL = DataBackendMeta(
    name="mssql",
    signals=("pyodbc", "pymssql", "mssql-jdbc", "microsoft.data.sqlclient", "tedious"),
    kind="sql",
    default_image="mcr.microsoft.com/mssql/server:2022-latest",
    port=1433,
    py_module="testcontainers.mssql.SqlServerContainer",
    java_class="MSSQLServerContainer",
)

ORACLE = DataBackendMeta(
    name="oracle",
    signals=("oracledb", "cx_oracle", "ojdbc", "oracle.manageddataaccess"),
    kind="sql",
    default_image="gvenzl/oracle-free:latest",
    port=1521,
    py_module="testcontainers.oracle.OracleDbContainer",
    java_class="OracleContainer",
)

MONGODB = DataBackendMeta(
    name="mongodb",
    signals=("pymongo", "motor", "mongoose", "mongodb-driver", "mongodb.driver"),
    kind="mongodb",
    default_image="mongo:7",
    port=27017,
    py_module="testcontainers.mongodb.MongoDbContainer",
    java_class="MongoDBContainer",
)

DYNAMODB = DataBackendMeta(
    name="dynamodb",
    signals=("boto3", "aioboto3", "client-dynamodb", "awssdk.dynamodbv2"),
    kind="dynamodb",
    default_image="amazon/dynamodb-local:latest",
    port=8000,
    py_module="testcontainers.core.container.DockerContainer",
    java_class="GenericContainer",
)

# ---------------------------------------------------------------------------
# Self-registration — wire all backends into the registry on import.
# ---------------------------------------------------------------------------

_BACKENDS: tuple[DataBackendMeta, ...] = (
    POSTGRES,
    MYSQL,
    MSSQL,
    ORACLE,
    MONGODB,
    DYNAMODB,
)

for _meta in _BACKENDS:
    register_data_backend(_meta.name, _meta)
