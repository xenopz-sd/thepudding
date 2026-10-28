"""Offline Terraform validation + resource-shape assertions for the SVC-17/18 root.

Slice task 2.4 (spec: svc-17-18-observability) — requirements FR-16 / AC-2 / AC-11
("three observability LXCs provisioned via Terraform with computed static IPs on
VLAN 20") and design §3.1 ("Terraform root — SVC-07/SVC-09 shape, THREE guests").

Config-validation / contract-style tests for the SVC-17/18 IaC layer, structured
EXACTLY like the sibling ``test_svc09_terraform_shape.py`` (static HCL inspection
helpers ``_extract_named_block`` / ``_slice_subblock``, the ``requires_terraform``
gate from ``conftest``, the sandbox offline-plan pattern with a local backend
override + dummy vars, and the skip-on-provider-install-failure logic). Extended
from one Traefik LXC to THREE observability guests.

Two layers:

1. **Static HCL/source inspection (ALWAYS runs, no binary needed).** Asserts the
   SVC-17/18 root declares EXACTLY THREE
   ``proxmox_virtual_environment_container`` resources (prometheus, grafana,
   loki) and no other container type; each guest's sizes match requirements §6
   (prometheus 3 vCPU / 4608 MB / 100 GB; grafana 1 / 1024 / 10; loki 2 / 4096 /
   200); the three ``proxmox-compute`` module invocations carry
   ``svc_code=svc17`` (component ``prometheus``) / ``svc_code=svc17`` (component
   ``grafana``) / ``svc_code=svc18`` (component ``loki``), VLAN 20, host indices
   8 / 9 / 10, with ``peer_host_indices`` wired and NO IP literal anywhere; each
   NIC attaches to ``p20`` with NO NIC-level ``vlan_id`` and ``swap = 0``;
   ``prevent_destroy`` is set on Prometheus + Loki but NOT Grafana; the generic
   ``inventory_hosts`` output carries three entries; and the service-fact outputs
   (``grafana_url`` / ``loki_push_url`` / ``prometheus_internal_ip`` / etc.) are
   present. State name ``svc-17-18-observability`` + the partial
   ``backend "http" {}`` live in ``versions.tf``.

2. **terraform fmt/validate + plan-json (GATED on the toolchain).**
   ``fmt -check`` + offline ``validate`` on the real root. Skipped when no
   terraform/tofu binary or when the provider cannot be installed offline.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_terraform_shape.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from conftest import requires_terraform, _terraform_binary

_INFRA_DIR = Path(__file__).resolve().parent.parent
_OBS_ROOT = _INFRA_DIR / "projects" / "svc-17-18-observability"

_CONTAINER_TYPE = "proxmox_virtual_environment_container"

#: Expected shape from the SVC-17/18 requirements §6 (three observability LXCs).
#: ``cores``/``memory``/``disk`` per §6; Prometheus bumped +1 vCPU / +512 MB for
#: the co-located Alertmanager. ``prevent_destroy`` seatbelt on the stateful
#: TSDB/chunk guests (Prometheus, Loki), NOT on config-as-code Grafana.
_EXPECTED = {
    "prometheus": {
        "hostname": "svc17-prometheus-20-01",
        "ipv4": "10.0.20.18",
        "tag": "proj-shared-prometheus",
        "cores": 3,
        "memory": 4608,
        "disk": 100,
        "svc_code": "svc17",
        "component": "prometheus",
        "host_index": 8,
        "prevent_destroy": True,
    },
    "grafana": {
        "hostname": "svc17-grafana-20-01",
        "ipv4": "10.0.20.19",
        "tag": "proj-shared-grafana",
        "cores": 1,
        "memory": 1024,
        "disk": 10,
        "svc_code": "svc17",
        "component": "grafana",
        "host_index": 9,
        "prevent_destroy": False,
    },
    "loki": {
        "hostname": "svc18-loki-20-01",
        "ipv4": "10.0.20.20",
        "tag": "proj-shared-loki",
        "cores": 2,
        "memory": 4096,
        "disk": 200,
        "svc_code": "svc18",
        "component": "loki",
        "host_index": 10,
        "prevent_destroy": True,
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
class TestObsRootShapeStatic:
    """SVC-17/18 root: exactly three observability LXCs with the §6 shape."""

    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_OBS_ROOT)

    @staticmethod
    @pytest.fixture(scope="class")
    def counts(hcl) -> dict[str, int]:
        return _count_resources(hcl)

    def test_exactly_three_container_resources(self, counts):
        assert counts.get(_CONTAINER_TYPE, 0) == 3, (
            "SVC-17/18 root must declare EXACTLY three "
            f"{_CONTAINER_TYPE} resources (prometheus, grafana, loki); "
            f"found {counts.get(_CONTAINER_TYPE, 0)}"
        )

    def test_no_other_container_resource_type(self, counts):
        other = [t for t in counts if "container" in t.lower() and t != _CONTAINER_TYPE]
        assert not other, (
            f"SVC-17/18 root must declare NO container resource other than {_CONTAINER_TYPE}; found {other}"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_named_container_present(self, hcl, name):
        assert _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name) is not None, (
            f'expected a {_CONTAINER_TYPE} "{name}" resource block'
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_container_sizes_match_requirement(self, hcl, name):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        exp = _EXPECTED[name]
        cores = re.search(r"^\s*cores\s*=\s*(\d+)", block, re.MULTILINE)
        assert cores and int(cores.group(1)) == exp["cores"], (
            f"{name} must declare cores = {exp['cores']} (§6)"
        )
        mem = re.search(r"^\s*dedicated\s*=\s*(\d+)", block, re.MULTILINE)
        assert mem and int(mem.group(1)) == exp["memory"], (
            f"{name} must declare memory.dedicated = {exp['memory']} (§6)"
        )
        disk = re.search(r"^\s*size\s*=\s*(\d+)", block, re.MULTILINE)
        assert disk and int(disk.group(1)) == exp["disk"], (
            f"{name} must declare disk.size = {exp['disk']} (§6)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_container_is_unprivileged(self, hcl, name):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        assert re.search(r"^\s*unprivileged\s*=\s*true", block, re.MULTILINE), (
            f"{name} must be an unprivileged container (PF LXC-default)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_per_container_swap_cap(self, hcl, name):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        mem = _slice_subblock(block, "memory")
        assert mem is not None, f"{name} must declare a memory block"
        assert re.search(r"^\s*swap\s*=\s*0\b", mem, re.MULTILINE), (
            f"{name} memory block must set `swap = 0` — the provider-native "
            "per-container swap cap (ADR-0006)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_container_nic_on_p20_without_nic_vlan_tag(self, hcl, name):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, f"{name} must declare a network_interface"
        assert re.search(r'^\s*bridge\s*=\s*"p20"', nic, re.MULTILINE), (
            f'{name} NIC must attach to the VLAN-20 shared-services SDN VNet bridge = "p20"'
        )
        assert not re.search(r"^\s*vlan_id\s*=", nic, re.MULTILINE), (
            f'{name} NIC must NOT declare a NIC-level vlan_id: p20 is a VLAN-zone SDN VNet that '
            'applies tag 20 itself (double-tag => "vm vlans are not allowed on vnet p20")'
        )

    def test_prevent_destroy_on_prometheus_and_loki(self, hcl):
        for name in ("prometheus", "loki"):
            block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
            assert block is not None
            life = _slice_subblock(block, "lifecycle")
            assert life is not None, f"{name} must declare a lifecycle block"
            assert re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
                f"{name} lifecycle must set prevent_destroy = true — the "
                "stateful TSDB/chunk-store seatbelt (design §3.1)"
            )

    def test_no_prevent_destroy_on_grafana(self, hcl):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "grafana")
        assert block is not None
        life = _slice_subblock(block, "lifecycle")
        if life is not None:
            assert not re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
                "grafana must NOT set prevent_destroy = true — it is config-as-code "
                "(dashboards are version-controlled JSON), so it carries no "
                "stateful-destroy seatbelt (design §3.1)"
            )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_module_invocation_derives_computed_addressing(self, hcl, name):
        mod = _extract_named_block(hcl, "module", name)
        assert mod is not None, f'expected a module "{name}" invocation'
        exp = _EXPECTED[name]
        assert re.search(r'^\s*source\s*=\s*"\.\./\.\./modules/proxmox-compute"', mod, re.MULTILINE), (
            f"module {name} must consume the shared proxmox-compute module"
        )
        assert re.search(rf'^\s*svc_code\s*=\s*"{exp["svc_code"]}"', mod, re.MULTILINE), (
            f"module {name} must set svc_code = {exp['svc_code']}"
        )
        assert re.search(rf'^\s*component\s*=\s*"{exp["component"]}"', mod, re.MULTILINE), (
            f"module {name} must set component = {exp['component']} "
            f"(-> hostname {exp['hostname']}, NET-00 §4)"
        )
        assert re.search(r"^\s*vlan_id\s*=\s*(20\b|local\.\w+)", mod, re.MULTILINE), (
            f"module {name} must set vlan_id to VLAN 20 — either the literal 20 "
            "or a local. reference (NET-00 §3)"
        )
        assert re.search(
            rf"^\s*host_index\s*=\s*({exp['host_index']}\b|local\.\w+)", mod, re.MULTILINE
        ), (
            f"module {name} must set host_index = {exp['host_index']} so the computed IP is "
            f"{exp['ipv4']} — either the literal or a local. reference from the ledger (NET-00 §3)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_module_invocation_wires_peer_host_indices(self, hcl, name):
        mod = _extract_named_block(hcl, "module", name)
        assert mod is not None
        assert re.search(r"^\s*peer_host_indices\s*=\s*local\.\w+", mod, re.MULTILINE), (
            f"module {name} must thread peer_host_indices (from the committed ledger) into "
            "the proxmox-compute Guard 4 plan-time within-VLAN collision precondition (ADR-0015)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_hostname_and_ip_come_from_module_not_literal(self, hcl, name):
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        assert re.search(rf"^\s*hostname\s*=\s*module\.{name}\.hostname", block, re.MULTILINE), (
            f"{name} hostname must come from module.{name}.hostname"
        )
        assert re.search(rf"address\s*=\s*module\.{name}\.ip_config\.ipv4\.address", block), (
            f"{name} IP must come from module.{name}.ip_config (computed, not a literal)"
        )
        assert re.search(rf"^\s*tags\s*=\s*module\.{name}\.tags", block, re.MULTILINE), (
            f"{name} tags must come from module.{name}.tags"
        )
        assert not re.search(r'address\s*=\s*"10\.0\.20\.\d+', block), (
            f"{name} must NOT hardcode an IP literal — the address is module-computed (NET-00 §3)"
        )

    def test_no_ip_literal_anywhere_in_root(self, hcl):
        # No operator-chosen VLAN-20 host IP literal in ANY resource/module/output
        # assignment; every address is module-computed. (Comments are already
        # stripped by _read_root_hcl, so the documented .18/.19/.20 in comment
        # prose does not trip this.)
        for ip in ("10.0.20.18", "10.0.20.19", "10.0.20.20"):
            assert ip not in hcl, (
                f"IP literal {ip} must NOT appear in tracked SVC-17/18 HCL — every "
                "address is computed by proxmox-compute via cidrhost() (NET-00 §3)"
            )

    def test_operator_ssh_public_keys_variable_declared(self, hcl):
        block = _extract_named_block(hcl, "variable", "operator_ssh_public_keys")
        assert block is not None, 'SVC-17/18 root must declare variable "operator_ssh_public_keys"'
        assert re.search(r"^\s*type\s*=\s*list\(string\)", block, re.MULTILINE), (
            "operator_ssh_public_keys must be typed list(string)"
        )
        assert re.search(r"^\s*default\s*=\s*\[\s*\]", block, re.MULTILINE), (
            "operator_ssh_public_keys must default to [] (SSH-unreachable but non-breaking)"
        )

    @pytest.mark.parametrize("name", ["prometheus", "grafana", "loki"])
    def test_module_invocation_threads_ssh_public_keys(self, hcl, name):
        mod = _extract_named_block(hcl, "module", name)
        assert mod is not None
        assert re.search(r"^\s*ssh_public_keys\s*=\s*var\.operator_ssh_public_keys", mod, re.MULTILINE), (
            f"module {name} must pass ssh_public_keys = var.operator_ssh_public_keys"
        )

    def test_inventory_hosts_output_has_three_entries(self, hcl):
        block = _extract_named_block(hcl, "output", "inventory_hosts")
        assert block is not None, 'SVC-17/18 root must declare an output "inventory_hosts" (the generator surface)'
        for name in ("prometheus", "grafana", "loki"):
            assert re.search(rf"module\.{name}", block), (
                f"inventory_hosts must reference module.{name} (one entry per guest)"
            )
            assert re.search(
                rf"vmid\s*=\s*proxmox_virtual_environment_container\.{name}\.vm_id", block
            ), f"inventory_hosts vmid for {name} must come from the container resource's vm_id"
            assert re.search(
                rf"node_name\s*=\s*proxmox_virtual_environment_container\.{name}\.node_name", block
            ), f"inventory_hosts node_name for {name} must come from the container resource"
        for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
            assert re.search(rf"^\s*{field}\s*=", block, re.MULTILINE), (
                f"inventory_hosts host objects must carry `{field}`"
            )

    def test_service_fact_outputs_declared(self, hcl):
        # The per-component internal-IP + composed-URL outputs (design §3.1 /
        # requirements §8, FR-8, FR-13).
        for out_name, ref_re in (
            ("prometheus_internal_ip", r"module\.prometheus\.ipv4_address"),
            ("grafana_internal_ip", r"module\.grafana\.ipv4_address"),
            ("loki_internal_ip", r"module\.loki\.ipv4_address"),
        ):
            block = _extract_named_block(hcl, "output", out_name)
            assert block is not None, f'SVC-17/18 root must declare output "{out_name}"'
            assert re.search(ref_re, block), (
                f"{out_name} must come from the module's computed ipv4_address (not a literal)"
            )
        # grafana_url / loki_push_url embed `//` inside their URL string values
        # (https:// / http://), which the line-level comment stripper in
        # `_read_root_hcl` would truncate — so inspect the RAW outputs.tf for
        # these two blocks instead of the comment-stripped HCL.
        raw_outputs = (_OBS_ROOT / "outputs.tf").read_text()
        # grafana_url: external Traefik-fronted URL composed from platform_domain.
        grafana_url = _extract_named_block(raw_outputs, "output", "grafana_url")
        assert grafana_url is not None, 'SVC-17/18 root must declare output "grafana_url"'
        assert re.search(r"var\.platform_domain", grafana_url), (
            "grafana_url must be composed from var.platform_domain (FR-13)"
        )
        # loki_push_url: internal push endpoint composed from the computed Loki IP.
        loki_push = _extract_named_block(raw_outputs, "output", "loki_push_url")
        assert loki_push is not None, 'SVC-17/18 root must declare output "loki_push_url"'
        assert re.search(r"module\.loki\.ipv4_address", loki_push), (
            "loki_push_url must be composed from module.loki.ipv4_address (computed, not a literal)"
        )

    def test_no_ssh_key_literal_in_any_tracked_tf(self):
        tf_files = sorted(_OBS_ROOT.glob("*.tf"))
        assert tf_files, f"no .tf files found in {_OBS_ROOT}"
        raw = "\n".join(p.read_text() for p in tf_files)
        for pattern, label in (
            (r"ssh-ed25519\s+AAAA", "ssh-ed25519 public key literal"),
            (r"ssh-rsa\s+AAAA", "ssh-rsa public key literal"),
            (r"ecdsa-sha2-\S+\s+AAAA", "ecdsa public key literal"),
            (r"-----BEGIN[^\n]*PRIVATE KEY-----", "PEM private-key header"),
        ):
            assert not re.search(pattern, raw), (
                f"a {label} must NOT appear in any tracked SVC-17/18 .tf file — keys come from "
                f"TF_VAR_operator_ssh_public_keys / gitignored .env, never committed"
            )

    def test_state_name_in_versions_is_obs(self):
        versions = (_OBS_ROOT / "versions.tf").read_text()
        assert "svc-17-18-observability" in versions, (
            "versions.tf must document the GitLab-managed state name "
            "svc-17-18-observability (state isolation)"
        )
        assert re.search(r'backend\s+"http"\s*\{', versions), (
            'versions.tf must declare the partial backend "http" {} (GitLab-managed state, PF FR-2)'
        )


# --------------------------------------------------------------------------- #
# terraform fmt/validate (secondary — gated on the toolchain).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestObsTerraformFmtValidate:
    def test_fmt_check_clean(self):
        binary = _terraform_binary()
        proc = subprocess.run(
            [binary, "fmt", "-check", "-recursive", "-no-color"],
            cwd=_OBS_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "`terraform fmt -check` reported unformatted files in the SVC-17/18 root "
            f"(run `terraform fmt`):\n{proc.stdout}\n{proc.stderr}"
        )

    def test_init_backend_false_then_validate(self):
        binary = _terraform_binary()
        sandbox = Path(tempfile.mkdtemp(prefix="obs_validate_"))
        try:
            shutil.copytree(_INFRA_DIR, sandbox / "infra")
            target = sandbox / "infra" / "projects" / "svc-17-18-observability"
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
                f"`terraform validate` failed on the SVC-17/18 root:\n{val.stdout}\n{val.stderr}"
            )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
