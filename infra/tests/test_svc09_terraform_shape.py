"""Offline Terraform validation + resource-shape assertions for the SVC-09 root.

Slice-1 Task 2 (spec: svc-09-reverse-proxy) — requirements.md §13 acceptance
("Traefik LXC provisioned via Terraform with a computed static IP on VLAN 20")
and design.md §3.1 ("Terraform root — clone of SVC-07, single LXC").

Config-validation / contract-style tests for the SVC-09 IaC layer, structured
exactly like the sibling ``test_svc07_terraform_shape.py`` and reusing the
suite's terraform gate (``requires_terraform`` / ``_terraform_binary`` from
``infra/tests/conftest.py``).

Two layers:

1. **Static HCL/source inspection (ALWAYS runs, no binary needed).** Asserts the
   SVC-09 root declares EXACTLY ONE ``proxmox_virtual_environment_container`` and
   no other container type; the Traefik guest's sizes (1 vCPU / 512 MB / 4 GB)
   match the requirements; the single ``proxmox-compute`` module invocation
   carries ``svc_code=svc09`` / ``vlan_id=20`` / ``host_index=2`` (sourced from
   the committed host-index ledger via HCL locals) with no
   ``component`` (so the computed hostname is ``svc09-20-01`` and the computed IP
   is ``10.0.20.12``); the NIC attaches to ``p20`` with NO NIC-level ``vlan_id``;
   ``prevent_destroy`` is set (acme.json seatbelt); the generic ``inventory_hosts``
   output is present; and NO operator IP literal / SSH-key literal is written.

2. **terraform fmt/validate + plan-json (GATED on the toolchain).**
   ``fmt -check`` + offline ``validate`` on the real root, and the same shape
   assertions against ``terraform show -json`` of an offline plan (sandbox copy,
   transient ``backend "local" {}`` override, dummy non-contacting vars) — proving
   the module-derived IP/hostname/tag resolve to exactly 10.0.20.12 /
   svc09-20-01 / proj-shared-traefik. Skipped when no terraform/tofu binary.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc09_terraform_shape.py -v
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
_SVC09_ROOT = _INFRA_DIR / "projects" / "svc-09-reverse-proxy"

_CONTAINER_TYPE = "proxmox_virtual_environment_container"

#: Expected shape from the SVC-09 requirements (single Traefik LXC).
_EXPECTED = {
    "traefik": {
        "hostname": "svc09-20-01",
        "ipv4": "10.0.20.12",
        "tag": "proj-shared-traefik",
        "cores": 1,
        "memory": 512,
        "disk": 4,
        "host_index": 2,
        "component": None,
    },
}

_RESOURCE_HEADER_RE = re.compile(
    r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{',
    re.MULTILINE,
)


def _strip_hcl_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out = []
    for line in text.splitlines():
        out.append(line.split("#", 1)[0].split("//", 1)[0])
    return "\n".join(out)


def _read_root_hcl(root_dir: Path) -> str:
    assert root_dir.is_dir(), f"expected Terraform root dir at {root_dir}"
    tf_files = sorted(root_dir.glob("*.tf"))
    assert tf_files, f"no .tf files found in {root_dir}"
    return "\n".join(_strip_hcl_comments(p.read_text()) for p in tf_files)


def _count_resources(hcl: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rtype, _rname in _RESOURCE_HEADER_RE.findall(hcl):
        counts[rtype] = counts.get(rtype, 0) + 1
    return counts


def _extract_named_block(hcl: str, block_kw: str, label1: str, label2: str | None = None) -> str | None:
    if label2 is None:
        pattern = rf'{block_kw}\s+"{re.escape(label1)}"\s*\{{'
    else:
        pattern = rf'{block_kw}\s+"{re.escape(label1)}"\s+"{re.escape(label2)}"\s*\{{'
    header = re.search(pattern, hcl)
    if header is None:
        return None
    start = header.end() - 1
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    return None


def _slice_subblock(block: str, sub_kw: str) -> str | None:
    header = re.search(rf"^\s*{re.escape(sub_kw)}\s*\{{", block, re.MULTILINE)
    if header is None:
        return None
    start = header.end() - 1
    depth = 0
    for i in range(start, len(block)):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                return block[start : i + 1]
    return None


# --------------------------------------------------------------------------- #
# Static HCL inspection (primary — ALWAYS runs).
# --------------------------------------------------------------------------- #
class TestSvc09RootShapeStatic:
    """SVC-09 root: exactly one Traefik LXC with the requirements shape."""

    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_SVC09_ROOT)

    @staticmethod
    @pytest.fixture(scope="class")
    def counts(hcl) -> dict[str, int]:
        return _count_resources(hcl)

    def test_exactly_one_container_resource(self, counts):
        assert counts.get(_CONTAINER_TYPE, 0) == 1, (
            "SVC-09 root must declare EXACTLY one "
            f"{_CONTAINER_TYPE} resource (the Traefik LXC); "
            f"found {counts.get(_CONTAINER_TYPE, 0)}"
        )

    def test_no_other_container_resource_type(self, counts):
        other = [t for t in counts if "container" in t.lower() and t != _CONTAINER_TYPE]
        assert not other, (
            f"SVC-09 root must declare NO container resource other than {_CONTAINER_TYPE}; found {other}"
        )

    def test_named_container_present(self, hcl):
        assert _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik") is not None, (
            f'expected a {_CONTAINER_TYPE} "traefik" resource block'
        )

    def test_container_sizes_match_requirement(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        exp = _EXPECTED["traefik"]
        cores = re.search(r"^\s*cores\s*=\s*(\d+)", block, re.MULTILINE)
        assert cores and int(cores.group(1)) == exp["cores"], "traefik must declare cores = 1 (§7)"
        mem = re.search(r"^\s*dedicated\s*=\s*(\d+)", block, re.MULTILINE)
        assert mem and int(mem.group(1)) == exp["memory"], "traefik must declare memory.dedicated = 512 (§7)"
        disk = re.search(r"^\s*size\s*=\s*(\d+)", block, re.MULTILINE)
        assert disk and int(disk.group(1)) == exp["disk"], "traefik must declare disk.size = 4 (§7)"

    def test_container_is_unprivileged(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        assert re.search(r"^\s*unprivileged\s*=\s*true", block, re.MULTILINE), (
            "traefik must be an unprivileged container (PF LXC-default)"
        )

    def test_per_container_swap_cap(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        mem = _slice_subblock(block, "memory")
        assert mem is not None, "traefik must declare a memory block"
        assert re.search(r"^\s*swap\s*=\s*0\b", mem, re.MULTILINE), (
            "traefik memory block must set `swap = 0` — the provider-native per-container swap cap (ADR-0006)"
        )

    def test_container_nic_on_p20_without_nic_vlan_tag(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, "traefik must declare a network_interface"
        assert re.search(r'^\s*bridge\s*=\s*"p20"', nic, re.MULTILINE), (
            'traefik NIC must attach to the VLAN-20 shared-services SDN VNet bridge = "p20"'
        )
        assert not re.search(r"^\s*vlan_id\s*=", nic, re.MULTILINE), (
            'traefik NIC must NOT declare a NIC-level vlan_id: p20 is a VLAN-zone SDN VNet that '
            'applies tag 20 itself (double-tag => "vm vlans are not allowed on vnet p20")'
        )

    def test_container_prevent_destroy_seatbelt(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        life = _slice_subblock(block, "lifecycle")
        assert life is not None, "traefik must declare a lifecycle block"
        assert re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
            "traefik lifecycle must set prevent_destroy = true — the acme.json cert-store seatbelt"
        )

    def test_module_invocation_derives_computed_addressing(self, hcl):
        mod = _extract_named_block(hcl, "module", "traefik")
        assert mod is not None, 'expected a module "traefik" invocation'
        exp = _EXPECTED["traefik"]
        assert re.search(r'^\s*source\s*=\s*"\.\./\.\./modules/proxmox-compute"', mod, re.MULTILINE), (
            "module traefik must consume the shared proxmox-compute module"
        )
        assert re.search(r'^\s*svc_code\s*=\s*"svc09"', mod, re.MULTILINE), "module must set svc_code = svc09"
        assert re.search(r"^\s*vlan_id\s*=\s*(20\b|local\.\w+)", mod, re.MULTILINE), (
            "module must set vlan_id to VLAN 20 — either the literal 20 or a local. "
            "reference sourced from the host-index ledger (NET-00 §3)"
        )
        assert re.search(r"^\s*host_index\s*=\s*(2\b|local\.\w+)", mod, re.MULTILINE), (
            "module must set host_index = 2 (the moved index) so the computed IP is "
            "10.0.20.12 — either the literal 2 or a local. reference from the ledger (NET-00 §3)"
        )
        assert not re.search(r"^\s*component\s*=", mod, re.MULTILINE), (
            "module traefik must NOT set a component (plain hostname svc09-20-01)"
        )

    def test_hostname_and_ip_come_from_module_not_literal(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "traefik")
        assert block is not None
        assert re.search(r"^\s*hostname\s*=\s*module\.traefik\.hostname", block, re.MULTILINE), (
            "traefik hostname must come from module.traefik.hostname"
        )
        assert re.search(r"address\s*=\s*module\.traefik\.ip_config\.ipv4\.address", block), (
            "traefik IP must come from module.traefik.ip_config (computed, not a literal)"
        )
        assert re.search(r"^\s*tags\s*=\s*module\.traefik\.tags", block, re.MULTILINE), (
            "traefik tags must come from module.traefik.tags"
        )
        assert not re.search(r'address\s*=\s*"10\.0\.20\.\d+', block), (
            "traefik must NOT hardcode an IP literal — the address is module-computed (NET-00 §3)"
        )

    def test_operator_ssh_public_keys_variable_declared(self, hcl):
        block = _extract_named_block(hcl, "variable", "operator_ssh_public_keys")
        assert block is not None, 'SVC-09 root must declare variable "operator_ssh_public_keys"'
        assert re.search(r"^\s*type\s*=\s*list\(string\)", block, re.MULTILINE), (
            "operator_ssh_public_keys must be typed list(string)"
        )
        assert re.search(r"^\s*default\s*=\s*\[\s*\]", block, re.MULTILINE), (
            "operator_ssh_public_keys must default to [] (SSH-unreachable but non-breaking)"
        )

    def test_module_invocation_threads_ssh_public_keys(self, hcl):
        mod = _extract_named_block(hcl, "module", "traefik")
        assert mod is not None
        assert re.search(r"^\s*ssh_public_keys\s*=\s*var\.operator_ssh_public_keys", mod, re.MULTILINE), (
            "module traefik must pass ssh_public_keys = var.operator_ssh_public_keys"
        )

    def test_inventory_hosts_output_declared(self, hcl):
        block = _extract_named_block(hcl, "output", "inventory_hosts")
        assert block is not None, 'SVC-09 root must declare an output "inventory_hosts" (the generator surface)'
        assert re.search(r"module\.traefik", block), "inventory_hosts must reference module.traefik"
        for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
            assert re.search(rf"^\s*{field}\s*=", block, re.MULTILINE), (
                f"inventory_hosts host objects must carry `{field}`"
            )
        assert re.search(r"vmid\s*=\s*proxmox_virtual_environment_container\.traefik\.vm_id", block), (
            "inventory_hosts vmid must come from the container resource's vm_id"
        )
        assert re.search(r"node_name\s*=\s*proxmox_virtual_environment_container\.traefik\.node_name", block), (
            "inventory_hosts node_name must come from the container resource"
        )

    def test_traefik_internal_ip_output_declared(self, hcl):
        block = _extract_named_block(hcl, "output", "traefik_internal_ip")
        assert block is not None, 'SVC-09 root must declare output "traefik_internal_ip"'
        assert re.search(r"module\.traefik\.ipv4_address", block), (
            "traefik_internal_ip must come from module.traefik.ipv4_address (computed, not a literal)"
        )

    def test_no_ssh_key_literal_in_any_tracked_tf(self):
        tf_files = sorted(_SVC09_ROOT.glob("*.tf"))
        assert tf_files, f"no .tf files found in {_SVC09_ROOT}"
        raw = "\n".join(p.read_text() for p in tf_files)
        for pattern, label in (
            (r"ssh-ed25519\s+AAAA", "ssh-ed25519 public key literal"),
            (r"ssh-rsa\s+AAAA", "ssh-rsa public key literal"),
            (r"ecdsa-sha2-\S+\s+AAAA", "ecdsa public key literal"),
            (r"-----BEGIN[^\n]*PRIVATE KEY-----", "PEM private-key header"),
        ):
            assert not re.search(pattern, raw), (
                f"a {label} must NOT appear in any tracked SVC-09 .tf file — keys come from "
                f"TF_VAR_operator_ssh_public_keys / gitignored .env, never committed"
            )

    def test_state_name_in_versions_is_svc09(self):
        versions = (_SVC09_ROOT / "versions.tf").read_text()
        assert "svc-09-reverse-proxy" in versions, (
            "versions.tf must document the GitLab-managed state name svc-09-reverse-proxy (state isolation)"
        )
        assert re.search(r'backend\s+"http"\s*\{', versions), (
            "versions.tf must declare the partial backend \"http\" {} (GitLab-managed state, PF FR-2)"
        )


# --------------------------------------------------------------------------- #
# Offline-plan sandbox helper for the SVC-09 root.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_DUMMY_GUEST_VARS = (
    "-var", "proxmox_node_name=pve-test",
    "-var", "traefik_template_file_id=local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
    "-var", "traefik_vmid=2090",
)
_OVERRIDE_FILENAME = "zz_svc09_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written into a SANDBOX\n"
    "# COPY only by test_svc09_terraform_shape.py. Overrides the root's partial\n"
    "# `backend \"http\" {}` so an offline `terraform init -reconfigure` +\n"
    "# `plan` succeed without a live GitLab HTTP backend / CI_JOB_TOKEN. NEVER\n"
    "# committed into a real root (PF FR-2).\n"
    "terraform {\n"
    "  backend \"local\" {}\n"
    "}\n"
)


@contextmanager
def _svc09_offline_plan() -> Iterator[list[dict]]:
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"
    sandbox = Path(tempfile.mkdtemp(prefix="svc09_offline_plan_"))
    try:
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-09-reverse-proxy"
        for stale in (target / ".terraform",):
            if stale.is_dir():
                shutil.rmtree(stale)
        for stale_file in (target / "terraform.tfstate",):
            if stale_file.is_file():
                stale_file.unlink()
        (target / _OVERRIDE_FILENAME).write_text(_OVERRIDE_CONTENT, encoding="utf-8")
        init = subprocess.run(
            [binary, "init", "-reconfigure", "-input=false", "-no-color"],
            cwd=target, capture_output=True, text=True,
        )
        if init.returncode != 0:
            combined = (init.stdout + init.stderr).lower()
            env_markers = (
                "failed to install provider",
                "could not retrieve the list of available versions",
                "failed to query available provider packages",
                "could not connect", "network is unreachable", "dial tcp",
            )
            if any(m in combined for m in env_markers):
                pytest.skip(
                    "requires-infra, skipped: `terraform init` could not install/reuse "
                    "the provider offline — an environmental prerequisite unrelated to the "
                    f"shape under test.\ninit stderr tail:\n{init.stderr[-1200:]}"
                )
            pytest.fail(f"terraform init failed unexpectedly:\n{init.stdout}\n{init.stderr}")
        plan_path = target / "svc09.tfplan"
        plan = subprocess.run(
            [
                binary, "plan", "-input=false", "-no-color", "-refresh=false",
                "-var", f"proxmox_endpoint={_DUMMY_ENDPOINT}",
                "-var", f"proxmox_api_token={_DUMMY_TOKEN}",
                *_DUMMY_GUEST_VARS,
                f"-out={plan_path}",
            ],
            cwd=target, capture_output=True, text=True,
        )
        assert plan.returncode == 0, (
            "offline `terraform plan` of the SVC-09 root must succeed with dummy vars; "
            f"got exit {plan.returncode}:\n{plan.stdout}\n{plan.stderr}"
        )
        show = subprocess.run(
            [binary, "show", "-json", plan_path.name],
            cwd=target, capture_output=True, text=True,
        )
        assert show.returncode == 0 and show.stdout.strip(), (
            f"`terraform show -json` failed:\n{show.stdout}\n{show.stderr}"
        )
        data = json.loads(show.stdout)
        yield data.get("resource_changes", []) or []
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _first(seq):
    if not seq:
        return {} if seq == [] else None
    if isinstance(seq, list):
        return seq[0] if seq else {}
    return seq


# --------------------------------------------------------------------------- #
# terraform fmt/validate + plan-json (secondary — gated).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestSvc09TerraformFmtValidate:
    def test_fmt_check_clean(self):
        binary = _terraform_binary()
        proc = subprocess.run(
            [binary, "fmt", "-check", "-recursive", "-no-color"],
            cwd=_SVC09_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "`terraform fmt -check` reported unformatted files in the SVC-09 root "
            f"(run `terraform fmt`):\n{proc.stdout}\n{proc.stderr}"
        )

    def test_init_backend_false_then_validate(self):
        binary = _terraform_binary()
        sandbox = Path(tempfile.mkdtemp(prefix="svc09_validate_"))
        try:
            shutil.copytree(_INFRA_DIR, sandbox / "infra")
            target = sandbox / "infra" / "projects" / "svc-09-reverse-proxy"
            stale = target / ".terraform"
            if stale.is_dir():
                shutil.rmtree(stale)
            init = subprocess.run(
                [binary, "init", "-backend=false", "-input=false", "-no-color"],
                cwd=target, capture_output=True, text=True,
            )
            if init.returncode != 0:
                combined = (init.stdout + init.stderr).lower()
                if any(m in combined for m in (
                    "failed to install provider", "could not connect",
                    "network is unreachable", "dial tcp",
                    "failed to query available provider packages",
                )):
                    pytest.skip(
                        "requires-infra, skipped: provider could not be installed/reused "
                        f"offline for `init -backend=false`.\n{init.stderr[-1000:]}"
                    )
                pytest.fail(f"terraform init -backend=false failed:\n{init.stdout}\n{init.stderr}")
            val = subprocess.run(
                [binary, "validate", "-no-color"],
                cwd=target, capture_output=True, text=True,
            )
            assert val.returncode == 0, (
                f"`terraform validate` failed on the SVC-09 root:\n{val.stdout}\n{val.stderr}"
            )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


@requires_terraform
class TestSvc09PlanJsonShape:
    @staticmethod
    @pytest.fixture(scope="class")
    def containers() -> dict[str, dict]:
        with _svc09_offline_plan() as changes:
            out: dict[str, dict] = {}
            for c in changes:
                if c["type"] == _CONTAINER_TYPE:
                    out[c["name"]] = c["change"]["after"]
            return out

    @staticmethod
    @pytest.fixture(scope="class")
    def all_types() -> list[str]:
        with _svc09_offline_plan() as changes:
            return [c["type"] for c in changes]

    def test_exactly_one_container_planned(self, all_types):
        assert all_types.count(_CONTAINER_TYPE) == 1, (
            f"plan must create exactly one {_CONTAINER_TYPE}; types planned: {sorted(set(all_types))}"
        )

    def test_container_present(self, containers):
        assert set(containers) == {"traefik"}, f"expected planned container traefik; got {sorted(containers)}"

    def test_computed_ip_hostname_tag(self, containers):
        after = containers["traefik"]
        exp = _EXPECTED["traefik"]
        init = _first(after.get("initialization"))
        assert init is not None, "traefik: no initialization block in plan"
        assert init.get("hostname") == exp["hostname"], (
            f"traefik hostname must be {exp['hostname']}; got {init.get('hostname')}"
        )
        ipv4 = _first(_first(init.get("ip_config")).get("ipv4") if init.get("ip_config") else None)
        assert ipv4 is not None, "traefik: no ip_config.ipv4 in plan"
        assert ipv4.get("address") == f"{exp['ipv4']}/24", (
            f"traefik computed IP must be {exp['ipv4']}/24 (NET-00 §3); got {ipv4.get('address')}"
        )
        assert after.get("tags") == [exp["tag"]], (
            f"traefik tag must be [{exp['tag']!r}]; got {after.get('tags')}"
        )

    def test_computed_sizes(self, containers):
        after = containers["traefik"]
        exp = _EXPECTED["traefik"]
        cpu = _first(after.get("cpu"))
        mem = _first(after.get("memory"))
        disk = _first(after.get("disk"))
        assert cpu and cpu.get("cores") == exp["cores"], f"traefik cores must be {exp['cores']}; got {cpu}"
        assert mem and mem.get("dedicated") == exp["memory"], (
            f"traefik memory.dedicated must be {exp['memory']}; got {mem}"
        )
        assert disk and disk.get("size") == exp["disk"], f"traefik disk.size must be {exp['disk']}; got {disk}"

    def test_unprivileged(self, containers):
        after = containers["traefik"]
        assert after.get("unprivileged") is True, (
            f"traefik must be unprivileged; got {after.get('unprivileged')}"
        )
