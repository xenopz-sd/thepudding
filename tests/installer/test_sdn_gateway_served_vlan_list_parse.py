"""v0.14.0 operator feedback #2: the served-VLAN list must be parsed correctly and the parse must be visible.

Premise note: the phantom-VLAN mis-parse itself was fixed by WS-C12 (post-v0.14.0). This file adds
(1) preservation tests for the exact 3-VLAN shapes the operator used, and
(2) the new behavior: the role echoes the PARSED served set (plus the raw value) before acting, and the
    playbook header documents the accepted list forms.

Pure offline: the real expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _ROOT / "ansible/roles/sdn_gateway/tasks/main.yml"
_PLAYBOOK = _ROOT / "ansible/playbooks/sdn-gateway.yml"


def _tasks():
    return [t for t in yaml.safe_load(_ROLE.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _norm(raw):
    t = next(t for t in _tasks() if "Normalize the active / decommissioned VLAN-ID sets" in str(t.get("name")))
    return _tpl(t["ansible.builtin.set_fact"]["sdn_gateway_served_project_vlan_ids_norm"],
                {"sdn_gateway_served_project_vlan_ids": raw})


@pytest.mark.parametrize("raw", ["[100,101,102]", "[100, 101, 102]", "100,101,102", [100, 101, 102],
                                 ["100", "101", "102"]])
def test_three_vlan_list_forms_all_parse_to_the_same_set(raw):
    """The operator's exact shape: [100,101,102] must never become [0,1] (or any phantom set)."""
    assert _norm(raw) == [100, 101, 102]


def _echo_task():
    return next((t for t in _tasks() if "echo the parsed served-VLAN set" in str(t.get("name"))), None)


def test_role_echoes_the_parsed_served_set():
    t = _echo_task()
    assert t is not None, "the role must carry an INFO echo of the parsed served-VLAN set"
    msg = t["ansible.builtin.debug"]["msg"]
    out = _tpl(msg, {
        "sdn_gateway_served_vlan_ids": [20, 100, 101, 102],
        "sdn_gateway_served_project_vlan_ids_norm": [100, 101, 102],
        "sdn_gateway_served_project_vlan_ids": "[100,101,102]",
    })
    assert "serving VLANs: [20, 100, 101, 102]" in out
    assert "[100, 101, 102]" in out and "[100,101,102]" in out, "echo must show parsed AND raw value"


def test_echo_runs_after_the_served_set_is_computed_and_before_validation():
    names = [str(t.get("name")) for t in _tasks()]
    echo = next(i for i, n in enumerate(names) if "echo the parsed served-VLAN set" in n)
    compute = next(i for i, n in enumerate(names) if "Compute the served-VLAN set" in n)
    validate = next(i for i, n in enumerate(names) if "Validate every opted-in" in n)
    assert compute < echo < validate


def test_echo_is_not_no_log_hidden_and_carries_no_secret_vars():
    t = _echo_task()
    assert t is not None
    assert "no_log" not in t
    msg = str(t["ansible.builtin.debug"]["msg"]).lower()
    for word in ("token", "secret", "password"):
        assert word not in msg


def test_playbook_header_documents_accepted_list_forms():
    text = _PLAYBOOK.read_text(encoding="utf-8")
    assert "JSON array" in text and "comma string" in text
    assert "INFO sdn_gateway serving VLANs" in text
