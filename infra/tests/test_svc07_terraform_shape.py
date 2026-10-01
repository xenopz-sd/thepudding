"""Offline Terraform validation + resource-shape assertions for the SVC-07 root.

Task 3.1 (spec: svc-07-secrets-manager) — requirements.md Requirement 9
(9.1–9.5); design.md "Testing Strategy §1 (config-validation)" and the
"Requirement → evidence map" row 9.x ("Terraform plan resource-shape +
state-name uniqueness + `SDN.Allocate` degraded + destroy-isolation").

These are *config-validation / contract-style* tests for the SVC-07 IaC layer,
NOT property-based tests: per the testing-strategy steering and design.md, the
IaC layer's behaviour does not vary with generated input and is validated by
`terraform fmt`/`validate`, plan resource-shape assertions, and (elsewhere)
integration apply — never by PBT (design.md "Correctness Properties" is
intentionally empty for this feature).

Mirrors the sibling ``test_terraform_shape.py`` (spec: proxmox-network-
foundation) exactly in structure and gating, and reuses that suite's terraform
gate verbatim — ``requires_terraform`` / ``_terraform_binary`` from
``infra/tests/conftest.py`` (``shutil.which("terraform") or
shutil.which("tofu")``). No new gate is invented.

Two layers of assertions over the SVC-07 root
(``infra/projects/svc-07-secrets-manager/``):

1. **Static HCL/source inspection (primary — ALWAYS runs, no binary needed).**
   Parses the ``.tf`` files as text (comment-stripped) and asserts: exactly two
   ``proxmox_virtual_environment_container`` resources and no other container
   resource type (Req 9.1); each guest's sizes (cpu cores / memory / disk) match
   Req 9.2 (primary 2 / 2048 / 20) and Req 9.3 (unsealer 1 / 512 / 8); the two
   ``proxmox-compute`` module invocations carry the ``vlan_id``/``host_index``/
   ``svc_code``/``component`` that DERIVE the computed IPs ``10.0.20.10`` /
   ``10.0.20.11``, hostnames ``svc07-20-01`` / ``svc07-unsealer-20-01``, and
   tags ``proj-shared-openbao`` / ``proj-shared-openbao-unsealer`` (Req 9.2–9.5);
   and that no operator IP literal is written (the addresses are module-computed,
   NET-00 §3).

2. **terraform ``fmt``/``validate`` + plan-json inspection (secondary — GATED).**
   When a ``terraform`` (or ``tofu``) binary is present: ``terraform fmt -check``
   and ``terraform init -backend=false && terraform validate`` on the real root
   (both fully OFFLINE — ``validate`` does not need a configured backend), and
   the SAME resource-shape assertions against ``terraform show -json`` of an
   offline ``terraform plan``. The plan is produced by a self-contained sandbox
   helper (below) that copies the repo's ``infra/`` tree into a throwaway
   directory, drops a transient ``backend "local" {}`` override so the root's
   partial ``backend "http" {}`` (PF FR-2) does not abort ``plan`` with "Backend
   initialization required", and plans with dummy, non-contacting
   ``proxmox_endpoint`` / ``proxmox_api_token`` values plus dummy guest inputs —
   so NO live GitLab HTTP backend / ``CI_JOB_TOKEN`` / Proxmox credentials are
   required. This is the exact offline-plan technique the sibling suite uses
   (``infra/tests/conftest.py::offline_plan``), inlined here because the shared
   helper only knows the ``foundation`` / ``project_template`` roots and passes
   no guest ``-var`` inputs; keeping the SVC-07 variant local avoids changing
   that shared helper's contract. When no binary is present the whole gated
   class is skipped (per documentation-testing steering: absent infra =>
   skipped, not failed).

The state-name uniqueness (Req 9.6) and the ``SDN.Allocate`` degraded-mode /
destroy-isolation assertions (Req 9.7, 9.8) belong to task 3.2 and are not here.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_terraform_shape.py -v
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

# Reuse the suite's established terraform gate verbatim (no new gate invented).
from conftest import requires_terraform, _terraform_binary

# --------------------------------------------------------------------------- #
# Locate the SVC-07 root relative to this test file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_SVC07_ROOT = _INFRA_DIR / "projects" / "svc-07-secrets-manager"

# --------------------------------------------------------------------------- #
# Expected shape from Requirement 9 (the single source of truth for the asserts).
# --------------------------------------------------------------------------- #
#: Per-guest expectations keyed by the Terraform resource *name* (Req 9.2/9.3).
_EXPECTED = {
    "primary": {
        "hostname": "svc07-20-01",
        "ipv4": "10.0.20.10",
        "tag": "proj-shared-openbao",
        "cores": 2,
        "memory": 2048,
        "disk": 20,
        # module invocation inputs that DERIVE the above (NET-00 §3-§4)
        "host_index": 0,
        "component": None,
    },
    "unsealer": {
        "hostname": "svc07-unsealer-20-01",
        "ipv4": "10.0.20.11",
        "tag": "proj-shared-openbao-unsealer",
        "cores": 1,
        "memory": 512,
        "disk": 8,
        "host_index": 1,
        "component": "unsealer",
    },
}

_CONTAINER_TYPE = "proxmox_virtual_environment_container"


# --------------------------------------------------------------------------- #
# Lightweight HCL helpers (same idiom as the sibling test_terraform_shape.py).
#
# We deliberately avoid a full HCL parser (none is pinned in the venv, and one
# is not warranted for shape counting): strip comments, then regex the
# `resource "<type>" "<name>" {` headers and pull brace-balanced block bodies.
# --------------------------------------------------------------------------- #
_RESOURCE_HEADER_RE = re.compile(
    r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{',
    re.MULTILINE,
)


def _strip_hcl_comments(text: str) -> str:
    """Remove `#`/`//` line comments and `/* ... */` block comments.

    Prevents a documented/commented-out resource type (the .tf files mention
    every resource type in prose) from being miscounted as a real declaration.
    """
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
    """Return the brace-balanced body of the first matching block.

    ``block_kw`` is ``resource``/``module``; ``label1``/``label2`` are the
    quoted labels (a ``module`` has one label, a ``resource`` has two).
    Comment-stripped HCL is expected. Returns None if not found.
    """
    if label2 is None:
        pattern = rf'{block_kw}\s+"{re.escape(label1)}"\s*\{{'
    else:
        pattern = rf'{block_kw}\s+"{re.escape(label1)}"\s+"{re.escape(label2)}"\s*\{{'
    header = re.search(pattern, hcl)
    if header is None:
        return None
    start = header.end() - 1  # opening brace
    depth = 0
    for i in range(start, len(hcl)):
        if hcl[i] == "{":
            depth += 1
        elif hcl[i] == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    return None


# --------------------------------------------------------------------------- #
# Static HCL inspection tests (primary — ALWAYS run, no terraform binary).
# --------------------------------------------------------------------------- #
class TestSvc07RootShapeStatic:
    """SVC-07 root: exactly two LXC containers with the Req 9 shapes."""

    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_SVC07_ROOT)

    @staticmethod
    @pytest.fixture(scope="class")
    def counts(hcl) -> dict[str, int]:
        return _count_resources(hcl)

    def test_exactly_two_container_resources(self, counts):
        """Exactly two proxmox_virtual_environment_container resources (Req 9.1)."""
        assert counts.get(_CONTAINER_TYPE, 0) == 2, (
            "SVC-07 root must declare EXACTLY two "
            f"{_CONTAINER_TYPE} resources (primary + unsealer) — Req 9.1; "
            f"found {counts.get(_CONTAINER_TYPE, 0)}"
        )

    def test_no_other_container_resource_type(self, counts):
        """No other *container* resource type is declared (Req 9.1)."""
        other_container_types = [
            t for t in counts
            if "container" in t.lower() and t != _CONTAINER_TYPE
        ]
        assert not other_container_types, (
            "SVC-07 root must declare NO container resource other than "
            f"{_CONTAINER_TYPE} (Req 9.1); found {other_container_types}"
        )

    def test_both_named_containers_present(self, hcl):
        """The two containers are named `primary` and `unsealer` (Req 9.1)."""
        for name in ("primary", "unsealer"):
            assert _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name) is not None, (
                f"expected a {_CONTAINER_TYPE} \"{name}\" resource block (Req 9.1)"
            )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_container_sizes_match_requirement(self, hcl, name):
        """cpu cores / memory / disk match Req 9.2 (primary) and 9.3 (unsealer)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None, f"missing {name} container block"
        exp = _EXPECTED[name]

        cores = re.search(r"^\s*cores\s*=\s*(\d+)", block, re.MULTILINE)
        assert cores and int(cores.group(1)) == exp["cores"], (
            f"{name} must declare cores = {exp['cores']} (Req 9.2/9.3)"
        )
        mem = re.search(r"^\s*dedicated\s*=\s*(\d+)", block, re.MULTILINE)
        assert mem and int(mem.group(1)) == exp["memory"], (
            f"{name} must declare memory.dedicated = {exp['memory']} (Req 9.2/9.3)"
        )
        disk = re.search(r"^\s*size\s*=\s*(\d+)", block, re.MULTILINE)
        assert disk and int(disk.group(1)) == exp["disk"], (
            f"{name} must declare disk.size = {exp['disk']} (Req 9.2/9.3)"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_container_is_unprivileged(self, hcl, name):
        """Both LXCs are unprivileged (Req 9.2/9.3)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        assert re.search(r"^\s*unprivileged\s*=\s*true", block, re.MULTILINE), (
            f"{name} must be an unprivileged container (Req 9.2/9.3)"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_per_container_swap_cap_retained(self, hcl, name):
        """(d) PRESERVATION (ADR-0006, spec: fix-openbao-swap-hardening, Task 1
        / Design C4): the Terraform per-container swap cap (#2 in ADR-0006) is
        RETAINED on BOTH LXCs — the ``memory`` block sets ``swap = 0``, the
        provider-native expression of the "no secret material to swap" control.
        This is the Terraform half of Fix-Checking assertion (d) (the Docker
        ``mem_swappiness``/``--memory-swappiness=0`` half is asserted in
        test_svc07_openbao_config_render.py). GREEN today and must keep passing
        after the fix; the swap-hardening fix only rewords the surrounding
        cross-reference comments, never removes ``swap = 0`` (Req 3.3)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None, f"missing {name} container block"
        mem = _slice_subblock(block, "memory")
        assert mem is not None, f"{name} must declare a memory block"
        assert re.search(r"^\s*swap\s*=\s*0\b", mem, re.MULTILINE), (
            f"{name} memory block must RETAIN `swap = 0` — the provider-native "
            f"per-container swap cap (#2 in ADR-0006), kept as per-guest "
            f"defence-in-depth (Req 3.3, spec: fix-openbao-swap-hardening)"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_container_nic_on_p20_without_nic_vlan_tag(self, hcl, name):
        """Both LXCs attach their NIC to the p20 VLAN-zone SDN VNet and do NOT
        set a NIC-level vlan_id. p20 is a VLAN-zone SDN VNet that applies tag 20
        itself; a NIC-level vlan_id double-tags and Proxmox rejects the container
        start with "vm vlans are not allowed on vnet p20" (SDN Zones Plugin).
        Regression guard for the SVC-07 SDN NIC double-tag bug (Req 2.1, 3.1)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, f"{name} must declare a network_interface"
        # Preservation: the NIC must stay attached to the VLAN-20 SDN VNet p20.
        assert re.search(r'^\s*bridge\s*=\s*"p20"', nic, re.MULTILINE), (
            f"{name} NIC must attach to the VLAN-20 shared-services SDN VNet "
            f'bridge = "p20" (Req 3.1)'
        )
        # Fix guard: NO NIC-level vlan_id — the VLAN-zone VNet supplies tag 20.
        assert not re.search(r"^\s*vlan_id\s*=", nic, re.MULTILINE), (
            f"{name} NIC must NOT declare a NIC-level vlan_id: p20 is a VLAN-zone "
            f"SDN VNet that applies tag 20 itself, so a NIC vlan_id double-tags and "
            f'Proxmox rejects container start with "vm vlans are not allowed on '
            f'vnet p20" (Req 2.1)'
        )

    def test_unsealer_nic_firewall_not_enabled(self, hcl):
        """REGRESSION GUARD (ADR-0008): the unsealer NIC must NOT set firewall = true.

        Enabling the per-NIC guest firewall was tried as the enforcement point for
        primary-only :8200 access and BROKE THE GUEST: it lost all egress, `apt
        update` failed, Docker was never installed, and DNS resolution returned
        nothing — and it still failed after five verified guest-scoped `out` ACCEPT
        rules (DNS/HTTP/HTTPS/NTP) were applied and pve-firewall reloaded. It also
        re-plugs the guest behind an `fwbr` bridge, disturbing the ADR-0004 host
        egress MASQUERADE path.

        Network-layer primary-only scoping is therefore a documented KNOWN
        LIMITATION (currently UNMET), and the mechanism is left open for a later
        decision. This test makes re-enabling the flag a DELIBERATE, reviewed act
        that must come with a solved egress story — not an accidental
        re-introduction of the outage. If you are intentionally revisiting this,
        update ADR-0008 and this test together.
        """
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, "unsealer")
        assert block is not None
        nic = _slice_subblock(block, "network_interface")
        assert nic is not None, "unsealer must declare a network_interface"
        assert not re.search(r"^\s*firewall\s*=\s*true", nic, re.MULTILINE), (
            "unsealer NIC must NOT set firewall = true (ADR-0008): it removes the "
            "guest's egress and makes it unprovisionable, even with scoped egress "
            "ACCEPT rules applied. Re-enable only with a solved egress story and an "
            "updated ADR."
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_container_prevent_destroy_seatbelt(self, hcl, name):
        """Both live secret-store LXCs carry lifecycle.prevent_destroy = true, so
        Terraform HARD-ERRORS on any destroy/replace plan (e.g. a drifted
        datastore_id/template forcing replacement) instead of offering a `yes`
        prompt. Regression guard for the incident where a bare `terraform apply`
        with a wrong openbao_datastore_id default destroyed both live OpenBao
        containers (their Raft/Transit + unseal state)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        life = _slice_subblock(block, "lifecycle")
        assert life is not None, f"{name} must declare a lifecycle block"
        assert re.search(r"^\s*prevent_destroy\s*=\s*true", life, re.MULTILINE), (
            f"{name} lifecycle must set prevent_destroy = true — these are live, "
            f"stateful secret stores that must never be silently destroyed/replaced "
            f"by an apply (the seatbelt for the datastore-drift replacement incident)."
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_module_invocation_derives_computed_addressing(self, hcl, name):
        """The proxmox-compute module call carries the vlan_id/host_index/
        svc_code/component that DERIVE the computed IP, hostname, and tag —
        and no operator IP literal is passed (Req 9.2-9.5, NET-00 §3)."""
        mod = _extract_named_block(hcl, "module", name)
        assert mod is not None, f"expected a module \"{name}\" invocation"
        exp = _EXPECTED[name]

        assert re.search(r'^\s*source\s*=\s*"\.\./\.\./modules/proxmox-compute"', mod, re.MULTILINE), (
            f"module {name} must consume the shared proxmox-compute module"
        )
        assert re.search(r"^\s*svc_code\s*=\s*\"svc07\"", mod, re.MULTILINE), (
            f"module {name} must set svc_code = svc07 (NET-00 §4)"
        )
        assert re.search(r"^\s*vlan_id\s*=\s*20\b", mod, re.MULTILINE), (
            f"module {name} must set vlan_id = 20 (Req 9.2/9.3)"
        )
        assert re.search(rf"^\s*host_index\s*=\s*{exp['host_index']}\b", mod, re.MULTILINE), (
            f"module {name} must set host_index = {exp['host_index']} so the "
            f"computed IP is {exp['ipv4']} (Req 9.2/9.3, NET-00 §3)"
        )
        if exp["component"] is None:
            assert not re.search(r"^\s*component\s*=", mod, re.MULTILINE), (
                f"module {name} must NOT set a component (plain hostname "
                f"{exp['hostname']})"
            )
        else:
            assert re.search(rf'^\s*component\s*=\s*"{exp["component"]}"', mod, re.MULTILINE), (
                f"module {name} must set component = {exp['component']} so the "
                f"hostname is {exp['hostname']} (NET-00 §4)"
            )

    # ----------------------------------------------------------------------- #
    # Shared-symbol propagation guards (spec: terraform-ansible-handoff, task
    # 10, components C2 + C3). The handoff feature threads operator SSH keys
    # into BOTH SVC-07 containers (via a conditional `dynamic "user_account"`
    # gated on the empty-default no-op) and adds the generic `inventory_hosts`
    # root output the Ansible inventory generator keys on. These positive
    # assertions catch a future regression that drops the injection, the no-op
    # gate, the module thread-through, or the output — and guard that NO SSH key
    # literal is ever committed. See design.md "Testing Strategy §2".
    # ----------------------------------------------------------------------- #
    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_container_injects_operator_ssh_keys_via_user_account(self, hcl, name):
        """Each container's `initialization` block carries a conditional
        `dynamic "user_account"` whose `keys` references
        `var.operator_ssh_public_keys`, gated on
        `length(var.operator_ssh_public_keys) > 0` so the empty default injects
        NO block (strict no-op). (terraform-ansible-handoff Req 1.5, 2.2, C2)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None, f"missing {name} container block"
        init = _slice_subblock(block, "initialization")
        assert init is not None, (
            f"{name} must declare an initialization block (C2)"
        )
        # The conditional dynamic user_account block lives inside initialization.
        assert re.search(r'dynamic\s+"user_account"', init), (
            f'{name} initialization must inject a `dynamic "user_account"` block '
            f"so operator SSH keys reach the guest root account (Req 1.5, C2)"
        )
        # Empty-default no-op gate: for_each keyed on the key-list length.
        assert re.search(
            r"for_each\s*=\s*length\(\s*var\.operator_ssh_public_keys\s*\)\s*>\s*0",
            init,
        ), (
            f"{name} user_account block must be gated on "
            f"`length(var.operator_ssh_public_keys) > 0` — the empty-default "
            f"no-op that keeps a keyless apply byte-identical to pre-change "
            f"(Req 1.3, FD.3, C2)"
        )
        # The injected keys come from the operator variable, never a literal.
        assert re.search(r"keys\s*=\s*var\.operator_ssh_public_keys", init), (
            f"{name} user_account.keys must reference var.operator_ssh_public_keys "
            f"(Req 1.5, C2)"
        )

    def test_no_ssh_key_literal_in_any_tracked_tf(self):
        """No OpenSSH public/private key literal appears in ANY tracked SVC-07
        `.tf` file — keys are sourced from TF_VAR_operator_ssh_public_keys /
        gitignored .env, never committed (terraform-ansible-handoff Req 1.6,
        SEC.2, C2). Scanned on the RAW (non-comment-stripped) text so a key
        hidden in a comment would also trip this guard."""
        tf_files = sorted(_SVC07_ROOT.glob("*.tf"))
        assert tf_files, f"no .tf files found in {_SVC07_ROOT}"
        raw = "\n".join(p.read_text() for p in tf_files)
        # OpenSSH public-key material (ssh-ed25519/ssh-rsa AAAA..., ecdsa-...)
        # and any PEM private-key header.
        for pattern, label in (
            (r"ssh-ed25519\s+AAAA", "ssh-ed25519 public key literal"),
            (r"ssh-rsa\s+AAAA", "ssh-rsa public key literal"),
            (r"ecdsa-sha2-\S+\s+AAAA", "ecdsa public key literal"),
            (r"-----BEGIN[^\n]*PRIVATE KEY-----", "PEM private-key header"),
        ):
            assert not re.search(pattern, raw), (
                f"a {label} must NOT appear in any tracked SVC-07 .tf file — "
                f"keys come from TF_VAR_operator_ssh_public_keys / gitignored "
                f".env, never committed (Req 1.6, SEC.2)"
            )

    def test_operator_ssh_public_keys_variable_declared(self, hcl):
        """The SVC-07 root declares `variable "operator_ssh_public_keys"` of
        `list(string)` defaulting to `[]` (terraform-ansible-handoff Req 1.6,
        2.2, C2). The `[]` default is the documented SSH-unreachable-but-
        non-breaking degraded state (Req 1.3, FD.3)."""
        block = _extract_named_block(hcl, "variable", "operator_ssh_public_keys")
        assert block is not None, (
            'SVC-07 root must declare `variable "operator_ssh_public_keys"` '
            "(Req 1.6, C2) — not found"
        )
        assert re.search(r"^\s*type\s*=\s*list\(string\)", block, re.MULTILINE), (
            "operator_ssh_public_keys must be typed `list(string)` (C2)"
        )
        assert re.search(r"^\s*default\s*=\s*\[\s*\]", block, re.MULTILINE), (
            "operator_ssh_public_keys must default to `[]` — the SSH-unreachable "
            "but non-breaking degraded default (Req 1.3, FD.3, C2)"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_module_invocation_threads_ssh_public_keys(self, hcl, name):
        """Both module invocations thread the operator keys into the shared
        module's `ssh_public_keys` input (forward-compat echo of the C1
        contract) — terraform-ansible-handoff Req 1.5, 2.2, C2."""
        mod = _extract_named_block(hcl, "module", name)
        assert mod is not None, f"expected a module \"{name}\" invocation"
        assert re.search(
            r"^\s*ssh_public_keys\s*=\s*var\.operator_ssh_public_keys",
            mod,
            re.MULTILINE,
        ), (
            f"module {name} must pass "
            f"`ssh_public_keys = var.operator_ssh_public_keys` to exercise the "
            f"shared module's forward-compat echo contract (C1/C2)"
        )

    def test_inventory_hosts_output_declared_and_coexists(self, hcl):
        """The SVC-07 root declares the generic `inventory_hosts` output the
        Ansible inventory generator keys on — a list built from module.primary
        and module.unsealer carrying hostname/ipv4_address/service_role/
        project_slug/vmid — AND the existing service-specific openbao_* outputs
        are STILL present (coexistence, Q1). (terraform-ansible-handoff Req 3,
        2.2, C3)."""
        block = _extract_named_block(hcl, "output", "inventory_hosts")
        assert block is not None, (
            'SVC-07 root must declare an `output "inventory_hosts"` (the generic '
            "inventory surface the generator consumes — Req 3, C3); not found"
        )
        # Built from both module outputs.
        assert re.search(r"module\.primary", block), (
            "inventory_hosts must reference module.primary (C3)"
        )
        assert re.search(r"module\.unsealer", block), (
            "inventory_hosts must reference module.unsealer (C3)"
        )
        # Per-element host-object schema (all five standard fields).
        for field in ("hostname", "ipv4_address", "service_role", "project_slug", "vmid", "node_name"):
            assert re.search(rf"^\s*{field}\s*=", block, re.MULTILINE), (
                f"inventory_hosts host objects must carry `{field}` (C3 schema)"
            )
        # Coexistence: the existing service-specific outputs must remain (Q1).
        for existing in (
            "openbao_internal_ip",
            "openbao_api_port",
            "openbao_unsealer_internal_ip",
        ):
            assert _extract_named_block(hcl, "output", existing) is not None, (
                f"existing `output \"{existing}\"` must STILL be present alongside "
                f"inventory_hosts — downstream OpenBao-Agent bootstraps consume it "
                f"(coexistence, Q1)"
            )

        # Fix 1 (ADR-0007): vmid + node_name must come from the CONCRETE container
        # resources, not module.{primary,unsealer}.vmid (a null stand-in). This is
        # what carries the real, provider-assigned VMID + node downstream to the
        # Ansible inventory so the sdn_gateway per-guest firewall path needs no
        # hardcoded node/VMID.
        assert re.search(
            r"vmid\s*=\s*proxmox_virtual_environment_container\.primary\.vm_id", block
        ), "inventory_hosts primary vmid must come from the container resource's vm_id (Fix 1)"
        assert re.search(
            r"vmid\s*=\s*proxmox_virtual_environment_container\.unsealer\.vm_id", block
        ), "inventory_hosts unsealer vmid must come from the container resource's vm_id (Fix 1)"
        assert re.search(
            r"node_name\s*=\s*proxmox_virtual_environment_container\.unsealer\.node_name", block
        ), "inventory_hosts unsealer node_name must come from the container resource (Fix 1)"

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_hostname_and_ip_come_from_module_not_literal(self, hcl, name):
        """Hostname + IP are injected from the module output, never an IP
        literal (Req 9.2/9.3; NET-00 §3 — addresses are Terraform-computed)."""
        block = _extract_named_block(hcl, "resource", _CONTAINER_TYPE, name)
        assert block is not None
        assert re.search(rf"^\s*hostname\s*=\s*module\.{name}\.hostname", block, re.MULTILINE), (
            f"{name} hostname must come from module.{name}.hostname (NET-00 §4)"
        )
        assert re.search(
            rf"address\s*=\s*module\.{name}\.ip_config\.ipv4\.address", block
        ), (
            f"{name} IP must come from module.{name}.ip_config (computed, not a "
            f"literal) — Req 9.2/9.3, NET-00 §3"
        )
        assert re.search(rf"^\s*tags\s*=\s*module\.{name}\.tags", block, re.MULTILINE), (
            f"{name} tags must come from module.{name}.tags (PF §4)"
        )
        # Guard: no bare 10.0.20.x IP literal assigned to an address attribute.
        assert not re.search(r'address\s*=\s*"10\.0\.20\.\d+', block), (
            f"{name} must NOT hardcode an IP literal — the address is "
            f"module-computed (Req 9.2/9.3, NET-00 §3)"
        )


def _slice_subblock(block: str, sub_kw: str) -> str | None:
    """Return the brace-balanced body of the first ``<sub_kw> {`` nested block."""
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
# Offline-plan sandbox helper for the SVC-07 root.
#
# Self-contained variant of conftest.offline_plan: the shared helper only knows
# the `foundation` / `project_template` roots and passes no guest `-var` inputs,
# so a local variant is used here rather than widening that shared contract. It
# copies the repo's infra/ tree into a throwaway sandbox, drops a transient
# `backend "local" {}` override into the sandboxed SVC-07 root (so the partial
# `backend "http" {}` does not abort `plan` — PF FR-2), and plans with dummy,
# non-contacting provider + guest vars. Committed roots are never written to.
# --------------------------------------------------------------------------- #
_DUMMY_ENDPOINT = "https://dummy.invalid:8006/"
_DUMMY_TOKEN = "terraform@pve!ci=00000000-0000-0000-0000-000000000000"
_DUMMY_GUEST_VARS = (
    "-var", "proxmox_node_name=pve-test",
    "-var", "openbao_template_file_id=local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
    "-var", "openbao_primary_vmid=2010",
    "-var", "openbao_unsealer_vmid=2011",
)
_OVERRIDE_FILENAME = "zz_svc07_offline_backend_override.tf"
_OVERRIDE_CONTENT = (
    "# Transient, test-scoped local backend override written into a SANDBOX\n"
    "# COPY only by test_svc07_terraform_shape.py. Overrides the root's partial\n"
    "# `backend \"http\" {}` so an offline `terraform init -reconfigure` +\n"
    "# `plan` succeed without a live GitLab HTTP backend / CI_JOB_TOKEN. NEVER\n"
    "# committed into a real root (Req 9.6, PF FR-2).\n"
    "terraform {\n"
    "  backend \"local\" {}\n"
    "}\n"
)


@contextmanager
def _svc07_offline_plan() -> Iterator[list[dict]]:
    """Yield the ``resource_changes`` of an offline plan of the SVC-07 root.

    Skips cleanly (requires-infra) if ``terraform init`` cannot install/reuse
    the provider offline — an environmental prerequisite, never a failure.
    """
    binary = _terraform_binary()
    assert binary is not None, "gated by requires_terraform"

    sandbox = Path(tempfile.mkdtemp(prefix="svc07_offline_plan_"))
    try:
        # Copy the whole infra/ tree so ../../modules/proxmox-compute resolves.
        shutil.copytree(_INFRA_DIR, sandbox / "infra")
        target = sandbox / "infra" / "projects" / "svc-07-secrets-manager"
        # Clean any copied provider cache / local state so init starts fresh.
        for stale in (target / ".terraform", ):
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
                    "requires-infra, skipped: `terraform init` could not "
                    "install/reuse the provider offline — an environmental "
                    "prerequisite unrelated to the shape under test.\n"
                    f"init stderr tail:\n{init.stderr[-1200:]}"
                )
            pytest.fail(f"terraform init failed unexpectedly:\n{init.stdout}\n{init.stderr}")

        plan_path = target / "svc07.tfplan"
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
            "offline `terraform plan` of the SVC-07 root must succeed with "
            f"dummy vars; got exit {plan.returncode}:\n{plan.stdout}\n{plan.stderr}"
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


# --------------------------------------------------------------------------- #
# terraform fmt/validate + plan-json tests (secondary — gated on the toolchain).
# --------------------------------------------------------------------------- #
@requires_terraform
class TestSvc07TerraformFmtValidate:
    """`terraform fmt -check` and offline `validate` on the real SVC-07 root.

    Both are OFFLINE: `fmt -check` reads files only, and `validate` runs after
    `init -backend=false` (which configures no backend but installs providers)
    — `validate` does not require a configured backend. Skipped entirely when no
    terraform/tofu binary is present (documentation-testing steering).
    """

    def test_fmt_check_clean(self):
        binary = _terraform_binary()
        proc = subprocess.run(
            [binary, "fmt", "-check", "-recursive", "-no-color"],
            cwd=_SVC07_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "`terraform fmt -check` reported unformatted files in the SVC-07 "
            f"root (run `terraform fmt`):\n{proc.stdout}\n{proc.stderr}"
        )

    def test_init_backend_false_then_validate(self):
        binary = _terraform_binary()
        # A sandbox copy keeps the committed root's .terraform untouched.
        sandbox = Path(tempfile.mkdtemp(prefix="svc07_validate_"))
        try:
            shutil.copytree(_INFRA_DIR, sandbox / "infra")
            target = sandbox / "infra" / "projects" / "svc-07-secrets-manager"
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
                        "requires-infra, skipped: provider could not be "
                        "installed/reused offline for `init -backend=false`.\n"
                        f"{init.stderr[-1000:]}"
                    )
                pytest.fail(f"terraform init -backend=false failed:\n{init.stdout}\n{init.stderr}")
            val = subprocess.run(
                [binary, "validate", "-no-color"],
                cwd=target, capture_output=True, text=True,
            )
            assert val.returncode == 0, (
                "`terraform validate` failed on the SVC-07 root:\n"
                f"{val.stdout}\n{val.stderr}"
            )
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


@requires_terraform
class TestSvc07PlanJsonShape:
    """Resource-shape assertions via `terraform show -json` of an offline plan.

    Only runs when a terraform/tofu binary is present (else the class is
    skipped). Asserts the SAME Req 9 shapes the static tests assert, but against
    Terraform's own computed plan — proving the module-derived IPs/hostnames/
    tags resolve to exactly 10.0.20.10 / 10.0.20.11, svc07-20-01 /
    svc07-unsealer-20-01, proj-shared-openbao / proj-shared-openbao-unsealer.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def containers() -> dict[str, dict]:
        """Map container resource *name* -> its planned `after` attributes."""
        with _svc07_offline_plan() as changes:
            out: dict[str, dict] = {}
            for c in changes:
                if c["type"] == _CONTAINER_TYPE:
                    out[c["name"]] = c["change"]["after"]
            return out

    @staticmethod
    @pytest.fixture(scope="class")
    def all_types() -> list[str]:
        with _svc07_offline_plan() as changes:
            return [c["type"] for c in changes]

    def test_exactly_two_containers_planned(self, all_types):
        """Plan creates exactly two containers and no other container type (Req 9.1)."""
        assert all_types.count(_CONTAINER_TYPE) == 2, (
            f"plan must create exactly two {_CONTAINER_TYPE} (Req 9.1); "
            f"types planned: {sorted(set(all_types))}"
        )
        other_container = [
            t for t in set(all_types)
            if "container" in t.lower() and t != _CONTAINER_TYPE
        ]
        assert not other_container, (
            f"plan must create NO other container type (Req 9.1); found {other_container}"
        )

    def test_both_containers_present(self, containers):
        assert set(containers) == {"primary", "unsealer"}, (
            f"expected planned containers primary + unsealer (Req 9.1); got {sorted(containers)}"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_computed_ip_hostname_tag(self, containers, name):
        """Computed IP / hostname / tag match Req 9.2-9.5 exactly."""
        after = containers[name]
        exp = _EXPECTED[name]

        init = _first(after.get("initialization"))
        assert init is not None, f"{name}: no initialization block in plan"
        assert init.get("hostname") == exp["hostname"], (
            f"{name} hostname must be {exp['hostname']} (Req 9.4/9.5); "
            f"got {init.get('hostname')}"
        )
        ipv4 = _first(_first(init.get("ip_config")).get("ipv4") if init.get("ip_config") else None)
        assert ipv4 is not None, f"{name}: no ip_config.ipv4 in plan"
        assert ipv4.get("address") == f"{exp['ipv4']}/24", (
            f"{name} computed IP must be {exp['ipv4']}/24 (Req 9.2/9.3, NET-00 §3); "
            f"got {ipv4.get('address')}"
        )
        assert after.get("tags") == [exp["tag"]], (
            f"{name} tag must be [{exp['tag']!r}] (Req 9.4/9.5); got {after.get('tags')}"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_computed_sizes(self, containers, name):
        """cpu cores / memory / disk match Req 9.2 (primary) and 9.3 (unsealer)."""
        after = containers[name]
        exp = _EXPECTED[name]
        cpu = _first(after.get("cpu"))
        mem = _first(after.get("memory"))
        disk = _first(after.get("disk"))
        assert cpu and cpu.get("cores") == exp["cores"], (
            f"{name} cores must be {exp['cores']} (Req 9.2/9.3); got {cpu}"
        )
        assert mem and mem.get("dedicated") == exp["memory"], (
            f"{name} memory.dedicated must be {exp['memory']} (Req 9.2/9.3); got {mem}"
        )
        assert disk and disk.get("size") == exp["disk"], (
            f"{name} disk.size must be {exp['disk']} (Req 9.2/9.3); got {disk}"
        )

    @pytest.mark.parametrize("name", ["primary", "unsealer"])
    def test_unprivileged(self, containers, name):
        after = containers[name]
        assert after.get("unprivileged") is True, (
            f"{name} must be unprivileged (Req 9.2/9.3); got {after.get('unprivileged')}"
        )


def _first(seq):
    """Return the first element of a Terraform block-list (or {} / None).

    Terraform's JSON plan renders single nested blocks as a one-element list.
    """
    if not seq:
        return {} if seq == [] else None
    if isinstance(seq, list):
        return seq[0] if seq else {}
    return seq
