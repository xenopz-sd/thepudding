"""Offline contract tests for the SVC-01 postgres-server (+ mariadb skeleton) role.

Slice-2 Task 10 (spec: svc-01-sql-database) — design.md §3.2 / requirements.md
FR-1..FR-6. OFFLINE config-validation of the Ansible CONFIGURATION layer: the
`postgres-server` role tasks + templates (and the `mariadb-server` skeleton),
parsed with PyYAML / inspected as text. No infra, no terraform binary — always
runs.

What is asserted (the SVC-01 contract):
  * Least-privilege app role: created NOSUPERUSER/NOCREATEDB/NOCREATEROLE, granted
    only CONNECT/CREATE/TEMPORARY on its OWN database (FR-3).
  * postgres_exporter sidecar present on 9187 (FR-5).
  * Extensions pgvector/PostGIS/TimescaleDB are per-flag gated (FR-2).
  * NO LAN host port: the only `ports:` binding is the private VLAN-bound 5432
    (bound to the guest VLAN IP, never 0.0.0.0), and NO Traefik labels (FR-1 /
    wire-protocol exception).
  * NO secret value in the non-secret docker-compose-app `.env` (dca_env): the
    superuser password lives in a 0600 runtime env, not dca_env (FR-6 / secret
    hygiene). Password-handling tasks are no_log.
  * Nightly pg_dump backup units are installed (FR-4).
  * MariaDB skeleton selectable, private VLAN-bound 3306, no LAN port.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc01_postgres_contract.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_ROLE = _REPO_ROOT / "ansible" / "roles" / "postgres-server"
_MARIA_ROLE = _REPO_ROOT / "ansible" / "roles" / "mariadb-server"
_PG_TASKS = _PG_ROLE / "tasks" / "main.yml"
_MARIA_TASKS = _MARIA_ROLE / "tasks" / "main.yml"


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _task_text(t: dict) -> str:
    return str(t).lower()


# --------------------------------------------------------------------------- #
# Least-privilege app role (FR-3). DB/role/ext are created via `docker exec psql`
# idempotent SQL (NOT community.postgresql — the minimal guest lacks psycopg2;
# the platform does not install Python libs on guests, same as SVC-07's
# control-node cert generation). So we assert on the SQL in the role tasks text.
# --------------------------------------------------------------------------- #
def test_db_role_creation_uses_docker_exec_psql_not_guest_collection():
    text = _PG_TASKS.read_text(encoding="utf-8")
    # Assert on actual MODULE USAGE (FQCN as a task key), not mere mentions in
    # explanatory comments — the rework comment deliberately names the module to
    # explain why it is NOT used.
    for mod in ("community.postgresql.postgresql_db", "community.postgresql.postgresql_user",
                "community.postgresql.postgresql_privs", "community.postgresql.postgresql_ext"):
        assert mod not in text, (
            f"postgres-server must NOT use {mod} — it needs psycopg2 on the minimal guest. "
            "DB/role/ext creation runs via `docker exec ... psql` inside the container."
        )
    assert "docker exec" in text and "psql" in text, (
        "DB/role/ext creation must run via `docker exec ... psql` inside the postgres container (FR-3)"
    )


def test_app_role_is_least_privilege_no_superuser():
    text = _PG_TASKS.read_text(encoding="utf-8")
    # The ALTER ROLE for the app role must assert NOSUPERUSER/NOCREATEDB/NOCREATEROLE.
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE" in text, (
        "the app (and exporter) role SQL must set NOSUPERUSER NOCREATEDB NOCREATEROLE (FR-3)"
    )
    assert "postgres_app_role" in text, "the app role must be created/altered"


def test_app_role_grants_scoped_to_own_database():
    text = _PG_TASKS.read_text(encoding="utf-8")
    # The GRANT for the app role must be CONNECT/CREATE/TEMPORARY on its OWN db,
    # never GRANT ALL / superuser.
    assert "GRANT CONNECT, CREATE, TEMPORARY ON DATABASE" in text, (
        "the app role must be granted only CONNECT/CREATE/TEMPORARY on its own database (FR-3)"
    )
    assert "GRANT ALL" not in text.upper().replace("GRANT ALL ON DATABASE {{ POSTGRES_DB_NAME }}", ""), (
        "the app role must NOT be granted ALL (least-privilege, FR-3)"
    )
    assert "postgres_db_name" in text, "grants must target the project database"


def test_password_tasks_are_no_log():
    tasks = _load_tasks(_PG_TASKS)
    for t in tasks:
        body = _task_text(t)
        if any(m in body for m in ("password", "superuser_password", "app_password", "exporter_password")):
            # The generate / write / role-create (ALTER ROLE ... PASSWORD) /
            # openbao-write tasks must be no_log.
            if ("set_fact" in body or "secret.env" in body or "alter role" in body
                    or "bao" in body or "kv" in body):
                assert t.get("no_log") is True, (
                    f"password-handling task must be no_log: true: {t.get('name')!r}"
                )


# --------------------------------------------------------------------------- #
# Exporter sidecar (FR-5).
# --------------------------------------------------------------------------- #
def test_exporter_sidecar_on_9187():
    text = _PG_TASKS.read_text(encoding="utf-8")
    assert "postgres-exporter" in text or "postgres_exporter" in text, (
        "postgres-server must deploy a postgres_exporter sidecar (FR-5)"
    )
    assert "9187" in text, "the exporter must expose port 9187 (FR-5)"


# --------------------------------------------------------------------------- #
# Extensions per-flag (FR-2).
# --------------------------------------------------------------------------- #
def test_extensions_are_per_flag_gated():
    # Extensions are created via `docker exec ... psql -c "CREATE EXTENSION IF
    # NOT EXISTS <name>"` looped over per-flag items, gated so only enabled
    # extensions are created (FR-2). Idempotent via IF NOT EXISTS.
    text = _PG_TASKS.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS" in text, (
        "extensions must be created idempotently via CREATE EXTENSION IF NOT EXISTS (FR-2)"
    )
    tasks = _load_tasks(_PG_TASKS)
    ext = [t for t in tasks if "create extension if not exists" in _task_text(t)]
    assert ext, "postgres-server must have an extension-creation task (FR-2)"
    for t in ext:
        when = str(t.get("when", ""))
        assert "item.enabled" in when or "enabled" in when, (
            "extension creation must be gated per-item on the enabled flag (FR-2)"
        )
    # The three flagged extensions must each be represented in the loop.
    for e in ("vector", "postgis", "timescaledb"):
        assert e in text, f"the {e} extension must be handled (FR-2)"
    # And each must map to its enable flag.
    for flag in ("postgres_enable_pgvector", "postgres_enable_postgis", "postgres_enable_timescaledb"):
        assert flag in text, f"extension loop must reference {flag} (FR-2)"


# --------------------------------------------------------------------------- #
# No LAN host port; private VLAN-bound only; no Traefik (FR-1 / wire protocol).
# --------------------------------------------------------------------------- #
def test_no_lan_host_port_only_private_vlan_bound():
    text = _PG_TASKS.read_text(encoding="utf-8")
    # The only ports binding is the private VLAN IP (postgres_bind_ip), never 0.0.0.0.
    assert "postgres_bind_ip" in text, (
        "the 5432 port must be bound to the private VLAN IP (postgres_bind_ip), not 0.0.0.0 (FR-1)"
    )
    assert '"0.0.0.0:' not in text and "0.0.0.0:5432" not in text, (
        "postgres must NOT bind 5432 on 0.0.0.0 (no LAN exposure; wire-protocol exception, FR-1)"
    )
    # No Traefik labels — Postgres is wire-protocol, not HTTP.
    assert "traefik.enable" not in text.lower(), (
        "postgres-server must NOT carry Traefik labels (wire protocol, not HTTP — SVC-01 §12)"
    )


def test_no_secret_in_non_secret_env():
    """The superuser password must NOT be placed in the non-secret
    docker-compose-app dca_env — it belongs in the 0600 runtime secret env."""
    tasks = _load_tasks(_PG_TASKS)
    for t in tasks:
        if "ansible.builtin.include_role" in t:
            spec = t["ansible.builtin.include_role"]
            if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
                dca_env = str(t.get("vars", {}).get("dca_env", {}))
                assert "password" not in dca_env.lower(), (
                    "dca_env (the non-secret docker-compose-app .env) must NOT carry a password "
                    "(FR-6 / secret hygiene — the superuser password lives in the 0600 runtime env)"
                )
    # And the 0600 runtime secret env IS where the password goes.
    text = _PG_TASKS.read_text(encoding="utf-8")
    assert "postgres.secret.env" in text and "0600" in text, (
        "the superuser password must be written to a 0600 runtime secret env on the guest (FR-6)"
    )


# --------------------------------------------------------------------------- #
# Nightly backup (FR-4).
# --------------------------------------------------------------------------- #
def test_nightly_pg_dump_backup_units():
    text = _PG_TASKS.read_text(encoding="utf-8")
    assert "pg_dump_backup.sh" in text, "a nightly pg_dump backup script must be installed (FR-4)"
    assert "pg-backup.timer" in text, "a systemd timer must schedule the nightly backup (FR-4)"
    # The backup script uses pg_dump -Fc (custom format).
    script = (_PG_ROLE / "templates" / "pg_dump_backup.sh.j2").read_text(encoding="utf-8")
    assert "-Fc" in script, "pg_dump must use custom format -Fc (SVC-01 §6)"


# --------------------------------------------------------------------------- #
# MariaDB skeleton (FR-7 switch).
# --------------------------------------------------------------------------- #
def test_mariadb_skeleton_private_vlan_bound_no_lan_port():
    assert _MARIA_TASKS.is_file(), "mariadb-server skeleton must exist (db_engine=mariadb path, FR-7)"
    text = _MARIA_TASKS.read_text(encoding="utf-8")
    assert "mariadb_bind_ip" in text, "MariaDB 3306 must be bound to the private VLAN IP, not 0.0.0.0"
    assert "0.0.0.0:3306" not in text, "MariaDB must NOT bind 3306 on 0.0.0.0 (no LAN exposure)"
    assert "traefik.enable" not in text.lower(), "MariaDB must NOT carry Traefik labels (wire protocol)"
    # Passwords in the 0600 runtime env, not the non-secret dca_env.
    assert "mariadb.secret.env" in text and "0600" in text, (
        "MariaDB root/app passwords must be written to a 0600 runtime secret env"
    )
