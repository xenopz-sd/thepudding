# infra/projects/_TEMPLATE/sdn.tf
#
# This project root's own SDN fabric — exactly one VNet, one subnet, and this
# root's OWN applier (Requirements 4.3, 4.4, 6.2, 2.5; design.md "Terraform SDN
# resource graph" and "Cross-root applier trade-off (approach B)").
#
# What this root DOES declare:
#   * exactly ONE proxmox_sdn_vnet   — VLAN tag = local.vlan_id (100-254),
#     attached to the single cluster-wide foundation zone (Requirement 4.3).
#   * exactly ONE proxmox_sdn_subnet — CIDR 10.0.<vlan_id>.0/24, gateway
#     10.0.<vlan_id>.1, both derived from the same local.vlan_id (Requirement 4.4).
#   * exactly ONE proxmox_sdn_applier — this project's OWN applier, whose
#     replace_triggered_by watches ONLY this project's VNet + subnet
#     (approach B, Requirement 6.2, design.md "Cross-root applier trade-off").
#
# What this root does NOT declare (owned by the platform-foundation root):
#   * the proxmox_sdn_zone_vlan — this root only references it by id
#     (var.sdn_zone_id), NOT via terraform_remote_state. Approach B deliberately
#     avoids cross-root state coupling: the zone id is a stable cluster-wide
#     constant, so passing it as a plain input variable keeps this project's
#     plan independent of the foundation root's state (design.md rejected
#     approach A for re-introducing cross-project blast radius).
#   * the foundation-level applier (which watches the zone + registry hash).
#
# SDN built-in IPAM/DHCP is left disabled: no `ipam` on the zone (foundation
# root) and no dhcp_* / dhcp_range attributes on the subnet here. Host IPs are
# computed by Terraform and injected via cloud-init in the proxmox-compute
# module (Requirement 2.5, NET-00 §3).
#
# ---------------------------------------------------------------------------
# PRECONDITION — `SDN.Allocate` privilege on `terraform@pve` (Requirement 7.1-7.3)
# ---------------------------------------------------------------------------
# Creating the VNet / subnet / applier declared BELOW requires the Terraform
# service account's custom PVE role (`terraform@pve`) to additionally carry the
# `SDN.Allocate` privilege (NET-00 §6, security-standards "Least-privilege service
# accounts"). That grant is a Platform-Foundation-owned dependency and a
# PRECONDITION for this root, NOT a resource of it:
#   * this root does NOT declare, re-grant, or modify the `terraform@pve` role
#     or the `SDN.Allocate` privilege in any `proxmox_*_role` / `proxmox_*_acl`
#     resource — forbidden by Requirement 7.3;
#   * if the privilege is missing when `terraform apply` reaches the SDN API,
#     the Proxmox authorization failure surfaces and NO VNet/subnet is reported
#     as provisioned (Requirement 7.2).
# The degraded-mode behaviour (missing `SDN.Allocate`) is exercised by
# infra/tests/test_degraded_sdn_use.py.
#
# ---------------------------------------------------------------------------
# OBSERVABILITY — VNet/subnet/applier apply failures SURFACE (Req 4.7, FD.1)
# ---------------------------------------------------------------------------
# If creation of this project's VNet or subnet fails, or this root's applier
# fails to commit the SDN change cluster-wide, `terraform apply` exits NON-ZERO
# and the affected resource is NOT reported as provisioned (Req 4.7, FD.1).
# There is deliberately no `ignore_errors`, no `|| true`, and no best-effort
# local-exec wrapper here that could mask a failed commit as success. The
# applier below is a tracked resource whose replace_triggered_by watches this
# project's own VNet + subnet, so the commit is always a real, fail-surfacing
# apply step — never a silent side effect. This surfacing is exercised in the
# degraded-mode suite (infra/tests/test_degraded_modes.py, "Applier failure").
#
# local.vlan_id comes from main.tf (registry lookup for var.project_slug). The
# terraform_data.assert_registered precondition in main.tf already guarantees it
# is non-null before any resource here is created; the extra 100-254 guard below
# defends the SDN-specific range constraint (Requirement 4.3).

variable "sdn_zone_id" {
  description = <<-EOT
    Identifier of the single cluster-wide SDN VLAN zone that this project's VNet
    attaches to. Owned and created by the platform-foundation root
    (infra/platform-foundation/sdn.tf, var sdn_zone_id, default "vlanzone").
    Passed here as a plain input variable rather than read via
    terraform_remote_state, so this project root's plan stays decoupled from the
    foundation root's state (approach B, design.md "Cross-root applier
    trade-off"). Must match the foundation zone's id exactly, or the VNet apply
    will fail with an unknown-zone error at the Proxmox API.
  EOT
  type        = string
  default     = "vlanzone"

  validation {
    condition     = can(regex("^[A-Za-z0-9]{1,8}$", var.sdn_zone_id))
    error_message = "sdn_zone_id must be 1-8 alphanumeric characters (Proxmox SDN zone ID constraint), matching the foundation root's zone."
  }
}

