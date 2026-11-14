"""Bug-condition + preservation tests: consumer onboarding converges the host gateway for its VLAN (WS-C44).

THE BUG (found live on throwaway `shrimp`, WS-C38): on a from-scratch platform bring-up the gateway phase skips a
registry-active project VLAN whose network slice is not applied yet (WS-C26). The consumer orchestrator then
creates `p<vlan>` but nothing binds `10.0.<vlan>.1`, so the composed SVC-01 child cannot reach its guest
(ssh-keyscan fails) and Phase 2 halts. Re-running the platform gateway by hand fixed it.
THE FIX (operator decision WS-C44 option A): when the manifest needs a network slice, the consumer composes the
existing `platform_gateway` role right after the slice step, so the bring-up stays one command. The host address
comes from `consumer_proxmox_host`, else the cluster env file (`PROXMOX_HOST_LAN_IP`); missing values fail closed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_PROV = _REPO / "ansible/roles/consumer_provision/tasks/main.yml"
_GW = "Provisioning (gateway) | Converge the host L3 gateway for the project VLAN (platform_gateway)"
_ASSERT = "Provisioning (gateway) | Assert the Proxmox host address and admin CIDR are known (fail closed)"
_RESOLVE = "Provisioning (gateway) | Resolve the gateway inputs"


def _tasks():
    return [t for t in yaml.safe_load(_PROV.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _names():
    return [t.get("name", "") for t in _tasks()]


def _by(name):
    hits = [t for t in _tasks() if t.get("name") == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _tpl(expr, v):
    return Templar(loader=DataLoader(), variables=v).template(trust_as_template(expr))


def _when(t):
    w = t.get("when", [])
    return " ".join(str(x) for x in (w if isinstance(w, list) else [w]))


def test_BUG_the_gateway_is_composed_after_the_slice_and_before_any_guest():
    n = _names()
    slice_ = next(i for i, x in enumerate(n) if "Auto-create the project VNet" in x)
    db = next(i for i, x in enumerate(n) if "Compose svc-01-bootstrap.yml" in x)
    workload = next(i for i, x in enumerate(n) if "Stand up the optional workload guest" in x)
    assert slice_ < n.index(_RESOLVE) < n.index(_ASSERT) < n.index(_GW) < db < workload


def test_it_reuses_the_platform_gateway_role_not_a_copy():
    t = _by(_GW)
    assert t["ansible.builtin.include_role"]["name"] == "platform_gateway"
    v = t["vars"]
    assert v["platform_venv_python"] == "{{ consumer_venv_python }}"
    assert v["platform_repo_root"] == "{{ consumer_repo_root }}"
    assert v["platform_host_lan_ip"] == "{{ consumer_gateway_host }}"
    assert v["platform_admin_source_cidr"] == "{{ consumer_gateway_admin_cidr }}"


@pytest.mark.parametrize("name", [_RESOLVE, _ASSERT, _GW])
def test_only_when_a_slice_is_needed(name):
    assert "consumer_network_slice_required" in _when(_by(name))


def _resolve(proxmox_host="", tfvars=None):
    t = _by(_RESOLVE)["ansible.builtin.set_fact"]
    v = {"consumer_proxmox_host": proxmox_host, "consumer_tfvars": tfvars or {}}
    return _tpl(t["consumer_gateway_host"], v).strip(), _tpl(t["consumer_gateway_admin_cidr"], v).strip()


def test_host_prefers_the_explicit_var_then_the_cluster_env_file():
    tf = {"TF_VAR_proxmox_host_lan_ip": "192.0.2.5", "TF_VAR_sdn_gateway_admin_source_cidr": "192.0.2.0/24"}
    assert _resolve("198.51.100.7", tf) == ("198.51.100.7", "192.0.2.0/24")
    assert _resolve("", tf) == ("192.0.2.5", "192.0.2.0/24")
    assert _resolve("", {}) == ("", "")


def test_missing_inputs_fail_closed_naming_both_remedies():
    a = _by(_ASSERT)["ansible.builtin.assert"]
    that = " ".join(a["that"])
    assert "consumer_gateway_host" in that and "consumer_gateway_admin_cidr" in that
    for word in ("PROXMOX_HOST_LAN_IP", "ADMIN_SOURCE_CIDR", "consumer_proxmox_host", "Nothing"):
        assert word in a["fail_msg"]


def test_preservation_the_svc01_composition_is_unchanged():
    t = next(t for t in _tasks() if "Compose svc-01-bootstrap.yml" in t.get("name", ""))
    argv = [str(a) for a in t["ansible.builtin.command"]["argv"]]
    assert "{{ consumer_svc01_bootstrap_playbook }}" in argv
