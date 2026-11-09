# Shared dev mail catcher (Mailpit) — one unprivileged LXC on the shared-services
# VLAN 20. Spec: .kiro/specs/dev-mail-catcher, ADR-0025.
#
# IDENTITY (ADR-0021): there is no catalog SVC-NN slot for a mail sink, so the
# guest is identified by the NON-CATALOG `role_code = "mailcatcher"` (exactly one
# of svc_code / role_code), giving hostname `mailcatcher-20-01`. The ledger row is
# keyed on role_code too, so every ledger read here uses key-safe `lookup()`.

locals {
  # The Host_Index_Authority ledger (ADR-0015): read-only input, never state.
  _host_index_ledger   = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))
  _vlan_id             = 20
  _vlan20_entries      = local._host_index_ledger[local._vlan_id]
  _vlan20_host_indices = [for e in local._vlan20_entries : e.host_index]

  # This guest's ledger index (role_code mailcatcher, no component). Asserted
  # below to equal the explicit literal so root/ledger drift fails the plan.
  _mailcatcher_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if lookup(e, "role_code", null) == "mailcatcher" && lookup(e, "component", null) == null
  ])

  mailcatcher_host_index = 11 # -> 10.0.20.21 (cidrhost("10.0.20.0/24", 10 + 11))

  # Every OTHER VLAN-20 index; fed to the module's Guard 4 (within-VLAN collision).
  mailcatcher_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.mailcatcher_host_index
  ]
}

module "mailcatcher" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"      # shared platform service -> tag proj-shared-mailcatcher
  service_name = "mailcatcher" # -> tags ["proj-shared-mailcatcher"] (PF §4); also the inventory service_role
  role_code    = "mailcatcher" # NON-catalog identity (ADR-0021) -> hostname mailcatcher-20-01
  vlan_id      = local._vlan_id
  host_index   = local.mailcatcher_host_index
  instance     = "01"

  peer_host_indices = local.mailcatcher_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the container resource below
}

# Ledger/root drift guard (a `module` block cannot carry a lifecycle
# precondition, so this no-side-effect anchor hosts the cross-check).
resource "terraform_data" "ledger_index_assert" {
  input = {
    mailcatcher_host_index   = local.mailcatcher_host_index
    mailcatcher_ledger_index = local._mailcatcher_ledger_index
  }

  lifecycle {
    precondition {
      condition     = local.mailcatcher_host_index == local._mailcatcher_ledger_index
      error_message = "Mail catcher host_index (${local.mailcatcher_host_index}) does not match the host-indices.yaml ledger entry for role_code mailcatcher on VLAN 20 (${local._mailcatcher_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
  }
}

# The catcher container. Stateless and disposable (captured mail is dev-only), so
# NO prevent_destroy. Unprivileged, 1 vCPU / 512 MB / 4 GB, no swap (ADR-0006
# posture kept uniform).
resource "proxmox_virtual_environment_container" "mailcatcher" {
  description = "Shared dev mail catcher (Mailpit) for ZITADEL email flows. Dev-only permanent mock. Managed by Terraform; see .kiro/specs/dev-mail-catcher."

  node_name = var.proxmox_node_name
  # Operator override wins when non-zero; otherwise the deterministic module VMID
  # (vlan_id * 1000 + host_index = 20011, ADR-0022).
  vm_id = var.mailcatcher_vmid != 0 ? var.mailcatcher_vmid : module.mailcatcher.vmid

  unprivileged = true
  features {
    nesting = true # required for the in-container Docker engine
  }

  cpu {
    cores = 1
  }

  memory {
    dedicated = 512
    swap      = 0
  }

  disk {
    datastore_id = var.mailcatcher_datastore_id
    size         = 4
  }

  operating_system {
    template_file_id = var.mailcatcher_template_file_id
    type             = var.mailcatcher_template_os_type
  }

  initialization {
    hostname = module.mailcatcher.hostname # mailcatcher-20-01

    ip_config {
      ipv4 {
        address = module.mailcatcher.ip_config.ipv4.address # 10.0.20.21/24 (computed)
        gateway = module.mailcatcher.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys
      }
    }
  }

  # `p20` is a VLAN-zone SDN VNet that applies tag 20 itself; the guest NIC must
  # NOT also 802.1Q-tag (no vlan_id here). Static address from cloud-init only;
  # SDN IPAM/DHCP stays disabled (NET-00 §5). The p20 VNet is Platform-Foundation
  # owned; this root only references it.
  network_interface {
    name   = "veth0"
    bridge = "p20"
  }

  tags = module.mailcatcher.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
  }
}
