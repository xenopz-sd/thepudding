"""Bug-condition + preservation tests: the consumer flow resolves OPENBAO_ADMIN_TOKEN like the other installers (WS-C45).

THE BUG (found live, WS-C38): SVC-01 and SVC-17/18 resolve the platform-admin token from the process environment,
else the cluster env file, and fail fast at preflight. The consumer roles used a bare `lookup('env')`, so with the
token only in the cluster env file the run got to Phase 3 and failed at the DB-secret presence check (after the DB
had already been provisioned).
THE FIX: consumer_preflight resolves `consumer_openbao_admin_token` (env first, else the file) and fails closed when
OpenBao is wired and the token is empty; every consumer use, the composed OpenBao roles and the ZITADEL child run
take that fact. The composed roles keep their env-var default when no value is supplied (additive).
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_ROLES = _REPO / "ansible/roles"
_PRE = _ROLES / "consumer_preflight/tasks/main.yml"
_BOOT = _ROLES / "consumer_bootstrap/tasks/main.yml"
_HAND = _ROLES / "consumer_handoff/tasks/main.yml"
_RESOLVE = "Preflight | Resolve the OpenBao admin token (process env first, else the cluster env file) (no_log)"
_GATE = "Preflight | Fail closed if OpenBao is wired but no admin token is resolvable"


def _tasks(p):
    return [t for t in yaml.safe_load(p.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _by(p, name):
    hits = [t for t in _tasks(p) if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


def test_BUG_preflight_resolves_the_token_and_gates_before_phase_2():
    names = [t.get("name") for t in _tasks(_PRE)]
    assert names.index(_RESOLVE) < names.index(_GATE)
    assert _by(_PRE, _RESOLVE)["no_log"] is True


def _resolve(file_text, env_value=""):
    expr = _by(_PRE, _RESOLVE)["ansible.builtin.set_fact"]["consumer_openbao_admin_token"]
    expr = expr.replace("lookup('ansible.builtin.env', 'OPENBAO_ADMIN_TOKEN')", "_env_token")
    v = {"_env_token": env_value,
         "consumer_cluster_env_slurp": {"content": base64.b64encode(file_text.encode()).decode()}}
    return _tpl(expr, v).strip()


def test_env_wins_then_the_cluster_env_file():
    f = "PROXMOX_ENDPOINT=x\nOPENBAO_ADMIN_TOKEN=from-file\n"
    assert _resolve(f, "from-env") == "from-env"
    assert _resolve(f) == "from-file"
    assert _resolve('OPENBAO_ADMIN_TOKEN="quoted"\n') == "quoted"
    assert _resolve("OTHER=1\n") == ""


def test_the_gate_fails_closed_only_when_wired_and_names_both_sources():
    g = _by(_PRE, _GATE)
    when = " ".join(g["when"]) if isinstance(g["when"], list) else g["when"]
    assert "openbao_addr" in when
    assert "consumer_openbao_admin_token" in " ".join(g["ansible.builtin.assert"]["that"])
    msg = g["ansible.builtin.assert"]["fail_msg"]
    assert "OPENBAO_ADMIN_TOKEN" in msg and "cluster_env_file" in msg
    assert "consumer_openbao_admin_token }}" not in msg, "never print the value"


@pytest.mark.parametrize("path", [_BOOT, _HAND])
def test_BUG_no_consumer_role_reads_the_token_with_a_bare_env_lookup(path):
    assert "lookup('ansible.builtin.env', 'OPENBAO_ADMIN_TOKEN')" not in path.read_text(encoding="utf-8")
    assert "consumer_openbao_admin_token" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("role", ["openbao_project_onboard", "openbao_workload_approle"])
def test_composed_openbao_roles_accept_a_supplied_token_and_keep_their_env_default(role):
    t = next(t for t in _tasks(_ROLES / role / "tasks/main.yml")
             if "Resolve the platform-admin token" in str(t.get("name")))
    expr = t["ansible.builtin.set_fact"]["openbao_admin_token"]
    assert "openbao_admin_token_value" in expr
    assert "lookup('ansible.builtin.env', openbao_admin_token_env_var" in expr, "env default preserved"


def test_the_consumer_passes_the_token_to_both_composed_roles():
    text = _BOOT.read_text(encoding="utf-8")
    assert text.count('openbao_admin_token_value: "{{ consumer_openbao_admin_token }}"') == 2


def test_the_zitadel_child_run_gets_the_token_in_its_environment():
    t = next(t for t in _tasks(_BOOT) if "Compose zitadel-org-provision" in str(t.get("name")))
    assert t["environment"]["OPENBAO_ADMIN_TOKEN"] == "{{ consumer_openbao_admin_token }}"
    assert t["no_log"] is True


# --------------------------------------------------------------------------- #
# WS-C42 live finding: the consumer CLEAN-SLATE had the same gap (token only from the process env)
# --------------------------------------------------------------------------- #
_CS = _ROLES / "consumer_clean_slate/tasks/main.yml"
_CS_RESOLVE = "Clean-Slate | Resolve the OpenBao admin token (process env first, else the cluster env file) (no_log)"


def test_BUG_the_clean_slate_resolves_the_token_before_the_user_manager_teardown():
    names = [t.get("name") for t in _tasks(_CS)]
    teardown = next(i for i, n in enumerate(names) if "Compose the user-manager identity teardown" in (n or ""))
    assert names.index(_CS_RESOLVE) < teardown
    assert _by(_CS, _CS_RESOLVE)["no_log"] is True


def test_the_user_manager_teardown_child_gets_the_resolved_token():
    t = next(t for t in _tasks(_CS) if "Compose the user-manager identity teardown" in (t.get("name") or ""))
    assert t["environment"]["OPENBAO_ADMIN_TOKEN"] == "{{ consumer_openbao_admin_token }}"
    assert t["no_log"] is True
