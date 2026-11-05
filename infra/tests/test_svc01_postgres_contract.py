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


# =========================================================================== #
# fix-pg-hba-hostssl-vs-ssl-off (§1) — pg_hba connection-type must be
# internally consistent with the server `ssl` on/off setting.
#
# Bug: pg_hba.conf.j2 emits `hostssl` for the app + exporter project-DB rules
# unconditionally, while postgresql.conf.j2 leaves `ssl = on` commented out
# (effective `ssl = off`). A `hostssl` rule can never be satisfied while the
# server runs `ssl = off`, so the least-privilege app role cannot connect over
# TCP at all. Counterexample (from the v0.13.0 deployment feedback):
#   hostssl moto_maintenance moto_maintenance_app 10.0.103.0/24 scram-sha-256
# coexisting with an effective `ssl = off`.
#
# These tests render the two templates with Jinja2 and assert the connection
# type is consistent with the server ssl state.
# =========================================================================== #

import re

from jinja2 import Environment

_PG_TEMPLATES = _PG_ROLE / "templates"
_PG_HBA_J2 = _PG_TEMPLATES / "pg_hba.conf.j2"
_PG_CONF_J2 = _PG_TEMPLATES / "postgresql.conf.j2"


def _ansible_bool(value) -> bool:
    """Minimal stand-in for Ansible's `bool` filter so role templates that use
    `... | bool` render under plain Jinja2 in these offline tests."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "on", "1")


def _render(template_path: Path, **vars_) -> str:
    """Render a role template with a permissive Jinja2 env.

    Undefined vars default to empty-ish so the template renders without the
    full Ansible var set; the test supplies only the vars under scrutiny.
    The Ansible-specific `bool` filter is registered so templates that use
    `... | bool` (e.g. the opt-in block, and the new ssl-conditional lines)
    render identically to the Ansible runtime.
    """
    env = Environment(keep_trailing_newline=True)
    env.filters["bool"] = _ansible_bool
    defaults = {
        "postgres_project_slug": vars_.get("postgres_project_slug", "moto_maintenance"),
        "postgres_vlan_id": vars_.get("postgres_vlan_id", 103),
        "postgres_db_name": vars_.get("postgres_db_name", "moto_maintenance"),
        "postgres_app_role": vars_.get("postgres_app_role", "moto_maintenance_app"),
        "postgres_exporter_role": vars_.get("postgres_exporter_role", "moto_maintenance_exporter"),
        "postgres_port": vars_.get("postgres_port", 5432),
        "postgres_max_connections": vars_.get("postgres_max_connections", 100),
        "postgres_shared_buffers": vars_.get("postgres_shared_buffers", "256MB"),
        "postgres_work_mem": vars_.get("postgres_work_mem", "16MB"),
        "postgres_log_statement": vars_.get("postgres_log_statement", "ddl"),
        "postgres_client_nontls_subnet": vars_.get("postgres_client_nontls_subnet", False),
        # The new single source of truth (default false = current reality: no cert).
        "postgres_ssl_enabled": vars_.get("postgres_ssl_enabled", False),
        "postgres_ssl_cert_file": vars_.get(
            "postgres_ssl_cert_file", "/etc/postgresql/tls/server.crt"
        ),
        "postgres_ssl_key_file": vars_.get(
            "postgres_ssl_key_file", "/etc/postgresql/tls/server.key"
        ),
    }
    return env.from_string(template_path.read_text(encoding="utf-8")).render(**defaults)


def _app_exporter_conn_types(pg_hba_rendered: str, vlan_id: int = 103) -> list[str]:
    """Return the connection-type keyword (host|hostssl) of the app + exporter
    project-subnet rules in the rendered pg_hba."""
    subnet = f"10.0.{vlan_id}.0/24"
    types: list[str] = []
    for line in pg_hba_rendered.splitlines():
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        # app/exporter project-DB rules are the ones scoped to the project subnet
        # for a specific (non-"all") database.
        if subnet in s and " all " not in f" {s} ":
            types.append(s.split()[0])
    return types


def _server_ssl_on(postgres_conf_rendered: str) -> bool:
    """True iff the rendered postgresql.conf has an ACTIVE (uncommented) ssl = on."""
    for line in postgres_conf_rendered.splitlines():
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        m = re.match(r"ssl\s*=\s*(\w+)", s)
        if m:
            return m.group(1).lower() == "on"
    return False  # no active ssl line => PostgreSQL default is off


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition / Expected Behavior.
# On unfixed code this FAILS (hostssl emitted while ssl=off). After the fix the
# default render emits `host` with an explicit `ssl = off`.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "slug,vlan_id",
    [("moto_maintenance", 103), ("dronefleet", 100), ("acme_proj", 150)],
)
def test_pg_hba_conn_type_consistent_with_server_ssl_default(slug, vlan_id):
    """Default posture (postgres_ssl_enabled=false): the app + exporter rules
    MUST be `host` (not `hostssl`) because the server renders `ssl = off`.

    isBugCondition(X) = (server_ssl == OFF) AND existsHostsslRule(pg_hba).
    """
    pg_hba = _render(
        _PG_HBA_J2,
        postgres_project_slug=slug,
        postgres_db_name=slug,
        postgres_app_role=f"{slug}_app",
        postgres_exporter_role=f"{slug}_exporter",
        postgres_vlan_id=vlan_id,
        postgres_ssl_enabled=False,
    )
    pg_conf = _render(_PG_CONF_J2, postgres_ssl_enabled=False)

    server_ssl_on = _server_ssl_on(pg_conf)
    conn_types = _app_exporter_conn_types(pg_hba, vlan_id)

    assert conn_types, "expected app + exporter project-subnet rules to be present"
    assert server_ssl_on is False, "default posture must render an explicit ssl = off"
    # The core invariant: no hostssl rule may coexist with ssl = off.
    assert all(ct == "host" for ct in conn_types), (
        f"contradiction: {conn_types} with server ssl=off — a hostssl rule can "
        "never be satisfied while the server runs ssl=off (the least-privilege "
        "app role cannot connect over TCP)"
    )


# --------------------------------------------------------------------------- #
# Property 1 — Fix Checking: TLS posture render.
# With postgres_ssl_enabled=true, the app/exporter rules are `hostssl` AND the
# server renders `ssl = on` with the cert/key wired — internally consistent.
# --------------------------------------------------------------------------- #
def test_pg_hba_conn_type_consistent_with_server_ssl_tls_enabled():
    pg_hba = _render(_PG_HBA_J2, postgres_ssl_enabled=True)
    pg_conf = _render(_PG_CONF_J2, postgres_ssl_enabled=True)

    assert _server_ssl_on(pg_conf) is True, "ssl_enabled=true must render ssl = on"
    assert "ssl_cert_file" in pg_conf and "ssl_key_file" in pg_conf, (
        "ssl = on must wire ssl_cert_file / ssl_key_file"
    )
    conn_types = _app_exporter_conn_types(pg_hba, 103)
    assert conn_types and all(ct == "hostssl" for ct in conn_types), (
        f"with ssl = on the app/exporter rules must be hostssl, got {conn_types}"
    )


def test_postgresql_conf_default_renders_explicit_ssl_off_no_cert_lines():
    """Default posture: an explicit `ssl = off` (honest, not an implicit default)
    and NO cert-path lines."""
    pg_conf = _render(_PG_CONF_J2, postgres_ssl_enabled=False)
    assert re.search(r"(?m)^\s*ssl\s*=\s*off\s*$", pg_conf), (
        "default posture must render an explicit `ssl = off`"
    )
    assert "ssl_cert_file" not in pg_conf and "ssl_key_file" not in pg_conf, (
        "ssl = off must emit no cert-path lines"
    )


# --------------------------------------------------------------------------- #
# Property 2 — Preservation: surrounding pg_hba behaviour invariant across both
# ssl states and the opt-in.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ssl_enabled", [False, True])
@pytest.mark.parametrize("nontls_opt_in", [False, True])
def test_pg_hba_preserves_local_loopback_subnet_and_optin(ssl_enabled, nontls_opt_in):
    pg_hba = _render(
        _PG_HBA_J2,
        postgres_ssl_enabled=ssl_enabled,
        postgres_client_nontls_subnet=nontls_opt_in,
        postgres_vlan_id=103,
    )
    # Local-trust socket preserved (Req 3.2).
    assert re.search(r"(?m)^\s*local\s+all\s+all\s+trust\s*$", pg_hba), (
        "the local all all trust bootstrap socket line must be preserved"
    )
    # Loopback scram preserved (Req 3.3).
    assert re.search(r"(?m)^\s*host\s+all\s+all\s+127\.0\.0\.1/32\s+scram-sha-256", pg_hba)
    assert re.search(r"(?m)^\s*host\s+all\s+all\s+::1/128\s+scram-sha-256", pg_hba)
    # Subnet scoping preserved: app/exporter rules address only the project subnet,
    # never the LAN or another VLAN (Req 3.4).
    for line in pg_hba.splitlines():
        s = line.strip()
        if s.startswith("#") or not s or s.startswith("local"):
            continue
        if "scram-sha-256" in s and "127.0.0.1" not in s and "::1" not in s:
            assert "10.0.103.0/24" in s, f"rule not scoped to project subnet: {s!r}"
    # Opt-in preserved (Req 3.5): the extra non-TLS maintenance-DB app line is
    # present iff the opt-in is on, independent of the ssl reconciliation.
    optin_line = re.search(
        r"(?m)^\s*host\s+all\s+\S*_app\s+10\.0\.103\.0/24\s+scram-sha-256", pg_hba
    )
    assert bool(optin_line) == nontls_opt_in, (
        f"nontls opt-in line presence ({bool(optin_line)}) must match the flag ({nontls_opt_in})"
    )


# --------------------------------------------------------------------------- #
# Property 1 — Fix Checking: fail-closed provision-time invariant.
# The role must carry an assert that refuses a hostssl-with-ssl=off contradiction.
# --------------------------------------------------------------------------- #
def test_fail_closed_invariant_assert_present():
    tasks = _load_tasks(_PG_TASKS)
    text = _PG_TASKS.read_text(encoding="utf-8")
    # An assert task guarding the hostssl-vs-ssl=off contradiction must exist.
    assert_tasks = [t for t in tasks if "ansible.builtin.assert" in t]
    guard = None
    for t in assert_tasks:
        body = _task_text(t)
        if "hostssl" in body and "ssl" in body:
            guard = t
            break
    assert guard is not None, (
        "a provision-time assert must fail closed on a hostssl-with-ssl=off "
        "contradiction (Req 2.4)"
    )
    # The fail_msg must name the contradiction + the remedy.
    assert "postgres_ssl_enabled" in text, (
        "the fail-closed guard / remedy must reference postgres_ssl_enabled"
    )
