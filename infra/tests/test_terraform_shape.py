"""Provider resource-shape assertions for the Proxmox Network Foundation IaC layer.

Task 8.2 (spec: proxmox-network-foundation) — design.md "Testing Strategy §2,
Contract-style tests / Provider resource-shape assertions".

These are *contract-style* tests for the Terraform resource layer, NOT
property-based tests: per the testing-strategy steering and design.md, the IaC
layer's behaviour does not vary with generated input and is validated by
plan-idempotency, provider resource-shape assertions, and integration apply —
never by PBT.

Two implementations of the same assertions are provided:

1. **Static HCL/source inspection (primary, always runs).** Parses the `.tf`
   files as text and asserts resource counts, zone type, subnet CIDR/gateway
   formula, and the IPAM/DHCP-disabled + cloud-init `ip_config` posture. This
   path has no external dependency and runs and PASSES regardless of whether a
   terraform/tofu binary is present.

2. **terraform-json inspection (secondary, gated).** If the `terraform` (or
   `tofu`) binary is available, the same shapes are asserted against
   `terraform show -json` of a `terraform plan`. The plan is captured OFFLINE
   via the shared `offline_plan` helper (`infra/tests/conftest.py`): it copies
   both roots into a temporary sandbox (committed roots untouched), drops a
   transient `backend "local" {}` override, runs `terraform init -reconfigure`
   (NOT `init -backend=false`, which configures no backend and makes `plan`
   abort with "Backend initialization required ... backend http"), then runs
   `terraform plan` with dummy, non-contacting `proxmox_endpoint` /
   `proxmox_api_token` provider vars — so no live GitLab HTTP backend,
   `CI_JOB_TOKEN`, or Proxmox credential is needed. When no terraform/tofu
   binary is present the whole class is skipped, and `offline_plan` itself skips
   cleanly if the offline prerequisite is unavailable (per documentation-testing
   steering: absent infra => skipped, not failed).

Assertions (all against the two roots + the shared compute module):

  * foundation root (infra/platform-foundation/) has exactly one
    proxmox_sdn_zone_vlan + one proxmox_sdn_applier + exactly one
    shared-services VNet/subnet (the VLAN-20 `p20` / 10.0.20.0/24), and ZERO
    PER-PROJECT VNets/subnets (Req 4.1, 4.2, 6.1, NET-00 §6).
  * each project root (infra/projects/_TEMPLATE/) has exactly one
    proxmox_sdn_vnet + one proxmox_sdn_subnet + its own applier and NO zone
    (Req 4.3, 4.4, 6.2).
  * zone type is VLAN (proxmox_sdn_zone_vlan), never VXLAN/EVPN/bridge (Req 4.5).
  * subnet CIDR/gateway match the formula 10.0.<vlan_id>.0/24 / .1 (Req 4.4).
  * IPAM/DHCP disabled with cloud-init ip_config: no `ipam` attr on the zone,
    no `dhcp*` attr on the subnet, and proxmox-compute uses a cloud-init
    ip_config structure (Req 2.5).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_terraform_shape.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Locate the infra roots relative to this test file (infra/tests/...).
# --------------------------------------------------------------------------- #
_INFRA_DIR = Path(__file__).resolve().parent.parent
_FOUNDATION_DIR = _INFRA_DIR / "platform-foundation"
_PROJECT_TEMPLATE_DIR = _INFRA_DIR / "projects" / "_TEMPLATE"
_COMPUTE_MODULE_DIR = _INFRA_DIR / "modules" / "proxmox-compute"


# --------------------------------------------------------------------------- #
# Lightweight HCL helpers.
#
# We deliberately avoid a full HCL parser (no third-party HCL lib is pinned in
# the venv, and one is not warranted for shape counting). Instead we strip
# comments and count `resource "<type>" "<name>"` block headers with a regex,
# which is robust for the well-formed, project-authored .tf files here.
# --------------------------------------------------------------------------- #

# `resource "TYPE" "NAME" {`  — TYPE and NAME are double-quoted identifiers.
_RESOURCE_HEADER_RE = re.compile(
    r'^\s*resource\s+"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{',
    re.MULTILINE,
)


def _strip_hcl_comments(text: str) -> str:
    """Remove `#`/`//` line comments and `/* ... */` block comments.

    This prevents a commented-out or documented resource type (the .tf files
    are heavily commented and mention every resource type in prose) from being
    miscounted as a real declaration.
    """
    # Block comments first.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    out_lines = []
    for line in text.splitlines():
        # Strip `#` and `//` line comments. Heredocs/regex strings in these
        # files never contain a bare leading `#`/`//` we care about for
        # resource-header counting, so a simple split is safe here.
        no_hash = line.split("#", 1)[0]
        no_slash = no_hash.split("//", 1)[0]
        out_lines.append(no_slash)
    return "\n".join(out_lines)


def _read_root_hcl(root_dir: Path) -> str:
    """Concatenate all *.tf files in a root, with comments stripped."""
    assert root_dir.is_dir(), f"expected Terraform root dir at {root_dir}"
    tf_files = sorted(root_dir.glob("*.tf"))
    assert tf_files, f"no .tf files found in {root_dir}"
    return "\n".join(_strip_hcl_comments(p.read_text()) for p in tf_files)


def _count_resources(hcl: str) -> dict[str, int]:
    """Return a {resource_type: count} map for `resource` blocks in the HCL."""
    counts: dict[str, int] = {}
    for rtype, _rname in _RESOURCE_HEADER_RE.findall(hcl):
        counts[rtype] = counts.get(rtype, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Static HCL inspection tests (primary — always run).
# --------------------------------------------------------------------------- #
class TestFoundationRootShapeStatic:
    """Foundation root: one zone + one applier + exactly one shared-services
    VNet/subnet (the VLAN-20 `p20`); ZERO per-project VNets/subnets (NET-00 §6,
    Req 6.1). Per-project VNets/subnets live in each project's own root."""

    @staticmethod
    @pytest.fixture(scope="class")
    def counts() -> dict[str, int]:
        return _count_resources(_read_root_hcl(_FOUNDATION_DIR))

    def test_exactly_one_sdn_zone_vlan(self, counts):
        assert counts.get("proxmox_sdn_zone_vlan", 0) == 1, (
            "foundation root must declare exactly one proxmox_sdn_zone_vlan "
            f"(Req 4.1); found {counts.get('proxmox_sdn_zone_vlan', 0)}"
        )

    def test_exactly_one_sdn_applier(self, counts):
        assert counts.get("proxmox_sdn_applier", 0) == 1, (
            "foundation root must declare exactly one proxmox_sdn_applier "
            f"(Req 4.2); found {counts.get('proxmox_sdn_applier', 0)}"
        )

    def test_exactly_one_shared_services_vnet_in_foundation(self, counts):
        # NET-00 §6: the foundation root owns exactly ONE VNet — the shared-
        # services VLAN-20 VNet (`p20`, tag 20). Per-project VNets are NOT here;
        # they live in each project's own root (VLAN 100–254). So the foundation
        # count is 1 (the shared VNet), never 0 and never >1.
        assert counts.get("proxmox_sdn_vnet", 0) == 1, (
            "foundation root must declare exactly one proxmox_sdn_vnet — the "
            "shared-services VLAN-20 VNet `p20` (NET-00 §6); per-project VNets "
            "are per-project-root-owned, not here. "
            f"found {counts.get('proxmox_sdn_vnet', 0)}"
        )
        # The single foundation VNet must be the literal shared-services `p20`
        # (tag 20), NOT a per-project interpolated id — robust to whitespace.
        vnet_block = _extract_resource_block(
            _read_root_hcl(_FOUNDATION_DIR), "proxmox_sdn_vnet"
        )
        assert vnet_block is not None, "could not locate the vnet resource block"
        assert re.search(r'^\s*id\s*=\s*"p20"', vnet_block, re.MULTILINE), (
            "the foundation VNet must be the literal shared-services id \"p20\" "
            "(NET-00 §6), not a per-project interpolated id"
        )
        assert re.search(r"^\s*tag\s*=\s*20\b", vnet_block, re.MULTILINE), (
            "the foundation shared-services VNet must carry VLAN tag 20 (NET-00 §6)"
        )

    def test_exactly_one_shared_services_subnet_in_foundation(self, counts):
        # NET-00 §6: the foundation root owns exactly ONE subnet — the shared-
        # services subnet 10.0.20.0/24 (gw 10.0.20.1). Per-project subnets are
        # per-project-root-owned (VLAN 100–254), not here.
        assert counts.get("proxmox_sdn_subnet", 0) == 1, (
            "foundation root must declare exactly one proxmox_sdn_subnet — the "
            "shared-services VLAN-20 subnet 10.0.20.0/24 (NET-00 §6); per-project "
            "subnets are per-project-root-owned, not here. "
            f"found {counts.get('proxmox_sdn_subnet', 0)}"
        )
        # The single foundation subnet must be the literal shared-services CIDR,
        # NOT a per-project 10.0.${...}.0/24 interpolation — robust to whitespace.
        subnet_block = _extract_resource_block(
            _read_root_hcl(_FOUNDATION_DIR), "proxmox_sdn_subnet"
        )
        assert subnet_block is not None, "could not locate the subnet resource block"
        assert re.search(r'^\s*cidr\s*=\s*"10\.0\.20\.0/24"', subnet_block, re.MULTILINE), (
            "the foundation subnet must be the literal shared-services CIDR "
            "\"10.0.20.0/24\" (NET-00 §6), not a per-project interpolated CIDR"
        )

    def test_zone_type_is_vlan_never_vxlan_evpn_bridge(self):
        """Zone type is VLAN — never VXLAN/EVPN/manual bridge (Req 4.5)."""
        hcl = _read_root_hcl(_FOUNDATION_DIR)
        assert 'resource "proxmox_sdn_zone_vlan"' in hcl, (
            "foundation zone must be the VLAN zone type (proxmox_sdn_zone_vlan)"
        )
        for forbidden in (
            "proxmox_sdn_zone_vxlan",
            "proxmox_sdn_zone_evpn",
            "proxmox_sdn_zone_qinq",
            "proxmox_sdn_zone_simple",
        ):
            assert forbidden not in hcl, (
                f"foundation root must NOT use {forbidden} — VLAN zone only (Req 4.5)"
            )

    def test_zone_has_no_ipam_attribute(self):
        """SDN built-in IPAM/DHCP disabled: no `ipam` attr on the zone (Req 2.5)."""
        hcl = _read_root_hcl(_FOUNDATION_DIR)
        zone_block = _extract_resource_block(hcl, "proxmox_sdn_zone_vlan")
        assert zone_block is not None, "could not locate the zone resource block"
        assert not re.search(r"^\s*ipam\s*=", zone_block, re.MULTILINE), (
            "zone must NOT set an `ipam` attribute — SDN built-in IPAM is "
            "deliberately unused; host IPs come from cloud-init (Req 2.5)"
        )


