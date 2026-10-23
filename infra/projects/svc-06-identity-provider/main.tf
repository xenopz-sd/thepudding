# SVC-06 Identity Provider (ZITADEL) Terraform root — the ZITADEL LXC.
#
# This root declares EXACTLY ONE `proxmox_virtual_environment_container` (the
# ZITADEL application LXC) and NO database container. The backing PostgreSQL is a
# SEPARATE SVC-01 instance (`project_slug=shared-zitadel`) with its own isolated
# state `shared-zitadel-svc01-postgres`, provisioned by the installer's
# svc06_provision role as a child svc-01-bootstrap run (design.md §3.3). Keeping
# the DB out of this root preserves SVC-01's per-instance state isolation
# (integration-boundaries §5).
#
# The container's addressing/hostname/tags are produced by the shared
# `proxmox-compute` module, CONSUMED UNMODIFIED (svc_code=svc06, vlan_id=20,
# host_index -> computed static IPv4 10.0.20.1X, hostname svc06-20-01, tags
# ["proj-shared-zitadel"]). No operator IP literal is used anywhere (NET-00 §3).

# ===========================================================================
# Host-index ledger read (the Host_Index_Authority) — design §5.1
# ===========================================================================
# Read the committed per-VLAN host-index ledger
# (infra/platform-foundation/host-indices.yaml) the same way the per-project
# roots read projects.yaml (yamldecode(file(...)) — see _TEMPLATE/main.tf). This
# is read-only INPUT, never Terraform state; one state per service is preserved
# (integration-boundaries §5). The ledger is the single source of host-index
# truth across the SEPARATE Terraform roots that populate VLAN 20, so a
# within-VLAN host-index collision across roots is caught at plan time by the
# proxmox-compute Guard-4 precondition fed by `peer_host_indices` below.
#
# The formula is UNCHANGED: IP = cidrhost("10.0.20.0/24", 10 + host_index). This
# block changes only HOW host_index is made unique across roots.
locals {
  # Parse the committed ledger. Relative path walks back from this per-service
  # root (infra/projects/<slug>/) to the platform-foundation root.
  host_index_ledger = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))

  # This guest's VLAN (20, shared-services) and its ledger identity. The ZITADEL
  # guest is svc_code=svc06 with no component (its sole/primary guest).
  zitadel_vlan_id = 20

  # All host indices allocated on VLAN 20 per the ledger (both allocated and
  # reserved rows count as occupants — a reserved index must not be re-picked).
  vlan20_all_indices = [for e in local.host_index_ledger[local.zitadel_vlan_id] : e.host_index]

  # The ZITADEL guest's own assigned index, resolved from the ledger (svc06, no
  # component). `one()` returns the sole match (or raises on a duplicate, which
  # the append-only/diff-reviewed ledger prevents). Falls back to the
  # var.zitadel_host_index default only if the ledger has no svc06 row.
  zitadel_ledger_index = one([
    for e in local.host_index_ledger[local.zitadel_vlan_id] :
    e.host_index if e.svc_code == "svc06" && !can(e.component)
  ])
  zitadel_host_index = local.zitadel_ledger_index != null ? local.zitadel_ledger_index : var.zitadel_host_index

  # Peer-set view (design §Data Models): every OTHER host_index on VLAN 20 — the
  # full set minus this guest's own index — threaded into proxmox-compute's
  # peer_host_indices precondition (Guard 4). Self-exclusion means a guest is
  # never its own peer, so the precondition can only fire on a genuine
  # cross-guest collision.
  zitadel_peer_host_indices = [
    for i in local.vlan20_all_indices : i if i != local.zitadel_host_index
  ]
}

# Addressing/hostname/tags for the ZITADEL guest, computed by the shared module.
# svc_code=svc06, vlan_id=20, host_index 5 (sourced from the ledger) -> ipv4
# 10.0.20.15 (cidrhost("10.0.20.0/24", 10 + 5)), hostname "svc06-20-01", tags
# ["proj-shared-zitadel"] (PF §4; NET-00 §3-§4). No `component` -> the plain
# <svc-code>-<vlan_id>-<instance> hostname form. `peer_host_indices` is the
# VLAN-20 peer set from the ledger (design §5.1) so a cross-root host-index
# collision fails at plan time (proxmox-compute Guard 4).
module "zitadel" {
  source = "../../modules/proxmox-compute"

