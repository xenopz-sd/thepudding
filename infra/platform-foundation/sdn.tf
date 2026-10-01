# infra/platform-foundation/sdn.tf
#
# The cluster-wide, non-per-project SDN fabric owned by the platform-foundation
# root (Requirement 6.1, NET-00 §6, design.md "Terraform SDN resource graph"):
#
#   * exactly ONE proxmox_sdn_zone_vlan  — the single VLAN-type zone that every
#     project and shared-tier VNet attaches to (Requirement 4.1, 4.5).
#   * exactly ONE proxmox_sdn_applier    — commits pending SDN changes
#     cluster-wide, re-triggered whenever the zone or the VLAN-ID registry
#     changes (Requirement 4.2, design.md "Cross-root applier trade-off",
#     approach B).
#   * exactly ONE shared-services VNet + subnet — the VLAN-20 VNet (`p20`,
#     tag 20) and its subnet (10.0.20.0/24, gw 10.0.20.1) for the shared
#     platform-services tier (NET-00 §2, §6). VLAN 20 is shared and
#     cluster-wide — every "Shared"-tenancy service (SVC-06/07/09/13/14/17-18)
#     attaches to it — so, unlike a per-project VNet, it is foundation-owned.
#     It is FIXED here (not resolved from projects.yaml): the registry and the
#     per-project VNet mechanism in infra/projects/_TEMPLATE/sdn.tf are
#     deliberately limited to VLAN 100–254, so the shared VLAN-20 VNet has no
#     per-project owner and must live in this root beside the zone/applier.
#
# It declares ZERO PER-PROJECT VNets and ZERO PER-PROJECT subnets: those live
# in each project's own root under infra/projects/<slug>/ so one project's
# `terraform destroy` can never touch shared or other-project resources
# (Requirement 6.1, 6.2, PF §4). The single SHARED-SERVICES VLAN-20 VNet/subnet
# declared below is the deliberate exception — it is cluster-wide shared
# infrastructure, not a project's own, so it belongs to the foundation root.
#
# Zone type is VLAN — never VXLAN/EVPN and never a hand-edited VLAN-aware Linux
# bridge (Requirement 4.5, NET-00 §5). The SDN built-in IPAM/DHCP is left
# unset/disabled: host IPs are computed by Terraform and injected via cloud-init
# (Requirement 2.5, NET-00 §3), so no `ipam` attribute is configured here.
#
# ---------------------------------------------------------------------------
# PRECONDITION — `SDN.Allocate` privilege on `terraform@pve` (Requirement 7.1-7.3)
# ---------------------------------------------------------------------------
# Creating and managing the SDN zone / applier declared BELOW (and the VNets /
# subnets in each project root) requires the Terraform service account's custom
# PVE role (`terraform@pve`) to additionally carry the `SDN.Allocate` privilege
# (NET-00 §6, security-standards "Least-privilege service accounts").
#
# That grant is a Platform-Foundation-owned dependency. It is a PRECONDITION for
# this root, NOT a resource of it:
#   * this root does NOT declare, re-grant, or modify the `terraform@pve` role
#     or the `SDN.Allocate` privilege in any `proxmox_*_role` / `proxmox_*_acl`
#     resource — doing so is explicitly forbidden by Requirement 7.3;
#   * if the privilege is missing when a `terraform apply` reaches the SDN API,
#     the Proxmox authorization failure surfaces and NO SDN resource is reported
#     as provisioned (Requirement 7.2) — a clear signal, never a silent success.
# The degraded-mode behaviour (missing `SDN.Allocate`) is exercised by
# infra/tests/test_degraded_sdn_use.py.
#
# ---------------------------------------------------------------------------
# OBSERVABILITY — apply failures SURFACE, they are never swallowed (Req 4.7, FD.1)
# ---------------------------------------------------------------------------
# There is no CI wrapper, `ignore_errors`, `|| true`, or best-effort local-exec
# anywhere in this root that could mask a failed SDN commit as success. The
# surfacing is inherent to `terraform apply`:
#   * a failed zone create/update, or a failed `proxmox_sdn_applier` commit,
#     makes `terraform apply` exit NON-ZERO and marks the resource tainted /
#     not-created in state — it is never reported as provisioned (Req 4.7, FD.1);
#   * the applier below is a first-class, tracked resource (not a fire-and-forget
#     provisioner), so its cluster-wide SDN commit is part of the plan graph and
#     a commit failure fails the apply rather than being silently skipped;
#   * `replace_triggered_by` guarantees the applier re-runs whenever the zone or
#     the registry hash changes, so a change can never be "committed on paper"
#     without an actual applier run that can fail loudly.
# The only degraded mode that is intentionally NOT a Terraform error is the
# out-of-band physical trunk (NET-00 §8): Terraform has no visibility into the
# switch, so a correct apply plus a guest-connectivity failure is the documented
# signature of an unconfigured trunk, not a masked apply failure (FD.4).

variable "sdn_zone_id" {
  description = <<-EOT
    Identifier for the single cluster-wide SDN VLAN zone. Proxmox constrains SDN
    zone IDs to at most 8 alphanumeric characters. Kept as a variable (not
    hardcoded) so the operator can align it with local naming, but it is a
    stable cluster-wide value — not a per-project input.
  EOT
  type        = string
  default     = "vlanzone"

  validation {
    condition     = can(regex("^[A-Za-z0-9]{1,8}$", var.sdn_zone_id))
    error_message = "sdn_zone_id must be 1-8 alphanumeric characters (Proxmox SDN zone ID constraint)."
  }
}

