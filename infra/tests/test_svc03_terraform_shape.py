"""SVC-03 Garage Terraform root: static shape + offline ``terraform plan`` (spec svc-03-object-storage).

Two layers, like the mailcatcher / SVC-17-18 siblings:

* STATIC (always runs): the root exists with the bpg/proxmox provider only and a
  partial ``backend "http"``; three guests on catalog slot ``svc03`` read the ledger
  with key-safe ``lookup()``; each NIC attaches to ``p20`` without a VLAN tag; the
  guests are unprivileged, carry ``prevent_destroy`` (stateful object data) and a
  DEDICATED data volume mounted at ``/var/lib/garage`` (requirements R3).
* PLAN (gated on a terraform binary + installable provider): the computed hostnames,
  IPs, tags and VMIDs equal the Python derivation of the NET-00 / ADR-0022 formulas.
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
_ROOT = _INFRA_DIR / "projects" / "svc-03-object-storage"
_LEDGER = _INFRA_DIR / "platform-foundation" / "host-indices.yaml"
_CONTAINER_TYPE = "proxmox_virtual_environment_container"
_VLAN = 20
_NODES = {1: 12, 2: 13, 3: 14}  # instance -> host_index
_SUBNET = ipaddress.ip_network("10.0.20.0/24")


def _read(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _container_blocks(main: str) -> dict[str, str]:
    blocks = {}
    for m in re.finditer(r'resource "proxmox_virtual_environment_container" "(node\d)" \{', main):
        start = m.end()
        depth, i = 1, start
        while depth:
            depth += {"{": 1, "}": -1}.get(main[i], 0)
            i += 1
        blocks[m.group(1)] = main[start:i]
    return blocks


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
    assert re.search(r'backend\s+"http"\s*\{\s*\}', v)


def test_three_guests_on_catalog_slot_svc03():
    m = _code_only(_read("main.tf"))
    assert len(re.findall(r'^\s*svc_code\s*=\s*"svc03"', m, re.M)) == 3
    assert not re.search(r"^\s*role_code\s*=", m, re.M)
    for n in _NODES:
        assert re.search(rf'^\s*instance\s*=\s*"0{n}"', m, re.M)
    assert set(_container_blocks(m)) == {"node1", "node2", "node3"}


def test_ledger_reads_are_key_safe():
    m = _code_only(_read("main.tf"))
    assert 'lookup(e, "svc_code", null) == "svc03"' in m
    assert "e.svc_code" not in m and "e.instance" not in m


def test_every_container_is_unprivileged_on_p20_untagged_with_a_data_volume_and_seatbelt():
    for name, body in _container_blocks(_code_only(_read("main.tf"))).items():
        assert "unprivileged = true" in body, name
        nic = re.search(r"network_interface\s*\{(.*?)\}", body, re.S).group(1)
        assert 'bridge = "p20"' in nic and "vlan_id" not in nic, name
        mp = re.search(r"mount_point\s*\{(.*?)\n  \}", body, re.S)
        assert mp, f"{name}: Garage data must live on a dedicated volume (R3)"
        assert 'path   = "/var/lib/garage"' in mp.group(1)
        assert "var.garage_data_disk_gb" in mp.group(1)
        assert "prevent_destroy = true" in body, f"{name}: stateful node needs the destroy seatbelt"
        assert "swap      = 0" in body


def test_ledger_rows_match_the_root_literals_and_are_anchors():
    ledger = yaml.safe_load(_LEDGER.read_text(encoding="utf-8"))
    rows = {e["instance"]: e for e in ledger[_VLAN] if e.get("svc_code") == "svc03"}
    assert set(rows) == set(_NODES)
    main = _code_only(_read("main.tf"))
    for inst, idx in _NODES.items():
        assert rows[inst]["host_index"] == idx and rows[inst]["anchor"] is True
        assert rows[inst]["status"] == "allocated" and "component" not in rows[inst]
        assert f"node{inst}_host_index = {idx}" in main


def test_vlan20_ledger_host_indices_are_unique():
    ledger = yaml.safe_load(_LEDGER.read_text(encoding="utf-8"))
    idx = [e["host_index"] for e in ledger[_VLAN]]
    assert len(idx) == len(set(idx))


def test_inventory_hosts_output_carries_the_contract_fields_for_three_hosts():
    o = _code_only(_read("outputs.tf"))
    for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
        assert len(re.findall(rf"^\s*{field}\s*=", o, re.M)) == 3, field


# --------------------------------------------------------------------------- #
# PLAN layer (gated)
# --------------------------------------------------------------------------- #

_DUMMY_TEMPLATE = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
_OVERRIDE_NAME = "zz_svc03_offline_backend_override.tf"


@contextmanager
def _offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="svc03_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-03-object-storage"
        shutil.rmtree(target / ".terraform", ignore_errors=True)
        # The live root may hold gitignored local state and overrides; plan from an empty state.
        for stale in [*target.glob("*_override.tf"), *target.glob("terraform.tfstate*")]:
            stale.unlink()
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
        plan_file = target / "svc03.tfplan"
        plan = subprocess.run(
            [binary, "plan", "-input=false", "-no-color", "-refresh=false",
             "-var", "proxmox_endpoint=https://dummy.invalid:8006/",
             "-var", "proxmox_api_token=terraform@pve!ci=00000000-0000-0000-0000-000000000000",
             "-var", "proxmox_node_name=pve-test",
             "-var", f"garage_template_file_id={_DUMMY_TEMPLATE}",
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

    def test_exactly_three_containers(self, changes):
        assert [c["type"] for c in changes].count(_CONTAINER_TYPE) == 3

    def test_computed_identity_matches_python_derivation(self, changes):
        by_name = {c["name"]: c["change"]["after"] for c in changes if c["type"] == _CONTAINER_TYPE}
        for inst, idx in _NODES.items():
            after = by_name[f"node{inst}"]
            init = _first(after["initialization"])
            assert init["hostname"] == f"svc03-{_VLAN}-0{inst}"
            assert _first(_first(init["ip_config"])["ipv4"])["address"] == f"{_SUBNET[10 + idx]}/24"
            assert after["tags"] == ["proj-shared-garage"]
            assert after["vm_id"] == _VLAN * 1000 + idx, "ADR-0022 deterministic VMID"
            nic = _first(after["network_interface"])
            assert nic["bridge"] == "p20" and not nic.get("vlan_id")
            mp = _first(after["mount_point"])
            assert mp["path"] == "/var/lib/garage" and mp["size"] == "20G"

    def test_ledger_assert_anchor_is_planned(self, changes):
        assert any(c["type"] == "terraform_data" and c["name"] == "ledger_index_assert" for c in changes)