  project_slug      = "shared"                        # shared platform service -> tag proj-shared-zitadel
  service_name      = "zitadel"                       # -> tags ["proj-shared-zitadel"] (PF §4)
  svc_code          = "svc06"                         # -> hostname svc06-20-01 (NET-00 §4)
  vlan_id           = local.zitadel_vlan_id           # shared-services VLAN 20 (domain-rules §1)
  host_index        = local.zitadel_host_index        # ledger-sourced 5 -> 10.0.20.15 (distinct from the DB's host_index 4)
  peer_host_indices = local.zitadel_peer_host_indices # VLAN-20 peers from the ledger (Guard 4, design §5.1)
  instance          = "01"                            # singleton -> svc06-20-01

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the guest resource below
}

# The ZITADEL application container. The ONE LXC this root owns. Unprivileged,
# 2 vCPU / 2048 MB / 10 GB rootfs on the VLAN-20 SDN VNet, stock Debian 12 /
# Ubuntu 24.04 LTS template (Docker + Compose installed per host by the common
# role at provision time, ADR-0003). ZITADEL is HTTP-exposed ONLY via Traefik
# (SVC-09) — no published host port and no Traefik label is declared here; those
# belong to the Compose/Ansible layer (zitadel-server role).
resource "proxmox_virtual_environment_container" "zitadel" {
  description = "SVC-06 ZITADEL identity provider (OIDC/SAML SSO, PostgreSQL-backed). Managed by Terraform; see .kiro/specs/svc-06-identity-provider."

  node_name = var.proxmox_node_name
  vm_id     = var.zitadel_vmid

  # Unprivileged container (PF LXC-default). `nesting` is required so the
  # in-container Docker engine (which the Compose stack needs) runs inside an
  # unprivileged LXC.
  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # 2 vCPU (requirements §5 sizing)
  }

  # Memory sizing + swap lockdown. `swap = 0` is the provider-native per-container
  # swap cap (ADR-0006 defence-in-depth) — ZITADEL's masterkey lives in RAM, so a
  # swap device is undesirable; the host-layer control is encrypted host swap.
  memory {
    dedicated = 2048 # 2048 MB RAM (requirements §5 sizing)
    swap      = 0    # no swap device (ADR-0006)
  }

  # Root filesystem: 10 GB (requirements §5 sizing). ZITADEL's data lives in its
  # backing Postgres, not this rootfs, so 10 GB is ample.
  disk {
    datastore_id = var.zitadel_datastore_id
    size         = 10
  }

  operating_system {
    template_file_id = var.zitadel_template_file_id
    type             = var.zitadel_template_os_type # debian | ubuntu
  }

  # Hostname + computed static IPv4 (10.0.20.15) injected from the module — never
  # an operator IP literal (NET-00 §3).
  initialization {
    hostname = module.zitadel.hostname # svc06-20-01

    ip_config {
      ipv4 {
        address = module.zitadel.ip_config.ipv4.address # 10.0.20.15/24 (computed)
        gateway = module.zitadel.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys; empty list => block omitted => strict no-op
      }
    }
  }

  # Attach to the VLAN-20 shared-services SDN VNet `p20` (platform-foundation-
  # owned precondition — referenced, not created here). `p20` is a VLAN-zone VNet
  # and applies tag 20 itself, so the guest NIC must NOT also 802.1Q-tag onto
  # VLAN 20 (no `vlan_id` here), or Proxmox rejects the start with "vm vlans are
  # not allowed on vnet p20". The static address comes solely from cloud-init;
  # SDN built-in IPAM/DHCP stays disabled (NET-00 §5).
  network_interface {
    name   = "veth0"
    bridge = "p20"
  }

  # Human-facing Proxmox tag proj-shared-zitadel, from the module. Distinct from
  # the machine-facing hostname above.
  tags = module.zitadel.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: ZITADEL holds masterkey-encrypted identity state and is a hard
    # dependency for platform-wide login. It must NEVER be silently destroyed/
    # replaced by an apply. `prevent_destroy` HARD-ERRORS on any plan that would
    # destroy or replace this resource; a deliberate replacement requires an
    # operator to consciously remove this block (see the SVC-06 INSTALL-RUNBOOK
    # destructive-plan guardrail).
    prevent_destroy = true
  }
}
