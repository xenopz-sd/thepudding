"""Bug-condition + preservation tests: the SVC-01 OpenBao credential write is check-then-write (WS-C34).

THE BUG (found live on throwaway `shrimp`, WS-C34): a wired re-run of `svc-01-bootstrap` re-ran
`bao kv put` of the IDENTICAL credential every time. In KV v2 that creates a new secret version
(version 1 -> 2, values unchanged) and the task reports `changed` (rc == 0), so a second run can never be
`changed=0` (Property 7). The earlier decoupling fix wrongly assumed a re-put of the same value is a no-op.
THE FIX: compare the stored credential with the one about to be written and skip the write when they are
equal. The write still runs when the secret is absent or any value differs (self-healing is preserved).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_PG = Path(__file__).resolve().parents[2] / "ansible/roles/postgres-server/tasks/main.yml"
_SYNC = "Postgres | Detect whether the stored OpenBao credential already matches the desired one (no_log)"
_WRITE = "Postgres | Persist DB credentials to OpenBao via the bao CLI (no_log)"
_ASSERT = "Postgres | Assert the OpenBao credential write succeeded on a wired run"
_ORDER = [
    "Postgres | Reuse existing OpenBao passwords when present (no_log)",
    "Postgres | Generate DB passwords (no_log)",
    _SYNC,
    _WRITE,
    _ASSERT,
]

_DESIRED = {
    "postgres_superuser_password": "s", "postgres_app_password": "a", "postgres_exporter_password": "e",
    "postgres_app_role": "app", "postgres_exporter_role": "exp", "postgres_db_name": "db",
    "postgres_bind_ip": "10.0.20.14", "postgres_port": 5432,
}
_STORED = {"superuser_password": "s", "app_password": "a", "exporter_password": "e", "app_role": "app",
           "exporter_role": "exp", "db_name": "db", "host": "10.0.20.14", "port": "5432"}


def _tasks():
    return [t for t in yaml.safe_load(_PG.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _by(name):
    hits = [t for t in _tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _in_sync(read_rc=0, stored=_STORED, **override):
    t = _by(_SYNC)
    def trust(v):
        if isinstance(v, str):
            return trust_as_template(v)
        if isinstance(v, dict):
            return {k: trust(x) for k, x in v.items()}
        return v

    tv = {k: trust(v) for k, v in t.get("vars", {}).items()}
    import json
    read = {"rc": read_rc, "stdout": json.dumps({"data": {"data": stored}}) if stored is not None else ""}
    v = {**_DESIRED, **override, **tv, "postgres_openbao_read": read}
    out = _tpl(t["ansible.builtin.set_fact"]["postgres_openbao_in_sync"], v)
    return str(out).strip().lower() == "true"


def test_the_sync_check_sits_between_generate_and_write():
    names = [t.get("name") for t in _tasks()]
    idx = [names.index(n) for n in _ORDER]
    assert idx == sorted(idx)


def test_the_sync_check_hides_secrets():
    t = _by(_SYNC)
    assert t["no_log"] is True and t["changed_when"] is False


def test_BUG_an_identical_stored_credential_is_in_sync_so_no_write():
    assert _in_sync() is True


def test_an_absent_secret_is_not_in_sync_so_the_write_still_runs():
    assert _in_sync(read_rc=2, stored=None) is False
    assert _in_sync(stored={}) is False


@pytest.mark.parametrize("key,value", [
    ("app_password", "other"), ("superuser_password", "x"), ("exporter_password", "x"),
    ("app_role", "x"), ("exporter_role", "x"), ("db_name", "x"), ("host", "10.0.20.99"), ("port", "5433"),
])
def test_any_differing_value_is_not_in_sync(key, value):
    assert _in_sync(stored={**_STORED, key: value}) is False


def test_a_missing_key_is_not_in_sync():
    s = dict(_STORED)
    s.pop("exporter_password")
    assert _in_sync(stored=s) is False


def test_the_write_is_skipped_only_when_in_sync_and_keeps_the_address_gate():
    w = _by(_WRITE)
    cond = " ".join(str(c) for c in (w["when"] if isinstance(w["when"], list) else [w["when"]]))
    assert "postgres_openbao_in_sync" in cond and "not" in cond
    assert "OPENBAO_ADDR" in cond, "the wired-run address gate is preserved"


def test_the_loud_assert_accepts_an_in_sync_skip_but_still_fails_a_real_failed_write():
    a = _by(_ASSERT)["ansible.builtin.assert"]
    that = " ".join(a["that"]) if isinstance(a["that"], list) else a["that"]
    assert "postgres_openbao_in_sync" in that

    def ok(in_sync, skipped, rc):
        v = {"postgres_openbao_in_sync": in_sync, "postgres_openbao_write": {"skipped": skipped, "rc": rc}}
        return all(_tpl("{{ " + c + " }}", v) in (True, "True") for c in (a["that"] if isinstance(a["that"], list) else [a["that"]]))

    assert ok(True, True, None) is True, "in sync: skipped write is fine"
    assert ok(False, False, 0) is True, "needed write succeeded"
    assert ok(False, True, None) is False, "needed write silently skipped must still fail loudly"
    assert ok(False, False, 1) is False, "failed write must fail"


# --------------------------------------------------------------------------- #
# WS-C34 part 2: the DB role create/update tasks are check-then-change as well
# --------------------------------------------------------------------------- #
_ROLES = [
    ("app", "Postgres | Create/update the least-privilege app role (no SUPERUSER, no_log)",
     "Postgres | Probe whether the app role already matches (attributes, grants, password) (no_log)",
     "postgres_app_role_probe", "postgres_app_password"),
    ("exporter", "Postgres | Create/update the read-only exporter role (no_log)",
     "Postgres | Probe whether the exporter role already matches (attributes, grants, password) (no_log)",
     "postgres_exporter_role_probe", "postgres_exporter_password"),
]


@pytest.mark.parametrize("label,apply_name,probe_name,reg,pw", _ROLES)
def test_BUG_role_apply_is_gated_on_a_probe_not_unconditionally_changed(label, apply_name, probe_name, reg, pw):
    names = [t.get("name") for t in _tasks()]
    assert names.index(probe_name) < names.index(apply_name)
    probe, apply_ = _by(probe_name), _by(apply_name)
    assert probe["register"] == reg
    assert probe["changed_when"] is False and probe["no_log"] is True
    cond = " ".join(str(c) for c in (apply_["when"] if isinstance(apply_["when"], list) else [apply_["when"]]))
    assert reg in cond and "OK" in cond, "the apply runs only when the probe did not report OK"
    assert apply_["no_log"] is True


@pytest.mark.parametrize("label,apply_name,probe_name,reg,pw", _ROLES)
def test_probe_checks_attributes_grants_and_a_real_password_login_without_leaking(label, apply_name, probe_name, reg, pw):
    probe = _by(probe_name)
    cmd = probe["ansible.builtin.shell"]["cmd"]
    assert "pg_roles" in cmd and "has_database_privilege" in cmd
    assert "-h 127.0.0.1" in cmd and "SELECT 1" in cmd, "verifies the password by actually logging in"
    assert "echo OK" in cmd and "DRIFT" in cmd
    assert "PGPASSWORD" in cmd and ("PGPASSWORD=" not in cmd), "the password is inherited from the environment, never on an argv"
    assert pw in str(probe["environment"])


def test_the_app_probe_pins_the_createdb_opt_in_so_flipping_it_triggers_the_apply():
    cmd = _by(_ROLES[0][2])["ansible.builtin.shell"]["cmd"]
    assert "postgres_app_role_createdb" in cmd and "rolcreatedb" in cmd