locals {
  # The project's VLAN ID, resolved from the registry in main.tf. Constrained to
  # the per-project range 100-254 by the precondition below (Requirement 4.3);
  # the CIDR/gateway formula (Requirement 4.4) reuses this same value so the
  # VNet tag and subnet always agree.
  subnet_cidr = "10.0.${local.vlan_id}.0/24"
  gateway_ip  = "10.0.${local.vlan_id}.1"

  # Proxmox SDN VNet IDs must start with a letter, be alphanumeric only (no
  # hyphens), and are at most 8 characters. `p<vlan_id>` (e.g. "p100") satisfies
  # all three for any vlan_id in 100-254 and is deterministically derivable from
  # the VLAN ID by inspection, matching the platform's "no human chooses an
  # identifier" principle (NET-00 §3-§4).
  vnet_id = "p${local.vlan_id}"
}

# ---------------------------------------------------------------------------
# Exactly one VNet for this project's VLAN (Requirement 4.3).
#
# `tag` carries the VLAN tag; on a VLAN-type zone this is the 802.1Q VLAN ID.
# `zone` references the foundation zone by id (var.sdn_zone_id), NOT via remote
# state (approach B). The 100-254 guard is enforced by the precondition below.
# ---------------------------------------------------------------------------
resource "proxmox_sdn_vnet" "project" {
  id   = local.vnet_id
  zone = var.sdn_zone_id
  tag  = local.vlan_id

  lifecycle {
    precondition {
      # Per-project VLAN IDs are 100-254 under the single-octet
      # 10.0.<vlan_id>.0/24 formula (Requirements 4.3, 9.1, NET-00 §2-§3). The
      # management VLAN (10) and shared-services VLAN (20) are never assigned to
      # a project root; the reserved buffer is 30-99. A registry entry outside
      # 100-254 for a project is a data error surfaced here before any SDN
      # object is created.
      condition     = local.vlan_id != null && local.vlan_id >= 100 && local.vlan_id <= 254
      error_message = "vlan_id for slug '${var.project_slug}' resolved to '${local.vlan_id == null ? "null" : tostring(local.vlan_id)}', which is outside the per-project range 100-254 (NET-00 §2-§3, Requirement 4.3). Check projects.yaml."
    }
  }

  depends_on = [
    # The registry-membership guard from main.tf must pass before we attempt to
    # create the VNet.
    terraform_data.assert_registered,
  ]
}

# ---------------------------------------------------------------------------
# Exactly one subnet for this project's VLAN (Requirement 4.4).
#
# CIDR 10.0.<vlan_id>.0/24 and gateway 10.0.<vlan_id>.1, both derived from the
# same local.vlan_id as the VNet above. No DHCP attributes (dhcp_dns_server,
# dhcp_range, ...) and no SNAT are set: the SDN built-in IPAM/DHCP is
# deliberately left disabled (Requirement 2.5, NET-00 §5). Host addresses are
# computed by Terraform and injected via cloud-init in proxmox-compute.
# ---------------------------------------------------------------------------
resource "proxmox_sdn_subnet" "project" {
  vnet    = proxmox_sdn_vnet.project.id
  cidr    = local.subnet_cidr
  gateway = local.gateway_ip
}

# ---------------------------------------------------------------------------
# This project root's OWN SDN applier (Requirement 6.2, approach B).
#
# replace_triggered_by watches ONLY this project's VNet + subnet — never the
# zone (owned by the foundation root) and never any other project's resources.
# This keeps each project's SDN commit scoped to its own plan: a change to this
# project re-triggers only this applier, and a change to another project or to
# the foundation zone does not force a plan here (design.md "Cross-root applier
# trade-off", chosen approach B; rejected approach A because a shared foundation
# applier watching all projects via remote state re-couples cross-project blast
# radius).
#
# Accepted trade-off (design.md, recorded open risk): because Proxmox's SDN
# apply is a cluster-global commit, this applier still flushes ALL pending SDN
# changes when it runs, not just this project's. In practice pending changes are
# only the object just created/changed in the same apply; concurrent
# cross-project applies are serialized by per-root GitLab state locks and are
# not expected at this scale (PF §6).
# ---------------------------------------------------------------------------
resource "proxmox_sdn_applier" "project" {
  lifecycle {
    replace_triggered_by = [
      proxmox_sdn_vnet.project,
      proxmox_sdn_subnet.project,
    ]
  }

  depends_on = [
    proxmox_sdn_vnet.project,
    proxmox_sdn_subnet.project,
  ]
}
