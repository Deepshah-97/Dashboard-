import json

import psycopg
import pytest


def as_role(dsn, role, claims=None):
    c = psycopg.connect(dsn, autocommit=False)
    c.execute(f"set local role {role}")
    if claims is not None:
        c.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
    return c


def test_defaults_are_fail_cheap(dsn):
    with psycopg.connect(dsn) as c:
        row = c.execute("select enabled, llm_mode, voice_calls_enabled from project_config where project_id='recruit-ai'").fetchone()
    assert row == (False, "free", False)


def test_anon_reads_public_only(dsn):
    with as_role(dsn, "anon") as c:
        assert c.execute("select count(*) from projects").fetchone()[0] == 1
        assert c.execute("select status from project_public_status").fetchone()[0] == "paused"
        assert c.execute("select total_runs from project_stats").fetchone()[0] == 0
        for table in ("project_config", "events", "project_services", "project_tokens", "owners"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"select * from {table}")
            c.rollback()
            c.execute("set local role anon")


def test_non_owner_sees_no_config(dsn):
    with as_role(dsn, "authenticated", {"email": "stranger@example.com"}) as c:
        assert c.execute("select count(*) from project_config").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("update project_config set llm_mode='paid'")


def test_owner_reads_config_but_not_tokens(dsn):
    with as_role(dsn, "authenticated", {"email": "OWNER_EMAIL@example.com"}) as c:
        assert c.execute("select count(*) from project_config").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("select * from project_tokens")
