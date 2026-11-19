# SVC-10 internal DNS (PowerDNS authoritative + recursor) — one unprivileged LXC
# on the shared-services VLAN 20. Spec: .kiro/specs/svc-10-internal-dns.
#
# IDENTITY: catalog slot svc10, instance 01, no component -> hostname
# svc10-20-01 (NET-00 §4). The host index (15) comes from the Host_Index_Authority
# ledger (ADR-0015) -> 10.0.20.25; VMID 20015 (ADR-0022).
#
# NON-ANCHOR: the zone content is rebuilt from the generated inventories by the
# installer on every run, so the guest is disposable (no prevent_destroy).
locals {
  # The Host_Index_Authority ledger (ADR-0015): read-only input, never state.
  _host_index_ledger   = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))
  _vlan_id             = 20
  _vlan20_entries      = local._host_index_ledger[local._vlan_id]
  _vlan20_host_indices = [for e in local._vlan20_entries : e.host_index]
  # This guest's ledger index (svc_code svc10, instance 1). Key-safe lookup():
  # role_code rows carry no svc_code key.
  _svc10_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if lookup(e, "svc_code", null) == "svc10" && lookup(e, "instance", null) == 1
  ])
  svc10_host_index = 15 # -> 10.0.20.25 (cidrhost("10.0.20.0/24", 10 + 15))
  # Every OTHER VLAN-20 index; fed to the module's Guard 4 (within-VLAN collision).
  svc10_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.svc10_host_index
  ]
}

module "dns" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared" # shared platform service -> tag proj-shared-dns
  service_name = "dns"    # -> tags ["proj-shared-dns"] (PF §4); also the inventory service_role
  svc_code     = "svc10"  # stable catalog slot (NET-00 §4)
  vlan_id      = local._vlan_id
  host_index   = local.svc10_host_index
  instance     = "01" # -> svc10-20-01

  peer_host_indices = local.svc10_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the container resource below
}

# Ledger/root drift guard (a `module` block cannot carry a lifecycle
# precondition, so this no-side-effect anchor hosts the cross-check).
resource "terraform_data" "ledger_index_assert" {
  input = {
    svc10_host_index   = local.svc10_host_index
    svc10_ledger_index = local._svc10_ledger_index
  }
  lifecycle {
    precondition {
      condition     = local.svc10_host_index == local._svc10_ledger_index
      error_message = "SVC-10 host_index (${local.svc10_host_index}) does not match the host-indices.yaml ledger entry for svc_code svc10 instance 1 on VLAN 20 (${local._svc10_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
  }
}

# The DNS container. Unprivileged, 1 vCPU / 512 MB / 4 GB, no swap (ADR-0006
# posture kept uniform). Disposable: no prevent_destroy.
resource "proxmox_virtual_environment_container" "dns" {
  description = "SVC-10 internal DNS (PowerDNS authoritative + recursor) for the platform domain. Managed by Terraform; see .kiro/specs/svc-10-internal-dns."
  node_name   = var.proxmox_node_name
  # Operator override wins when non-zero; otherwise the deterministic module VMID
  # (vlan_id * 1000 + host_index = 20015, ADR-0022).
  vm_id = var.svc10_vmid != 0 ? var.svc10_vmid : module.dns.vmid

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
    datastore_id = var.svc10_datastore_id
    size         = 4
  }

  operating_system {
    template_file_id = var.svc10_template_file_id
    type             = var.svc10_template_os_type
  }

  initialization {
    hostname = module.dns.hostname # svc10-20-01
    ip_config {
      ipv4 {
        address = module.dns.ip_config.ipv4.address # 10.0.20.25/24 (computed)
        gateway = module.dns.ip_config.ipv4.gateway # 10.0.20.1
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

  tags = module.dns.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
  }
}
