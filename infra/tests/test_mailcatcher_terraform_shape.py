"""Mail catcher Terraform root: static shape + offline ``terraform plan`` (spec dev-mail-catcher, ADR-0025).

Two layers, like the SVC-09 / SVC-17-18 siblings:

* STATIC (always runs, no terraform needed): the root exists with the
  bpg/proxmox provider only, a partial ``backend "http"``, the guest is identified
  by ``role_code`` (NOT ``svc_code``, ADR-0021), reads the ledger with key-safe
  ``lookup()`` (the VLAN-20 ledger now holds a role_code row), attaches to ``p20``
  without a NIC vlan tag, is unprivileged, and carries NO ``prevent_destroy``
  (stateless dev sink).
* PLAN (gated on a terraform binary + installable provider; skips cleanly
  otherwise): the computed hostname/IP/tag/VMID equal the Python derivation of the
  same formulas (the ADR-0015 / ADR-0022 HCL-vs-Python cross-check).
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from conftest import _terraform_binary, requires_terraform

_INFRA_DIR = Path(__file__).resolve().parent.parent
_ROOT = _INFRA_DIR / "projects" / "mailcatcher"
_LEDGER = _INFRA_DIR / "platform-foundation" / "host-indices.yaml"
_CONTAINER_TYPE = "proxmox_virtual_environment_container"

_VLAN = 20
_HOST_INDEX = 11
_SUBNET = ipaddress.ip_network("10.0.20.0/24")


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


# --------------------------------------------------------------------------- #
# STATIC layer
# --------------------------------------------------------------------------- #
def test_root_exists_with_expected_files():
    for f in ("main.tf", "variables.tf", "outputs.tf", "providers.tf", "versions.tf", ".terraform.lock.hcl"):
        assert (_ROOT / f).is_file(), f"missing {f}"


def test_only_the_bpg_proxmox_provider_and_a_partial_http_backend():
    v = _code_only(_read("versions.tf"))
    assert 'source  = "bpg/proxmox"' in v
    assert v.count("source") == 1, "tech.md: bpg/proxmox is the ONLY Proxmox provider"
    assert re.search(r'backend\s+"http"\s*\{\s*\}', v), "partial http backend (state name from -backend-config)"


def test_guest_identity_is_role_code_not_svc_code():
    m = _code_only(_read("main.tf"))
    assert 'role_code    = "mailcatcher"' in m
    assert not re.search(r"^\s*svc_code\s*=", m, re.M), "ADR-0021: exactly one of svc_code/role_code"


def test_ledger_reads_are_key_safe_for_a_role_code_row():
    m = _code_only(_read("main.tf"))
    assert 'lookup(e, "role_code", null) == "mailcatcher"' in m
    assert "e.svc_code" not in m, "a role_code ledger row has no svc_code key"


def test_every_other_vlan20_root_reads_svc_code_key_safely():
    """Bug condition: adding the role_code row to the VLAN-20 ledger must not break the other roots."""
    for root in ("svc-06-identity-provider", "svc-09-reverse-proxy", "svc-17-18-observability"):
        text = _code_only((_INFRA_DIR / "projects" / root / "main.tf").read_text(encoding="utf-8"))
        assert "e.svc_code" not in text, f"{root}/main.tf indexes e.svc_code directly (breaks on a role_code row)"


def test_nic_attaches_to_p20_without_a_vlan_tag_and_is_unprivileged():
    m = _code_only(_read("main.tf"))
    assert 'bridge = "p20"' in m
    nic = re.search(r"network_interface\s*\{(.*?)\}", m, re.S).group(1)
    assert "vlan_id" not in nic, "p20 is a VLAN-zone VNet; the NIC must not tag again"
    assert "unprivileged = true" in m


def test_stateless_guest_has_no_prevent_destroy():
    assert "prevent_destroy" not in _code_only(_read("main.tf"))


def test_ledger_row_matches_the_root_literal_and_is_non_anchor():
    ledger = yaml.safe_load(_LEDGER.read_text(encoding="utf-8"))
    rows = [e for e in ledger[_VLAN] if e.get("role_code") == "mailcatcher"]
    assert len(rows) == 1 and "svc_code" not in rows[0]
    assert rows[0]["host_index"] == _HOST_INDEX and not rows[0].get("anchor", False)
    assert rows[0]["status"] == "allocated"
    assert f"mailcatcher_host_index = {_HOST_INDEX}" in _code_only(_read("main.tf"))


def test_vlan20_ledger_host_indices_are_unique():
    ledger = yaml.safe_load(_LEDGER.read_text(encoding="utf-8"))
    idx = [e["host_index"] for e in ledger[_VLAN]]
    assert len(idx) == len(set(idx))


def test_inventory_hosts_output_carries_the_contract_fields():
    o = _code_only(_read("outputs.tf"))
    for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
        assert re.search(rf"^\s*{field}\s*=", o, re.M), field


# --------------------------------------------------------------------------- #
# PLAN layer (gated)
# --------------------------------------------------------------------------- #
_DUMMY_TEMPLATE = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
_OVERRIDE_NAME = "zz_mailcatcher_offline_backend_override.tf"


@contextmanager
def _offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="mailcatcher_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "mailcatcher"
        shutil.rmtree(target / ".terraform", ignore_errors=True)
        (target / _OVERRIDE_NAME).write_text('terraform {\n  backend "local" {}\n}\n', encoding="utf-8")
        init = subprocess.run([binary, "init", "-reconfigure", "-input=false", "-no-color"],
                              cwd=target, capture_output=True, text=True)
        if init.returncode != 0:
            blob = (init.stdout + init.stderr).lower()
            if any(m in blob for m in ("failed to install provider", "could not retrieve the list of available versions",
                                       "failed to query available provider packages", "could not connect",
                                       "network is unreachable", "dial tcp")):
                pytest.skip("requires-infra, skipped: terraform init could not install the provider offline")
            pytest.fail(f"terraform init failed unexpectedly:\n{init.stdout}\n{init.stderr}")
        plan_file = target / "mc.tfplan"
        plan = subprocess.run(
            [binary, "plan", "-input=false", "-no-color", "-refresh=false",
             "-var", "proxmox_endpoint=https://dummy.invalid:8006/",
             "-var", "proxmox_api_token=terraform@pve!ci=00000000-0000-0000-0000-000000000000",
             "-var", "proxmox_node_name=pve-test",
             "-var", f"mailcatcher_template_file_id={_DUMMY_TEMPLATE}",
             f"-out={plan_file}"],
            cwd=target, capture_output=True, text=True)
        assert plan.returncode == 0, f"offline plan must succeed:\n{plan.stdout}\n{plan.stderr}"
        show = subprocess.run([binary, "show", "-json", plan_file.name], cwd=target, capture_output=True, text=True)
        assert show.returncode == 0 and show.stdout.strip()
        yield json.loads(show.stdout).get("resource_changes", []) or []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _first(x):
    return (x[0] if x else {}) if isinstance(x, list) else x


@requires_terraform
class TestPlan:
    @staticmethod
    @pytest.fixture(scope="class")
    def changes():
        with _offline_plan() as c:
            return c

    def test_exactly_one_container(self, changes):
        assert [c["type"] for c in changes].count(_CONTAINER_TYPE) == 1

    def test_computed_hostname_ip_tag_and_vmid_match_python_derivation(self, changes):
        after = next(c for c in changes if c["type"] == _CONTAINER_TYPE)["change"]["after"]
        init = _first(after["initialization"])
        assert init["hostname"] == f"mailcatcher-{_VLAN}-01"
        assert _first(_first(init["ip_config"])["ipv4"])["address"] == f"{_SUBNET[10 + _HOST_INDEX]}/24"
        assert after["tags"] == ["proj-shared-mailcatcher"]
        assert after["vm_id"] == _VLAN * 1000 + _HOST_INDEX == 20011, "ADR-0022 deterministic VMID"
        assert after["unprivileged"] is True

    def test_nic_is_on_p20_with_no_vlan_tag(self, changes):
        after = next(c for c in changes if c["type"] == _CONTAINER_TYPE)["change"]["after"]
        nic = _first(after["network_interface"])
        assert nic["bridge"] == "p20" and not nic.get("vlan_id")

    def test_ledger_assert_anchor_is_planned(self, changes):
        assert any(c["type"] == "terraform_data" and c["name"] == "ledger_index_assert" for c in changes)
