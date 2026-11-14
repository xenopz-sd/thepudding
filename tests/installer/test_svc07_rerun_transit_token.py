"""Bug-condition + preservation tests: an SVC-07 re-run converges using the primary's own seal token (WS-C41, ADR-0027).

THE BUG (found live, WS-C36): a second `svc-07-bootstrap.yml` run halted at "No Bootstrap_Transit_Token is
available" (the token is in memory only during a fresh mint), and with the token it would still halt at the
drift guard (Req 5.11). No config change could reach a running primary through the installer.
THE FIX (operator decision B): when the token is neither in memory nor in the environment, read it from the seal
stanza of the primary's existing config.hcl over SSH (no_log, never persisted). In that re-run mode, skip the drift
guard and the Admin_Token capture/persist, and require a VALID existing OPENBAO_ADMIN_TOKEN (lookup-self) instead.
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
_TASKS = _REPO / "ansible/roles/svc07_bootstrap/tasks/main.yml"
_DEFAULTS = _REPO / "ansible/roles/svc07_bootstrap/defaults/main.yml"
_READ = "Bootstrap | Re-run: read the primary's existing config.hcl for the seal token (no_log)"
_BIND = "Bootstrap | Re-run: bind the seal token from the primary config (no_log)"
_RESOLVE = "Bootstrap | Resolve the transit token for the primary child env (no_log)"
_ASSERT_TT = "Bootstrap | Assert a Bootstrap_Transit_Token is available for the primary (no_log)"
_CHILD = "Bootstrap | Task 8.4 — primary bootstrap (openbao.yml install,init + mint, no_log)"
_DRIFT = "Bootstrap | Drift guard — require the primary ran a fresh bootstrap (Req 5.11, no_log)"
_ADMIN_RESOLVE = "Bootstrap | Re-run: resolve the existing platform-admin token (no_log)"
_ADMIN_CHECK = "Bootstrap | Re-run: verify the existing platform-admin token is valid (lookup-self, no_log)"
_ADMIN_ASSERT = "Bootstrap | Re-run: require a valid existing platform-admin token (else clean-slate)"
_CAPTURE = "Bootstrap | Capture the revealed Admin_Token from the child output (no_log)"
_CAPTURED = "Bootstrap | Assert the Admin_Token was captured (no_log)"
_PERSIST = "Bootstrap | Persist OPENBAO_ADMIN_TOKEN to the Cluster_Env_File at 0600 (Req 5.8, no_log)"

_CONFIG = '''storage "raft" {
  path = "/openbao/data"
}
seal "transit" {
  address    = "https://10.0.20.11:8200"
  token      = "s.seal-token-value"
  key_name   = "openbao-unseal"
}
'''


def _tasks():
    return [t for t in yaml.safe_load(_TASKS.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _names():
    return [t.get("name") for t in _tasks()]


def _by(name):
    hits = [t for t in _tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


def _when(t):
    w = t.get("when", [])
    return " ".join(str(x) for x in (w if isinstance(w, list) else [w]))


def test_BUG_the_rerun_read_precedes_the_resolve_and_the_assert():
    n = _names()
    assert n.index(_READ) < n.index(_BIND) < n.index(_RESOLVE) < n.index(_ASSERT_TT) < n.index(_CHILD)


def test_the_read_is_delegated_with_connection_vars_hidden_and_never_fatal():
    t = _by(_READ)
    assert t["ansible.builtin.slurp"]["src"] == "{{ svc07_primary_config_path }}"
    assert t["delegate_to"] == "{{ svc07_primary_host }}"
    assert {"ansible_user", "ansible_connection", "ansible_python_interpreter"} <= set(t["vars"])
    assert t["no_log"] is True and t["failed_when"] is False
    w = _when(t)
    assert "svc07_bootstrap_transit_token" in w and "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN" in w


def test_defaults_name_the_primary_and_its_config_path():
    d = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert d["svc07_primary_host"] == "10.0.20.10"
    assert d["svc07_primary_config_path"] == "/openbao/config/config.hcl"


def _bind(content):
    t = _by(_BIND)
    v = {"svc07_primary_config_read": {"content": base64.b64encode(content.encode()).decode()},
         **{k: trust_as_template(x) for k, x in t.get("vars", {}).items()}}
    sf = t["ansible.builtin.set_fact"]
    return _tpl(sf["svc07_guest_transit_token"], v).strip(), str(_tpl(sf["svc07_rerun_mode"], v)).strip()


def test_the_token_is_parsed_from_the_seal_stanza_only():
    assert _bind(_CONFIG) == ("s.seal-token-value", "True")
    assert _bind('storage "raft" {\n  token = "not-this"\n}\n') == ("", "False")
    assert _bind("") == ("", "False")


def test_resolve_order_is_memory_then_env_then_the_guest():
    expr = _by(_RESOLVE)["ansible.builtin.set_fact"]["svc07_primary_transit_token"]
    expr = expr.replace("lookup('ansible.builtin.env', 'OPENBAO_BOOTSTRAP_TRANSIT_TOKEN', default='')", "_env")
    base = {"svc07_guest_transit_token": "guest", "_env": "env"}
    assert _tpl(expr, {**base, "svc07_bootstrap_transit_token": "mem"}).strip() == "mem"
    assert _tpl(expr, base).strip() == "env"
    assert _tpl(expr, {**base, "_env": ""}).strip() == "guest"


@pytest.mark.parametrize("name", [_DRIFT, _CAPTURE, _CAPTURED, _PERSIST])
def test_fresh_bootstrap_only_steps_are_skipped_in_rerun_mode(name):
    assert "not (svc07_rerun_mode | default(false) | bool)" in _when(_by(name))


@pytest.mark.parametrize("name", [_ADMIN_RESOLVE, _ADMIN_CHECK, _ADMIN_ASSERT])
def test_rerun_mode_requires_a_valid_existing_admin_token(name):
    t = _by(name)
    assert "svc07_rerun_mode | default(false) | bool" in _when(t)
    n = _names()
    assert n.index(_CHILD) < n.index(name)


def test_the_admin_check_is_hidden_and_the_assert_names_the_remedy():
    assert _by(_ADMIN_RESOLVE)["no_log"] is True
    assert _by(_ADMIN_CHECK)["no_log"] is True
    a = _by(_ADMIN_ASSERT)["ansible.builtin.assert"]
    assert "clean_slate=true" in a["fail_msg"] and "OPENBAO_ADMIN_TOKEN" in a["fail_msg"]


def test_preservation_a_fresh_install_still_uses_the_drift_guard_and_persists_the_token():
    assert "TOKEN=" in " ".join(_by(_DRIFT)["ansible.builtin.assert"]["that"])
    assert "lineinfile" in str(_by(_PERSIST))