class TestProjectRootShapeStatic:
    """Project root: one VNet + one subnet + own applier, no zone (Req 6.2)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def counts() -> dict[str, int]:
        return _count_resources(_read_root_hcl(_PROJECT_TEMPLATE_DIR))

    def test_exactly_one_vnet(self, counts):
        assert counts.get("proxmox_sdn_vnet", 0) == 1, (
            "project root must declare exactly one proxmox_sdn_vnet "
            f"(Req 4.3); found {counts.get('proxmox_sdn_vnet', 0)}"
        )

    def test_exactly_one_subnet(self, counts):
        assert counts.get("proxmox_sdn_subnet", 0) == 1, (
            "project root must declare exactly one proxmox_sdn_subnet "
            f"(Req 4.4); found {counts.get('proxmox_sdn_subnet', 0)}"
        )

    def test_exactly_one_own_applier(self, counts):
        assert counts.get("proxmox_sdn_applier", 0) == 1, (
            "project root must declare exactly one (its own) proxmox_sdn_applier "
            f"(Req 6.2, approach B); found {counts.get('proxmox_sdn_applier', 0)}"
        )

    def test_no_zone_in_project_root(self, counts):
        assert counts.get("proxmox_sdn_zone_vlan", 0) == 0, (
            "project root must NOT declare the zone — it is foundation-owned "
            f"(Req 6.2); found {counts.get('proxmox_sdn_zone_vlan', 0)}"
        )

    def test_subnet_cidr_matches_formula(self):
        """Subnet CIDR is 10.0.<vlan_id>.0/24 (Req 4.4)."""
        hcl = _read_root_hcl(_PROJECT_TEMPLATE_DIR)
        # The CIDR is built from local.vlan_id: "10.0.${local.vlan_id}.0/24".
        assert re.search(
            r'"10\.0\.\$\{\s*local\.vlan_id\s*\}\.0/24"', hcl
        ), (
            "project subnet CIDR must be the formula 10.0.<vlan_id>.0/24 (Req 4.4)"
        )

    def test_subnet_gateway_matches_formula(self):
        """Subnet gateway is 10.0.<vlan_id>.1 (Req 4.4)."""
        hcl = _read_root_hcl(_PROJECT_TEMPLATE_DIR)
        assert re.search(
            r'"10\.0\.\$\{\s*local\.vlan_id\s*\}\.1"', hcl
        ), (
            "project subnet gateway must be the formula 10.0.<vlan_id>.1 (Req 4.4)"
        )

    def test_subnet_binds_cidr_and_gateway(self):
        """The subnet resource actually consumes the CIDR + gateway locals."""
        hcl = _read_root_hcl(_PROJECT_TEMPLATE_DIR)
        subnet_block = _extract_resource_block(hcl, "proxmox_sdn_subnet")
        assert subnet_block is not None, "could not locate the subnet resource block"
        assert re.search(r"^\s*cidr\s*=\s*local\.subnet_cidr", subnet_block, re.MULTILINE), (
            "subnet must set cidr = local.subnet_cidr (Req 4.4)"
        )
        assert re.search(r"^\s*gateway\s*=\s*local\.gateway_ip", subnet_block, re.MULTILINE), (
            "subnet must set gateway = local.gateway_ip (Req 4.4)"
        )

    def test_vnet_tag_is_vlan_id(self):
        """VNet VLAN tag equals the resolved vlan_id (Req 4.3)."""
        hcl = _read_root_hcl(_PROJECT_TEMPLATE_DIR)
        vnet_block = _extract_resource_block(hcl, "proxmox_sdn_vnet")
        assert vnet_block is not None, "could not locate the vnet resource block"
        assert re.search(r"^\s*tag\s*=\s*local\.vlan_id", vnet_block, re.MULTILINE), (
            "vnet must set tag = local.vlan_id so the 802.1Q tag == the VLAN ID (Req 4.3)"
        )

    def test_subnet_has_no_dhcp_attributes(self):
        """SDN built-in DHCP disabled: no dhcp* attr on the subnet (Req 2.5)."""
        hcl = _read_root_hcl(_PROJECT_TEMPLATE_DIR)
        subnet_block = _extract_resource_block(hcl, "proxmox_sdn_subnet")
        assert subnet_block is not None, "could not locate the subnet resource block"
        assert not re.search(r"^\s*dhcp[A-Za-z_]*\s*=", subnet_block, re.MULTILINE), (
            "subnet must NOT set any dhcp* attribute — SDN built-in DHCP is "
            "deliberately disabled; host IPs come from cloud-init (Req 2.5)"
        )


class TestComputeModuleCloudInitStatic:
    """proxmox-compute derives addresses via cloud-init ip_config (Req 2.5)."""

    @staticmethod
    @pytest.fixture(scope="class")
    def hcl() -> str:
        return _read_root_hcl(_COMPUTE_MODULE_DIR)

    def test_ip_config_structure_present(self, hcl):
        assert re.search(r"^\s*ip_config\s*=", hcl, re.MULTILINE), (
            "proxmox-compute must expose a cloud-init ip_config structure (Req 2.5)"
        )

    def test_ip_config_carries_computed_address_and_gateway(self, hcl):
        # ip_config.ipv4 must carry the computed CIDR address and gateway.
        assert re.search(r"address\s*=\s*local\.ipv4_cidr", hcl), (
            "ip_config.ipv4.address must be the Terraform-computed CIDR (Req 2.5)"
        )
        assert re.search(r"gateway\s*=\s*local\.gateway", hcl), (
            "ip_config.ipv4.gateway must be the computed gateway (Req 2.5)"
        )

    def test_address_uses_cidrhost_formula(self, hcl):
        # ipv4_address = cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)
        assert re.search(
            r"cidrhost\(\s*local\.subnet_cidr\s*,\s*10\s*\+\s*var\.host_index\s*\)", hcl
        ), (
            "host IPv4 must be cidrhost(subnet, 10 + host_index) — the NET-00 §3 "
            "formula (Req 2.1, 2.3)"
        )

    def test_no_operator_ip_literal_input(self):
        """No `ip_config`/IP-literal *input variable* — the module computes it (Req 2.4)."""
        variables = (_COMPUTE_MODULE_DIR / "variables.tf")
        assert variables.is_file(), "expected proxmox-compute variables.tf"
        var_hcl = _strip_hcl_comments(variables.read_text())
        var_names = re.findall(r'^\s*variable\s+"([A-Za-z0-9_]+)"', var_hcl, re.MULTILINE)
        assert "ip_config" not in var_names, (
            "proxmox-compute must NOT accept an operator-supplied ip_config/IP "
            "literal input — the address is Terraform-computed (Req 2.4)"
        )
        assert "ipv4_address" not in var_names and "ip_address" not in var_names, (
            "proxmox-compute must NOT accept an operator-supplied IP literal input (Req 2.4)"
        )

    # ----------------------------------------------------------------------- #
    # Shared-symbol propagation guards (spec: terraform-ansible-handoff, task
    # 10, component C1). The handoff feature added a backward-compatible
    # `ssh_public_keys` input + echo output to this shared module so a consuming
    # root can inject operator SSH keys into a guest's root account via the
    # bpg/proxmox `initialization.user_account.keys` mechanism. These positive
    # assertions catch a future regression that removes/renames the input or
    # output, or drops its `list(string)` type / `[]` default (which is what
    # makes the change a strict, backward-compatible no-op). See design.md
    # "Testing Strategy §2 (module input)".
    # ----------------------------------------------------------------------- #
    def test_ssh_public_keys_input_declared(self):
        """proxmox-compute declares `variable "ssh_public_keys"` of
        `list(string)` defaulting to `[]` (terraform-ansible-handoff Req 1.1,
        2.2, C1). The empty-list default is the backward-compatible no-op that
        keeps every existing consumer unchanged."""
        variables = (_COMPUTE_MODULE_DIR / "variables.tf")
        assert variables.is_file(), "expected proxmox-compute variables.tf"
        var_hcl = _strip_hcl_comments(variables.read_text())

        block = _extract_named_block(var_hcl, "variable", "ssh_public_keys")
        assert block is not None, (
            'proxmox-compute must declare a `variable "ssh_public_keys"` '
            "(terraform-ansible-handoff Req 1.1, C1) — not found"
        )
        assert re.search(r"^\s*type\s*=\s*list\(string\)", block, re.MULTILINE), (
            "ssh_public_keys must be typed `list(string)` (C1)"
        )
        assert re.search(r"^\s*default\s*=\s*\[\s*\]", block, re.MULTILINE), (
            "ssh_public_keys must default to `[]` — the backward-compatible "
            "no-op that keeps existing consumers unchanged (Req 1.1, 1.3, C1)"
        )

    def test_ssh_public_keys_output_declared(self):
        """proxmox-compute echoes the input as `output "ssh_public_keys"` whose
        value is `var.ssh_public_keys` (forward-compat echo; the module owns no
        guest resource yet — terraform-ansible-handoff Req 1.1, 2.2, C1)."""
        outputs = (_COMPUTE_MODULE_DIR / "outputs.tf")
        assert outputs.is_file(), "expected proxmox-compute outputs.tf"
        out_hcl = _strip_hcl_comments(outputs.read_text())

        block = _extract_named_block(out_hcl, "output", "ssh_public_keys")
        assert block is not None, (
            'proxmox-compute must declare an `output "ssh_public_keys"` echoing '
            "the input (forward-compat, C1) — not found"
        )
        assert re.search(r"^\s*value\s*=\s*var\.ssh_public_keys", block, re.MULTILINE), (
            "the ssh_public_keys output must echo `var.ssh_public_keys` (C1)"
        )

    def test_new_ssh_input_does_not_trip_no_ip_literal_guard(self):
        """The new `ssh_public_keys` input must not be mistaken for an
        operator-supplied IP-literal input: the Req-2.4 guard is specific to
        `ip_config`/`ipv4_address`/`ip_address`, so `ssh_public_keys` is fine.
        Explicit coexistence assertion so the shared-symbol addition and the
        existing no-IP-literal guard are known to hold together (task 10a)."""
        variables = (_COMPUTE_MODULE_DIR / "variables.tf")
        var_hcl = _strip_hcl_comments(variables.read_text())
        var_names = re.findall(r'^\s*variable\s+"([A-Za-z0-9_]+)"', var_hcl, re.MULTILINE)
        # The new input is present ...
        assert "ssh_public_keys" in var_names, (
            "ssh_public_keys input must be present (C1)"
        )
        # ... and none of the forbidden IP-literal inputs were introduced with it.
        assert not ({"ip_config", "ipv4_address", "ip_address"} & set(var_names)), (
            "the ssh_public_keys addition must NOT reintroduce an operator IP "
            "literal input — the address stays module-computed (Req 2.4)"
        )

    # ----------------------------------------------------------------------- #
    # ssh_public_keys validation-REGEX contract (regression guard).
    #
    # A live `terraform apply` showed the validation regex wrongly REJECTED a
    # valid `ssh-ed25519 AAAA... comment` public key: the `\S+` sat where the
    # SPACE between the type token and the base64 blob actually is. The presence
    # tests above never exercised the regex against a real key, so the bug
    # slipped through. This test extracts the regex string from the validation
    # `condition`, translates the HCL doubled-backslash escaping (`\\S` -> `\S`)
    # so Python `re` sees the same pattern terraform does, compiles it, and
    # asserts it ACCEPTS representative valid public keys and — via the full
    # condition (pattern match AND no "PRIVATE KEY") — REJECTS a PEM private
    # key. This makes the regex a tested contract, not just its presence.
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _extract_ssh_key_validation_regex() -> str:
        r"""Pull the ``regex("...")`` pattern out of the ssh_public_keys
        validation ``condition`` and translate HCL string-literal escaping to
        what Python ``re`` receives.

        In the .tf source the pattern is written with doubled backslashes
        (``\\S``, ``\\s``); an HCL string literal collapses ``\\`` -> ``\``, so
        terraform's ``regex()`` sees ``\S``/``\s``. We reproduce that collapse
        so the Python-compiled pattern is identical to the one terraform
        applies.
        """
        variables = (_COMPUTE_MODULE_DIR / "variables.tf")
        block = _extract_named_block(
            _strip_hcl_comments(variables.read_text()), "variable", "ssh_public_keys"
        )
        assert block is not None, "ssh_public_keys variable block not found"
        # Grab the first regex("...") argument inside the validation condition.
        m = re.search(r'regex\(\s*"((?:[^"\\]|\\.)*)"', block)
        assert m is not None, "could not locate the regex(...) call in the condition"
        raw = m.group(1)
        # Collapse HCL literal escaping: `\\` -> `\` (so `\\S` -> `\S`).
        return raw.replace("\\\\", "\\")

    def test_ssh_key_regex_accepts_valid_public_keys(self):
        """The validation regex ACCEPTS representative valid OpenSSH public keys
        — including an ed25519 key with a comment (the exact case a live apply
        wrongly rejected) and the operator's actual key."""
        pattern = self._extract_ssh_key_validation_regex()
        compiled = re.compile(pattern)
        accepted = [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILOOMPUx6YH0Q45an5SDp9YXoM5UsWUg0Ds8qC7MzP6h user@host",
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILOOMPUx6YH0Q45an5SDp9YXoM5UsWUg0Ds8qC7MzP6h bart.deboeck@xenopz.com",
            "ssh-rsa AAAAB3NzaC1yc2Ea comment",
            "ecdsa-sha2-nistp256 AAAAE2VjZHNh comment",
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILOOMPUx6YH0Q45an5SDp9YXoM5UsWUg0Ds8qC7MzP6h",
        ]
        for key in accepted:
            assert compiled.search(key), (
                f"validation regex must ACCEPT a valid public key, but rejected: {key!r}"
            )

    def test_ssh_key_full_condition_rejects_private_key_and_junk(self):
        """The FULL condition (regex match AND not containing 'PRIVATE KEY')
        REJECTS a PEM private-key header line, a bare type token with no blob,
        and arbitrary junk."""
        pattern = self._extract_ssh_key_validation_regex()
        compiled = re.compile(pattern)

        def full_condition(k: str) -> bool:
            # Mirror the HCL: can(regex(pat, k)) && !can(regex("PRIVATE KEY", k))
            return bool(compiled.search(k)) and ("PRIVATE KEY" not in k)

        rejected = [
            "-----BEGIN OPENSSH PRIVATE KEY-----",
            "not-a-key",
            "ssh-ed25519",
        ]
        for bad in rejected:
            assert not full_condition(bad), (
                f"validation condition must REJECT a non-public-key value: {bad!r}"
            )


