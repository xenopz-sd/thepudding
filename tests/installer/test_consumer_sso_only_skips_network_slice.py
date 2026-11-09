"""WS-C24 — a consumer manifest that declares no guest does not need a project network slice.

Found live (WS-C19): onboarding an SSO-only manifest (only ``sso:``, no ``database`` and no ``workload``) still demanded the
project's VNet ``p<vlan>``. With auto-create ON it applied the project's network-slice root (creating an SDN VNet for a project
that has no guest); with auto-create OFF it failed closed in preflight. Neither makes sense: a VLAN is only needed to place a
guest. The registry slug resolution stays (the slug is still allocated and never reused); only the slice requirement goes.

Pure offline: tasks are parsed and the real Jinja expressions are evaluated with Ansible's templater.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_PRE = _ROOT / "ansible" / "roles" / "consumer_preflight" / "tasks" / "main.yml"
_PROV = _ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"

_REQ = "Decide whether the manifest needs a project network slice"
_ASSERT = "Fail closed if the project VNet is absent AND auto-create is OFF"
_INFO = "INFO — project VNet decision"
_SLICE = "Auto-create the project VNet when absent"


def _tasks(path):
    return [t for t in yaml.safe_load(path.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _task(path, fragment):
    return next(t for t in _tasks(path) if fragment in str(t.get("name")))


def _tpl(expr, variables):
    return Templar(loader=DataLoader(), variables=variables).template(trust_as_template(expr))


def _needs(manifest: dict) -> bool:
    t = _task(_PRE, _REQ)
    return bool(_tpl(t["ansible.builtin.set_fact"]["consumer_network_slice_required"], {"consumer_manifest": manifest}))


def test_non_vacuity_the_tasks_under_test_exist():
    _task(_PRE, _REQ)
    _task(_PRE, _ASSERT)
    _task(_PRE, _INFO)
    _task(_PROV, _SLICE)


@pytest.mark.parametrize("manifest,expected", [
    ({"slug": "p", "sso": {"org_name": "x"}}, False),                                    # the WS-C19 case
    ({"slug": "p", "sso": {"org_name": "x"}, "secrets": {"policy": "a"}}, False),          # secrets alone: no guest
    ({"slug": "p", "database": {"db_name": "d"}}, True),
    ({"slug": "p", "workload": {"enabled": True}}, True),
    ({"slug": "p", "workload": {"enabled": False}}, False),
    ({"slug": "p", "workload": {}}, False),
    ({"slug": "p", "database": {"db_name": "d"}, "sso": {"org_name": "x"}, "workload": {"enabled": True}}, True),
])
def test_a_slice_is_required_only_when_the_manifest_declares_a_guest(manifest, expected):
    assert _needs(manifest) is expected


def test_the_fail_closed_vnet_assert_applies_only_when_a_slice_is_required():
    """Bug condition: with auto-create OFF an SSO-only manifest failed closed demanding a VNet it does not need."""
    when = " ".join(str(w) for w in (_task(_PRE, _ASSERT)["when"] if isinstance(_task(_PRE, _ASSERT)["when"], list)
                                     else [_task(_PRE, _ASSERT)["when"]]))
    assert "consumer_network_slice_required" in when and "consumer_autocreate_network_slice" in when


def _assert_runs(manifest, autocreate):
    t = _task(_PRE, _ASSERT)
    conds = t["when"] if isinstance(t["when"], list) else [t["when"]]
    variables = {"consumer_manifest": manifest, "consumer_autocreate_network_slice": autocreate,
                 "consumer_network_slice_required": _needs(manifest)}
    return all(bool(_tpl("{{ " + str(c) + " }}", variables)) for c in conds)


def test_assert_matrix_sso_only_never_fails_for_a_missing_vnet_database_still_does():
    sso_only = {"slug": "p", "sso": {"org_name": "x"}}
    db = {"slug": "p", "database": {"db_name": "d"}}
    assert _assert_runs(sso_only, autocreate=False) is False
    assert _assert_runs(sso_only, autocreate=True) is False
    assert _assert_runs(db, autocreate=False) is True, "a guest-bearing manifest keeps the fail-closed prerequisite"
    assert _assert_runs(db, autocreate=True) is False, "auto-create ON is not an assert case (unchanged)"


def test_phase2_auto_create_runs_only_when_a_slice_is_required():
    conds = _task(_PROV, _SLICE)["when"]
    conds = conds if isinstance(conds, list) else [conds]
    joined = " ".join(str(c) for c in conds)
    assert "consumer_network_slice_required" in joined
    base = {"consumer_autocreate_network_slice": True, "consumer_vnet_present": False}

    def runs(required):
        v = {**base, "consumer_network_slice_required": required}
        return all(bool(_tpl("{{ " + str(c) + " }}", v)) for c in conds)

    assert runs(True) is True
    assert runs(False) is False


def test_phase2_default_when_the_fact_is_missing_is_to_require_the_slice():
    """Fail-safe: if the preflight fact were ever absent the old (create the slice) behavior applies."""
    conds = _task(_PROV, _SLICE)["when"]
    conds = conds if isinstance(conds, list) else [conds]
    v = {"consumer_autocreate_network_slice": True, "consumer_vnet_present": False}
    assert all(bool(_tpl("{{ " + str(c) + " }}", v)) for c in conds)


def test_the_decision_is_reported_and_the_registry_slug_resolution_is_untouched():
    msg = str(_task(_PRE, _INFO)["ansible.builtin.debug"]["msg"])
    assert "consumer_network_slice_required" in msg
    text = _PRE.read_text(encoding="utf-8")
    assert "consumer_registry_resolve" in text and "Resolve the project's vlan_id from the Registry" in text


def test_the_info_line_says_why_no_slice_is_needed():
    t = _task(_PRE, _INFO)["ansible.builtin.debug"]["msg"]
    v = {"project_slug": "edge-sensors", "consumer_vlan_id": 102, "consumer_vnet_present": False,
         "consumer_autocreate_network_slice": True, "consumer_network_slice_required": False}
    out = " ".join(str(_tpl(t, v)).split())
    assert "not needed" in out and "no database or workload" in out
