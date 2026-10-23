"""Offline Terraform validation + resource-shape assertions for the SVC-01 root.

Slice-2 Task 9 (spec: svc-01-sql-database) — requirements.md §13 acceptance +
design.md §3.1. Config-validation / contract-style tests for the REUSABLE,
per-project SVC-01 Terraform root, structured like the sibling
`test_svc09_terraform_shape.py` and reusing the suite's terraform gate.

Two layers:
1. Static HCL inspection (ALWAYS runs): exactly one container; tier-sized; module
   wiring parameterized by var.project_slug/var.vlan_id (not hardcoded 20);
   db_engine switch; the optional firewall-rules resource is count-gated on
   enable_firewall_rules; prevent_destroy; generic inventory_hosts; no IP literal;
   no SSH-key literal.
2. terraform fmt/validate + plan-json (GATED): the same shapes against an offline
   plan parameterized over TWO different slugs/VLANs, proving the one reusable
   root produces distinct computed hostnames/IPs per project.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc01_terraform_shape.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from conftest import requires_terraform, _terraform_binary

_INFRA_DIR = Path(__file__).resolve().parent.parent
_SVC01_ROOT = _INFRA_DIR / "projects" / "svc-01-sql-database"
_CONTAINER_TYPE = "proxmox_virtual_environment_container"
_FIREWALL_TYPE = "proxmox_virtual_environment_firewall_rules"


_RESOURCE_HEADER_RE = re.compile(
    r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{', re.MULTILINE
)


def _strip_hcl_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(line.split("#", 1)[0].split("//", 1)[0] for line in text.splitlines())


def _read_root_hcl(root_dir: Path) -> str:
    assert root_dir.is_dir(), f"expected Terraform root dir at {root_dir}"
    tf_files = sorted(root_dir.glob("*.tf"))
    assert tf_files, f"no .tf files found in {root_dir}"
    return "\n".join(_strip_hcl_comments(p.read_text()) for p in tf_files)


def _count_resources(hcl: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rtype, _ in _RESOURCE_HEADER_RE.findall(hcl):
        counts[rtype] = counts.get(rtype, 0) + 1
    return counts


def _extract_named_block(hcl: str, kw: str, l1: str, l2: str | None = None) -> str | None:
    pat = rf'{kw}\s+"{re.escape(l1)}"\s*\{{' if l2 is None else rf'{kw}\s+"{re.escape(l1)}"\s+"{re.escape(l2)}"\s*\{{'
    h = re.search(pat, hcl)
    if h is None:
        return None
    start = h.end() - 1
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start:i + 1]
    return None


def _slice_subblock(block: str, kw: str) -> str | None:
    h = re.search(rf"^\s*{re.escape(kw)}\s*\{{", block, re.MULTILINE)
    if h is None:
        return None
    start = h.end() - 1
    depth = 0
    for i in range(start, len(block)):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                return block[start:i + 1]
    return None


class TestSvc01RootShapeStatic:
    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_SVC01_ROOT)

    @staticmethod
    @pytest.fixture(scope="class")
    def counts(hcl) -> dict[str, int]:
        return _count_resources(hcl)

    def test_exactly_one_container_resource(self, counts):
        assert counts.get(_CONTAINER_TYPE, 0) == 1, (
            f"SVC-01 root must declare EXACTLY one {_CONTAINER_TYPE} (the DB LXC); "
            f"found {counts.get(_CONTAINER_TYPE, 0)}"
        )

    def test_named_container_present(self, hcl):
        assert _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "db") is not None, (
            f'expected a {_CONTAINER_TYPE} "db" resource'
        )

    def test_firewall_rules_resource_is_count_gated(self, hcl):
        block = _extract_named_block(hcl, "resource", _FIREWALL_TYPE, "db")
        assert block is not None, "SVC-01 root must declare the opt-in firewall-rules resource"
        assert re.search(r"^\s*count\s*=\s*var\.enable_firewall_rules\s*\?\s*1\s*:\s*0", block, re.MULTILINE), (
            "the firewall-rules resource must be count-gated on var.enable_firewall_rules "
            "(default-off, reversible per ADR-0008 posture)"
        )

    def test_enable_firewall_rules_defaults_false(self, hcl):
        v = _extract_named_block(hcl, "variable", "enable_firewall_rules")
        assert v is not None, 'SVC-01 root must declare variable "enable_firewall_rules"'
        assert re.search(r"^\s*default\s*=\s*false", v, re.MULTILINE), (
            "enable_firewall_rules must default to false (never flip guest firewalling on by merge)"
        )

    def test_container_is_unprivileged_and_swap_capped(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "db")
        assert re.search(r"^\s*unprivileged\s*=\s*true", block, re.MULTILINE), "DB LXC must be unprivileged"
        mem = _slice_subblock(block, "memory")
        assert mem and re.search(r"^\s*swap\s*=\s*0\b", mem, re.MULTILINE), (
            "DB LXC memory block must set swap = 0 (ADR-0006 per-container swap cap)"
        )

    def test_container_prevent_destroy_seatbelt(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "db")
        life = _slice_subblock(block, "lifecycle")
        assert life and re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
            "DB LXC must set lifecycle.prevent_destroy = true (stateful store seatbelt)"
        )

    def test_container_nic_on_project_vnet_without_nic_vlan_tag(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "db")
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, "DB LXC must declare a network_interface"
        assert re.search(r"^\s*bridge\s*=\s*local\.svc01_vnet_id", nic, re.MULTILINE), (
            "DB NIC must attach to the project's SDN VNet local.svc01_vnet_id (p<vlan>)"
        )
        assert not re.search(r"^\s*vlan_id\s*=", nic, re.MULTILINE), (
            "DB NIC must NOT set a NIC-level vlan_id (VLAN-zone VNet applies the tag)"
        )

    def test_module_invocation_is_parameterized_not_hardcoded_vlan(self, hcl):
        mod = _extract_named_block(hcl, "module", "db")
        assert mod is not None, 'expected a module "db" invocation'
        assert re.search(r'^\s*source\s*=\s*"\.\./\.\./modules/proxmox-compute"', mod, re.MULTILINE), (
            "module db must consume the shared proxmox-compute module"
        )
        assert re.search(r'^\s*svc_code\s*=\s*"svc01"', mod, re.MULTILINE), "module must set svc_code = svc01"
        # Parameterized — NOT a hardcoded vlan_id = 20.
        assert re.search(r"^\s*vlan_id\s*=\s*var\.vlan_id", mod, re.MULTILINE), (
            "module db must take vlan_id FROM var.vlan_id (reusable per-project), not a hardcoded value"
        )
        assert re.search(r"^\s*project_slug\s*=\s*var\.project_slug", mod, re.MULTILINE), (
            "module db must take project_slug from var.project_slug (reusable)"
        )
        assert re.search(r"^\s*service_name\s*=\s*var\.db_engine", mod, re.MULTILINE), (
            "module db service_name must track var.db_engine so the tag reflects the engine"
        )

    def test_db_engine_variable_switch(self, hcl):
        v = _extract_named_block(hcl, "variable", "db_engine")
        assert v is not None, 'SVC-01 root must declare variable "db_engine"'
        assert '"postgres"' in v and '"mariadb"' in v, "db_engine must allow postgres | mariadb"
        assert re.search(r'default\s*=\s*"postgres"', v), "db_engine must default to postgres"

    def test_hostname_ip_from_module_no_literal(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "db")
        assert re.search(r"^\s*hostname\s*=\s*module\.db\.hostname", block, re.MULTILINE)
        assert re.search(r"address\s*=\s*module\.db\.ip_config\.ipv4\.address", block)
        assert re.search(r"^\s*tags\s*=\s*module\.db\.tags", block, re.MULTILINE)
        assert not re.search(r'address\s*=\s*"10\.0\.\d+\.\d+', block), "no hardcoded IP literal"

    def test_inventory_hosts_output(self, hcl):
        block = _extract_named_block(hcl, "output", "inventory_hosts")
        assert block is not None, 'SVC-01 root must declare output "inventory_hosts"'
        for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
            assert re.search(rf"^\s*{field}\s*=", block, re.MULTILINE), f"inventory_hosts must carry {field}"
        assert re.search(r"vmid\s*=\s*proxmox_virtual_environment_container\.db\.vm_id", block)

    def test_db_outputs_present(self, hcl):
        for out in ("db_host", "db_port", "db_engine"):
            assert _extract_named_block(hcl, "output", out) is not None, f'SVC-01 root must declare output "{out}"'

    def test_no_ssh_key_literal(self):
        raw = "\n".join(p.read_text() for p in sorted(_SVC01_ROOT.glob("*.tf")))
        for pat in (r"ssh-ed25519\s+AAAA", r"ssh-rsa\s+AAAA", r"-----BEGIN[^\n]*PRIVATE KEY-----"):
            assert not re.search(pat, raw), "no SSH key / private-key literal may appear in SVC-01 .tf files"

    def test_state_name_documented_in_versions(self):
        versions = (_SVC01_ROOT / "versions.tf").read_text()
        assert "<slug>-svc01-postgres" in versions, (
            "versions.tf must document the per-instance state name <slug>-svc01-postgres (state isolation)"
        )


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper — parameterized over slug/vlan.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_OVERRIDE = "zz_svc01_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient test-scoped local backend override (SANDBOX copy only).\n"
    'terraform {\n  backend "local" {}\n}\n'
)


@contextmanager
def _svc01_offline_plan(slug: str, vlan: int, vmid: int, **extra_vars) -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="svc01_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-01-sql-database"
        for stale in (target / ".terraform",):
            if stale.is_dir():
                shutil.rmtree(stale)
        # Strip any copied LOCAL-backend state so a prior live run (which writes
        # terraform.tfstate here) can't contaminate this fresh offline plan — the
        # copied state carries prevent_destroy resources that make `plan` refuse.
        # Same isolation the SVC-07 shape fixture already applies.
        for stale_file in target.glob("terraform.tfstate*"):
            stale_file.unlink()
        (target / _OVERRIDE).write_text(_OVERRIDE_CONTENT, encoding="utf-8")
        init = subprocess.run(
            [binary, "init", "-reconfigure", "-input=false", "-no-color"],
            cwd=target, capture_output=True, text=True,
        )
        if init.returncode != 0:
            combined = (init.stdout + init.stderr).lower()
            if any(m in combined for m in ("failed to install provider", "could not connect",
                                           "network is unreachable", "dial tcp",
                                           "failed to query available provider packages")):
                pytest.skip(f"requires-infra, skipped: provider unavailable offline.\n{init.stderr[-800:]}")
            pytest.fail(f"terraform init failed:\n{init.stdout}\n{init.stderr}")
        var_args = [
            "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
            "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
            "-var", "proxmox_node_name=pve-test",
            "-var", "svc01_template_file_id=local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
            "-var", f"project_slug={slug}",
            "-var", f"vlan_id={vlan}",
            "-var", f"svc01_vmid={vmid}",
        ]
        for k, v in extra_vars.items():
            var_args += ["-var", f"{k}={v}"]
        plan_path = target / "svc01.tfplan"
        plan = subprocess.run(
            [binary, "plan", "-input=false", "-no-color", "-refresh=false", *var_args, f"-out={plan_path}"],
            cwd=target, capture_output=True, text=True,
        )
        assert plan.returncode == 0, f"offline plan failed:\n{plan.stdout}\n{plan.stderr}"
        show = subprocess.run([binary, "show", "-json", plan_path.name], cwd=target, capture_output=True, text=True)
        assert show.returncode == 0 and show.stdout.strip(), f"show -json failed:\n{show.stderr}"
        yield json.loads(show.stdout).get("resource_changes", []) or []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _first(seq):
    if not seq:
        return {} if seq == [] else None
    return seq[0] if isinstance(seq, list) and seq else (seq if not isinstance(seq, list) else {})


@requires_terraform
class TestSvc01PlanJsonShape:
    def test_fmt_and_validate(self):
        binary = _terraform_binary()
        proc = subprocess.run([binary, "fmt", "-check", "-recursive", "-no-color"], cwd=_SVC01_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"terraform fmt -check failed:\n{proc.stdout}\n{proc.stderr}"

    def test_two_projects_get_distinct_computed_addressing(self):
        """The ONE reusable root, planned for two different slugs/VLANs, produces
        distinct computed hostnames/IPs — proving per-project reuse."""
        with _svc01_offline_plan("dronefleet", 100, 2101) as changes_a:
            a = next(c["change"]["after"] for c in changes_a if c["type"] == _CONTAINER_TYPE)
        with _svc01_offline_plan("mlvideo", 101, 2102) as changes_b:
            b = next(c["change"]["after"] for c in changes_b if c["type"] == _CONTAINER_TYPE)
        init_a = _first(a.get("initialization"))
        init_b = _first(b.get("initialization"))
        assert init_a.get("hostname") == "svc01-100-01", f"got {init_a.get('hostname')}"
        assert init_b.get("hostname") == "svc01-101-01", f"got {init_b.get('hostname')}"
        ip_a = _first(_first(init_a.get("ip_config")).get("ipv4"))
        ip_b = _first(_first(init_b.get("ip_config")).get("ipv4"))
        assert ip_a.get("address") == "10.0.100.10/24", f"got {ip_a.get('address')}"
        assert ip_b.get("address") == "10.0.101.10/24", f"got {ip_b.get('address')}"

    def test_tier_sizing_small_default(self):
        with _svc01_offline_plan("dronefleet", 100, 2101) as changes:
            c = next(x["change"]["after"] for x in changes if x["type"] == _CONTAINER_TYPE)
        cpu = _first(c.get("cpu"))
        mem = _first(c.get("memory"))
        disk = _first(c.get("disk"))
        assert cpu.get("cores") == 1, f"small tier default cpu should be 1; got {cpu}"
        assert mem.get("dedicated") == 2048, f"small tier default mem should be 2048; got {mem}"
        assert disk.get("size") == 20, f"small tier default disk should be 20; got {disk}"

    def test_tier_sizing_medium(self):
        with _svc01_offline_plan("mlvideo", 101, 2102, tier="medium") as changes:
            c = next(x["change"]["after"] for x in changes if x["type"] == _CONTAINER_TYPE)
        cpu = _first(c.get("cpu"))
        mem = _first(c.get("memory"))
        assert cpu.get("cores") == 4, f"medium tier cpu should be 4; got {cpu}"
        assert mem.get("dedicated") == 8192, f"medium tier mem should be 8192; got {mem}"

    def test_firewall_rules_absent_by_default_present_when_enabled(self):
        with _svc01_offline_plan("dronefleet", 100, 2101) as changes:
            assert not any(c["type"] == _FIREWALL_TYPE for c in changes), (
                "firewall rules must NOT be planned by default (enable_firewall_rules=false)"
            )
        with _svc01_offline_plan("dronefleet", 100, 2101, enable_firewall_rules="true") as changes:
            assert any(c["type"] == _FIREWALL_TYPE for c in changes), (
                "firewall rules MUST be planned when enable_firewall_rules=true"
            )

    def test_mariadb_engine_switch_sets_tag(self):
        with _svc01_offline_plan("dronefleet", 100, 2101, db_engine="mariadb") as changes:
            c = next(x["change"]["after"] for x in changes if x["type"] == _CONTAINER_TYPE)
        assert c.get("tags") == ["proj-dronefleet-mariadb"], (
            f"db_engine=mariadb must tag proj-dronefleet-mariadb; got {c.get('tags')}"
        )