def _extract_resource_block(hcl: str, resource_type: str) -> str | None:
    """Return the brace-balanced body of the first `resource "<type>"` block.

    Comment-stripped HCL is expected as input. Returns None if not found.
    """
    header = re.search(
        rf'resource\s+"{re.escape(resource_type)}"\s+"[A-Za-z0-9_]+"\s*\{{',
        hcl,
    )
    if header is None:
        return None
    start = header.end() - 1  # position of the opening brace
    depth = 0
    for i in range(start, len(hcl)):
        c = hcl[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return hcl[start : i + 1]
    return None


def _extract_named_block(hcl: str, block_kw: str, label1: str) -> str | None:
    """Return the brace-balanced body of the first `<block_kw> "<label1>" {`.

    Used for single-label blocks — ``variable`` / ``output`` / ``module``.
    Comment-stripped HCL is expected as input. Returns None if not found.
    (Mirrors the same-named helper in ``test_svc07_terraform_shape.py``, kept
    local here to avoid a cross-test-module import.)
    """
    header = re.search(rf'{block_kw}\s+"{re.escape(label1)}"\s*\{{', hcl)
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
# terraform-json inspection tests (secondary — gated on the toolchain).
#
# These assert the SAME shapes against `terraform show -json` of a plan. They
# are skipped entirely when neither `terraform` nor `tofu` is installed, per the
# documentation-testing steering (absent infra => skipped, not failed).
# --------------------------------------------------------------------------- #
from conftest import offline_plan, requires_terraform, _terraform_binary


@requires_terraform
class TestTerraformJsonShape:
    """Resource-shape assertions via `terraform show -json` of a plan fixture.

    Only runs when a terraform/tofu binary is present. When the binary is absent
    the whole class is skipped (per documentation-testing steering: absent infra
    => skipped, not failed).

    The offline plan is produced by the shared ``offline_plan`` helper
    (``infra/tests/conftest.py``), which runs ``terraform plan`` against a
    transient ``backend "local" {}`` override in a sandbox copy of the roots
    with dummy ``proxmox_endpoint`` / ``proxmox_api_token`` provider vars — so
    no live GitLab HTTP backend / ``CI_JOB_TOKEN`` / Proxmox credentials are
    required (spec: fix-offline-terraform-plan-tests, Change 3).
    """

    def _planned_resource_types(self, root_name: str, *var_args: str) -> list[str]:
        with offline_plan(root_name, *var_args) as result:
            return [c["type"] for c in result.resource_changes]

    def test_foundation_root_shape(self):
        types = self._planned_resource_types("foundation")
        assert types.count("proxmox_sdn_zone_vlan") == 1
        assert types.count("proxmox_sdn_applier") == 1
        # The one VNet + one subnet are the shared-services VLAN-20 `p20`
        # VNet + its 10.0.20.0/24 subnet, foundation-owned (NET-00 §6). NOT a
        # per-project VNet/subnet — those live in each project's own root.
        assert types.count("proxmox_sdn_vnet") == 1
        assert types.count("proxmox_sdn_subnet") == 1

    def test_project_root_shape(self):
        types = self._planned_resource_types(
            "project_template", "-var", "project_slug=mlvideo"
        )
        assert types.count("proxmox_sdn_vnet") == 1
        assert types.count("proxmox_sdn_subnet") == 1
        assert types.count("proxmox_sdn_applier") == 1
        assert types.count("proxmox_sdn_zone_vlan") == 0
