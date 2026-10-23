"""Offline Terraform validation + resource-shape assertions for the SVC-06 root.

Slice-3 Task 15 (spec: svc-06-identity-provider) — requirements.md §13 +
design.md §3.1. Config-validation / contract-style tests for the SVC-06 ZITADEL
Terraform root, structured like the sibling SVC-01/SVC-07/SVC-09 shape tests and
reusing the suite's terraform gate.

Two layers:
1. Static HCL inspection (ALWAYS runs): exactly ONE container (the ZITADEL LXC)
   and NO database container (the backing DB is a separate SVC-01 instance —
   design.md §3.3); module wiring svc06/shared/zitadel on VLAN 20; p20 NIC with
   NO NIC-level vlan_id; unprivileged + swap=0; prevent_destroy; computed
   hostname/IP/tags from the module (no IP literal); the mandatory inventory_hosts
   output; the zitadel_* service outputs.
2. terraform fmt/validate + plan-json (GATED): the computed ZITADEL guest is
   svc06-20-01 / 10.0.20.15 / proj-shared-zitadel; exactly one container is
   planned.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc06_terraform_shape.py -v
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
_SVC06_ROOT = _INFRA_DIR / "projects" / "svc-06-identity-provider"
_CONTAINER_TYPE = "proxmox_virtual_environment_container"

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


class TestSvc06RootShapeStatic:
    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_SVC06_ROOT)

    @staticmethod
    @pytest.fixture(scope="class")
    def counts(hcl) -> dict[str, int]:
        return _count_resources(hcl)

    def test_exactly_one_container_resource(self, counts):
        assert counts.get(_CONTAINER_TYPE, 0) == 1, (
            f"SVC-06 root must declare EXACTLY one {_CONTAINER_TYPE} (the ZITADEL LXC); "
            f"found {counts.get(_CONTAINER_TYPE, 0)}"
        )

    def test_named_container_present(self, hcl):
        assert _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "zitadel") is not None, (
            f'expected a {_CONTAINER_TYPE} "zitadel" resource'
        )

    def test_root_does_not_embed_a_database(self, hcl, counts):
        """The backing DB is a SEPARATE SVC-01 instance (design.md §3.3). The
        SVC-06 root must NOT embed a DB — i.e. the ONLY container resource is the
        ZITADEL one, and there is no `module "db"` / SVC-01-root consumption here.
        (A doc mention of 'postgresql-backed' in a description string is fine; we
        assert on resource/module STRUCTURE, not prose.)"""
        # The only container is the ZITADEL guest (asserted elsewhere); there must
        # be no second container that would be a DB.
        assert counts.get(_CONTAINER_TYPE, 0) == 1, (
            "SVC-06 root must declare exactly one container (ZITADEL); a second container would be an "
            "embedded DB — the backing DB belongs to a separate SVC-01 instance (design.md §3.3)"
        )
        # No DB module block, and no consumption of the SVC-01 root as a module.
        assert _extract_named_block(hcl, "module", "db") is None, (
            "SVC-06 root must NOT declare a module \"db\" — the backing DB is provisioned by a child "
            "svc-01-bootstrap run, not a module block in this root"
        )
        assert "svc-01-sql-database" not in hcl, (
            "SVC-06 root must NOT reference the SVC-01 root as a module source — the backing DB is a "
            "child-orchestrated SVC-01 instance (own state), not an in-root module (design.md §3.3)"
        )

    def test_container_is_unprivileged_and_swap_capped(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "zitadel")
        assert re.search(r"^\s*unprivileged\s*=\s*true", block, re.MULTILINE), "ZITADEL LXC must be unprivileged"
        mem = _slice_subblock(block, "memory")
        assert mem and re.search(r"^\s*swap\s*=\s*0\b", mem, re.MULTILINE), (
            "ZITADEL LXC memory block must set swap = 0 (ADR-0006 per-container swap cap)"
        )
        feat = _slice_subblock(block, "features")
        assert feat and re.search(r"^\s*nesting\s*=\s*true", feat, re.MULTILINE), (
            "ZITADEL LXC must enable nesting (in-container Docker)"
        )

    def test_container_prevent_destroy_seatbelt(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "zitadel")
        life = _slice_subblock(block, "lifecycle")
        assert life and re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
            "ZITADEL LXC must set lifecycle.prevent_destroy = true (masterkey-encrypted state seatbelt)"
        )

    def test_container_nic_on_p20_without_nic_vlan_tag(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "zitadel")
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, "ZITADEL LXC must declare a network_interface"
        assert re.search(r'^\s*bridge\s*=\s*"p20"', nic, re.MULTILINE), (
            "ZITADEL NIC must attach to the shared-services SDN VNet p20"
        )
        assert not re.search(r"^\s*vlan_id\s*=", nic, re.MULTILINE), (
            "ZITADEL NIC must NOT set a NIC-level vlan_id (VLAN-zone VNet p20 applies the tag)"
        )

    def test_module_invocation_svc06_shared_zitadel_vlan20(self, hcl):
        mod = _extract_named_block(hcl, "module", "zitadel")
        assert mod is not None, 'expected a module "zitadel" invocation'
        assert re.search(r'^\s*source\s*=\s*"\.\./\.\./modules/proxmox-compute"', mod, re.MULTILINE), (
            "module zitadel must consume the shared proxmox-compute module"
        )
        assert re.search(r'^\s*svc_code\s*=\s*"svc06"', mod, re.MULTILINE), "module must set svc_code = svc06"
        assert re.search(r'^\s*project_slug\s*=\s*"shared"', mod, re.MULTILINE), (
            "module must set project_slug = shared (shared platform service)"
        )
        assert re.search(r'^\s*service_name\s*=\s*"zitadel"', mod, re.MULTILINE), (
            "module service_name must be zitadel -> tag proj-shared-zitadel"
        )
        assert re.search(r"^\s*vlan_id\s*=\s*(20\b|local\.\w+)", mod, re.MULTILINE), (
            "module zitadel must be on VLAN 20 (shared-services tier, domain-rules §1) — "
            "either the literal 20 or a local. reference sourced from the host-index ledger"
        )

    def test_hostname_ip_tags_from_module_no_literal(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "zitadel")
        assert re.search(r"^\s*hostname\s*=\s*module\.zitadel\.hostname", block, re.MULTILINE)
        assert re.search(r"address\s*=\s*module\.zitadel\.ip_config\.ipv4\.address", block)
        assert re.search(r"^\s*tags\s*=\s*module\.zitadel\.tags", block, re.MULTILINE)
        assert not re.search(r'address\s*=\s*"10\.0\.\d+\.\d+', block), "no hardcoded IP literal"

    def test_inventory_hosts_output(self, hcl):
        block = _extract_named_block(hcl, "output", "inventory_hosts")
        assert block is not None, 'SVC-06 root must declare output "inventory_hosts"'
        for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
            assert re.search(rf"^\s*{field}\s*=", block, re.MULTILINE), f"inventory_hosts must carry {field}"
        assert re.search(r"vmid\s*=\s*proxmox_virtual_environment_container\.zitadel\.vm_id", block), (
            "inventory_hosts vmid must come from the CONCRETE container resource (not the module's null stand-in)"
        )

    def test_zitadel_outputs_present(self, hcl):
        for out in ("zitadel_host", "zitadel_http_port", "zitadel_grpc_port", "zitadel_issuer_url"):
            assert _extract_named_block(hcl, "output", out) is not None, f'SVC-06 root must declare output "{out}"'

    def test_no_ssh_key_literal(self):
        raw = "\n".join(p.read_text() for p in sorted(_SVC06_ROOT.glob("*.tf")))
        for pat in (r"ssh-ed25519\s+AAAA", r"ssh-rsa\s+AAAA", r"-----BEGIN[^\n]*PRIVATE KEY-----"):
            assert not re.search(pat, raw), "no SSH key / private-key literal may appear in SVC-06 .tf files"

    def test_state_name_documented_in_versions(self):
        versions = (_SVC06_ROOT / "versions.tf").read_text()
        assert "svc-06-identity-provider" in versions, (
            "versions.tf must document the state name svc-06-identity-provider"
        )
        assert 'backend "http" {' in versions, "SVC-06 root must use a partial backend \"http\" {} (PF FR-2)"


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_OVERRIDE = "zz_svc06_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient test-scoped local backend override (SANDBOX copy only).\n"
    'terraform {\n  backend "local" {}\n}\n'
)


@contextmanager
def _svc06_offline_plan(vmid: int = 1103) -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="svc06_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-06-identity-provider"
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
            "-var", "zitadel_template_file_id=local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
            "-var", f"zitadel_vmid={vmid}",
        ]
        plan_path = target / "svc06.tfplan"
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


@requires_terraform
class TestSvc06PlanJsonShape:
    def test_fmt_and_validate(self):
        binary = _terraform_binary()
        proc = subprocess.run([binary, "fmt", "-check", "-recursive", "-no-color"], cwd=_SVC06_ROOT,
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"terraform fmt -check failed:\n{proc.stdout}\n{proc.stderr}"

    def test_computed_zitadel_guest_addressing(self):
        with _svc06_offline_plan() as changes:
            containers = [c for c in changes if c.get("type") == _CONTAINER_TYPE]
            assert len(containers) == 1, f"expected exactly one ZITADEL container in the plan, got {len(containers)}"
            after = containers[0]["change"]["after"]
            # host_index default 5 -> 10.0.20.15; hostname svc06-20-01; tag proj-shared-zitadel.
            init = (after.get("initialization") or [{}])[0]
            ipv4 = (((init.get("ip_config") or [{}])[0].get("ipv4")) or [{}])[0]
            assert ipv4.get("address") == "10.0.20.15/24", (
                f"ZITADEL guest IP must be the computed 10.0.20.15/24, got {ipv4.get('address')!r}"
            )
            assert init.get("hostname") == "svc06-20-01", (
                f"ZITADEL hostname must be svc06-20-01, got {init.get('hostname')!r}"
            )
            assert "proj-shared-zitadel" in (after.get("tags") or []), (
                f"ZITADEL tag must include proj-shared-zitadel, got {after.get('tags')!r}"
            )
