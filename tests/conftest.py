import os
import pathlib
import uuid

import psycopg
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADMIN_DSN = os.environ.get("TEST_PG_ADMIN_DSN", "postgresql://postgres@localhost:54329/postgres")


@pytest.fixture(scope="module")
def dsn():
    name = "ctl_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"create database {name}")
    url = ADMIN_DSN.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(url, autocommit=True) as c:
        c.execute((ROOT / "tests/supabase_stubs.sql").read_text())
        c.execute((ROOT / "supabase/migrations/0001_init.sql").read_text())
        c.execute((ROOT / "supabase/seed.sql").read_text())
    yield url
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"drop database {name} with (force)")
