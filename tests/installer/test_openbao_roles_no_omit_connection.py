"""Bug-condition + preservation tests: the OpenBao onboard roles never set connection vars to `omit` (WS-C41 part 2).

THE BUG (found live, WS-C36): `openbao_project_onboard` and `openbao_workload_approle` set `ansible_connection`,
`ansible_user` and `ansible_python_interpreter` to `omit` when no delegate host is given. ansible-core 2.21 rejects
an omit placeholder in task vars ("A template was resolved to an Omit scalar" / a connection plugin named after the
placeholder), so the standalone (no delegate) path failed before any OpenBao call.
THE FIX: each role captures the host's own connection values once, first, and the no-delegate branch reuses them.
The delegate branch is unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLES = Path(__file__).resolve().parents[2] / "ansible/roles"
_CASES = [("openbao_project_onboard", "openbao_onboard"), ("openbao_workload_approle", "openbao_workload")]
_CAPTURE = "Capture this host's own connection settings (for the no-delegate path)"


def _tasks(role):
    return [t for t in yaml.safe_load((_ROLES / role / "tasks/main.yml").read_text(encoding="utf-8"))
            if isinstance(t, dict)]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


@pytest.mark.parametrize("role,prefix", _CASES)
def test_BUG_no_task_var_resolves_to_omit(role, prefix):
    text = (_ROLES / role / "tasks/main.yml").read_text(encoding="utf-8")
    assert "else omit" not in text


@pytest.mark.parametrize("role,prefix", _CASES)
def test_the_capture_is_the_first_task(role, prefix):
    first = _tasks(role)[0]
    assert _CAPTURE in first["name"]
    sf = first["ansible.builtin.set_fact"]
    assert set(sf) == {f"{prefix}_self_connection", f"{prefix}_self_user", f"{prefix}_self_python"}


@pytest.mark.parametrize("role,prefix", _CASES)
def test_every_delegated_task_falls_back_to_the_captured_values(role, prefix):
    delegated = [t for t in _tasks(role) if "delegate_to" in t and "vars" in t and "ansible_connection" in t["vars"]]
    assert delegated, "non-vacuity"
    for t in delegated:
        v = t["vars"]
        for key, suffix in (("ansible_connection", "connection"), ("ansible_user", "user"),
                            ("ansible_python_interpreter", "python")):
            assert f"{prefix}_self_{suffix}" in v[key], (t["name"], key)


@pytest.mark.parametrize("role,prefix", _CASES)
def test_branch_values(role, prefix):
    t = next(t for t in _tasks(role) if "delegate_to" in t and "vars" in t and "ansible_connection" in t["vars"])
    expr = t["vars"]["ansible_connection"]
    base = {f"{prefix}_self_connection": "local", f"{prefix}_delegate_user": "root"}
    assert _tpl(expr, {**base, f"{prefix}_delegate_host": ""}).strip() == "local"
    assert _tpl(expr, {**base, f"{prefix}_delegate_host": "10.0.20.10"}).strip() == "ssh"


@pytest.mark.parametrize("role,prefix", _CASES)
def test_capture_defaults_are_safe(role, prefix):
    sf = _tasks(role)[0]["ansible.builtin.set_fact"]
    assert _tpl(sf[f"{prefix}_self_connection"], {}).strip() == "ssh"
    assert _tpl(sf[f"{prefix}_self_python"], {}).strip() == "auto_silent"
