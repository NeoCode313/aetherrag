"""Run the catalog contract suite against an isolated real PostgreSQL schema.

Opt in with CATALOG_TEST_DATABASE_URL pointing at a test database whose user can
create schemas. Each test creates/drops only its own UUID-prefixed schema. No
existing application table or volume is removed.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.infrastructure.catalog import Catalog
from tests.unit import test_catalog as contracts

DATABASE_URL = os.getenv("CATALOG_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="CATALOG_TEST_DATABASE_URL is not configured")


@pytest.fixture
def catalog():
    value = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        pytest.fail("CATALOG_TEST_DATABASE_URL must select a PostgreSQL test database")
    schema = "catalog_test_" + uuid.uuid4().hex
    admin = create_engine(url, pool_pre_ping=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
    instance = None
    try:
        instance = Catalog(isolated.render_as_string(hide_password=False))
        instance.ensure_generation("local-v1-8-normalized", "test_index_v1", {
            "provider": "local", "model": "hash-v1", "dimension": 8, "normalization": "l2",
        })
        yield instance
    finally:
        if instance:
            instance.close()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


# One suite, two engines: imported contract functions receive this module's real
# PostgreSQL fixture. These cover concurrency, delivery, leases and publication.
for _name, _function in vars(contracts).items():
    if _name.startswith("test_") and _name != "test_sqlite_is_explicitly_test_only":
        globals()[_name] = _function