variable "sdn_zone_bridge" {
  description = <<-EOT
    The local Linux or OVS bridge, already configured identically on every
    Proxmox node, that this VLAN zone tags onto (bpg/proxmox
    proxmox_sdn_zone_vlan requires it; NET-00 §5). This bridge and the physical
    trunk feeding it are an out-of-band precondition (NET-00 §8) — this root
    only references the bridge, it does not create it.
  EOT
  type        = string
  default     = "vmbr0"
}

variable "sdn_zone_nodes" {
  description = <<-EOT
    Optional set of Proxmox node names the zone (and attached VNets) deploy on.
    Leave empty to apply cluster-wide to all nodes, which is the intended
    behaviour for a single shared VLAN fabric.
  EOT
  type        = set(string)
  default     = []
}

variable "sdn_zone_mtu" {
  description = "Optional MTU for the VLAN zone. Null leaves it at the Proxmox default (note: the provider cannot reset MTU back to default once set)."
  type        = number
  default     = null
}

# ---------------------------------------------------------------------------
# The single cluster-wide SDN VLAN zone (Requirement 4.1, 4.5).
# ---------------------------------------------------------------------------
resource "proxmox_sdn_zone_vlan" "foundation" {
  id     = var.sdn_zone_id
  bridge = var.sdn_zone_bridge

  # Empty set => omit `nodes` so the zone applies to all cluster nodes.
  nodes = length(var.sdn_zone_nodes) > 0 ? var.sdn_zone_nodes : null

  mtu = var.sdn_zone_mtu

  # Intentionally NOT setting `ipam`: the SDN built-in IPAM/DHCP is tech-preview
  # and deliberately unused (Requirement 2.5, NET-00 §5). Host IPs are computed
  # by Terraform and injected via cloud-init in the proxmox-compute module.
}

# ---------------------------------------------------------------------------
# The single shared-services VLAN-20 VNet + subnet (NET-00 §2, §6).
#
# VLAN 20 is the SHARED platform-services tier: every "Shared"-tenancy service
# (SVC-06 ZITADEL, SVC-07 OpenBao, SVC-09 Traefik, SVC-13 CI runners, SVC-14
# Zot, SVC-17/18 observability, ...) attaches its guests to `p20`. Unlike a
# per-project VNet, this VNet is cluster-wide shared infrastructure created
# once — the same remit as the zone above — so it is FOUNDATION-owned here.
#
# It is FIXED (id `p20`, tag 20), NOT resolved from the projects.yaml registry:
# the registry and the per-project VNet mechanism in
# infra/projects/_TEMPLATE/sdn.tf are deliberately limited to VLAN 100–254
# (their precondition guards that range), so the shared VLAN-20 VNet has no
# per-project owner and must live in this root. Attaching a guest to a VNet
# that no root creates is exactly what fails at container start with
# `bridge 'p20' does not exist`.
#
# `tag` carries the 802.1Q VLAN ID (20) on the VLAN-type zone; `zone`
# references the foundation zone created above by its id. No `ipam`/DHCP/SNAT
# attributes: SDN built-in IPAM stays disabled (Requirement 2.5, NET-00 §5),
# host IPs are computed by Terraform and injected via cloud-init.
# ---------------------------------------------------------------------------
resource "proxmox_sdn_vnet" "shared_services" {
  id   = "p20"
  zone = proxmox_sdn_zone_vlan.foundation.id
  tag  = 20

  depends_on = [
    proxmox_sdn_zone_vlan.foundation,
  ]
}

resource "proxmox_sdn_subnet" "shared_services" {
  vnet    = proxmox_sdn_vnet.shared_services.id
  cidr    = "10.0.20.0/24"
  gateway = "10.0.20.1"
}

# ---------------------------------------------------------------------------
# Registry-hash change detector.
#
# `replace_triggered_by` may only reference managed resources or their
# attributes, not a bare local value. To let the foundation applier re-commit
# whenever projects.yaml changes (approach B: the foundation applier watches the
# zone + the registry hash), we track local.registry_hash (from registry.tf) on
# a lightweight terraform_data resource and point replace_triggered_by at it.
# ---------------------------------------------------------------------------
resource "terraform_data" "registry_watch" {
  # Replaced whenever the registry content hash changes; carries no
  # infrastructure side effect of its own.
  input = local.registry_hash
}

# ---------------------------------------------------------------------------
# The single cluster-wide SDN applier (Requirement 4.2).
#
# Approach B (design.md "Cross-root applier trade-off"): the FOUNDATION applier
# watches only the zone and the registry hash. Each project root declares its
# OWN applier that watches only that project's VNet + subnet, so a project
# change never forces a foundation apply and cross-project blast radius is
# avoided. Because Proxmox's SDN apply is a global commit, this applier still
# flushes all pending cluster-wide SDN changes when it runs — the accepted
# trade-off recorded as an open risk in design.md.
# ---------------------------------------------------------------------------
resource "proxmox_sdn_applier" "foundation" {
  lifecycle {
    replace_triggered_by = [
      proxmox_sdn_zone_vlan.foundation,
      # The shared-services VLAN-20 VNet/subnet are foundation-owned, so the
      # foundation applier commits them too: adding them here keeps the
      # cluster-wide commit correct whenever the shared VNet/subnet change.
      proxmox_sdn_vnet.shared_services,
      proxmox_sdn_subnet.shared_services,
      terraform_data.registry_watch,
    ]
  }

  depends_on = [
    proxmox_sdn_zone_vlan.foundation,
    proxmox_sdn_vnet.shared_services,
    proxmox_sdn_subnet.shared_services,
  ]
}
