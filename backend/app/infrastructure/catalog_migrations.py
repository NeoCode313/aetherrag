"""Small, explicit forward-only migrations (never drop/recreate live catalog data).

The PostgreSQL transaction-scoped advisory lock serializes API/worker startup.
Future incompatible changes must add a new version and an explicit upgrade.
"""

from sqlalchemy import Column, Integer, MetaData, Table, insert, inspect, select, text
from sqlalchemy.engine import Engine

from app.infrastructure.catalog_schema import control, metadata, settings

HEAD = 2
version_metadata = MetaData()
versions = Table(
    "catalog_schema_version", version_metadata,
    Column("version", Integer, primary_key=True),
)


def migrate(engine: Engine) -> int:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(418700219)"))
        versions.create(connection, checkfirst=True)
        current = connection.scalar(select(versions.c.version).order_by(versions.c.version.desc())) or 0
        if current > HEAD:
            raise RuntimeError("Catalog schema is newer than this application; refusing to start")
        if current == 0:
            metadata.create_all(connection)
            connection.execute(insert(control).values(
                control_id=1, admissions_paused=False, activations_paused=False,
            ))
            connection.execute(insert(versions).values(version=1))
        if current < 2:
            settings.create(connection, checkfirst=True)
            columns = {column["name"] for column in inspect(connection).get_columns("ingestion_jobs")}
            if "replacement_job_id" not in columns:
                connection.execute(text("ALTER TABLE ingestion_jobs ADD COLUMN replacement_job_id VARCHAR(64)"))
            connection.execute(insert(versions).values(version=2))
        return HEAD
