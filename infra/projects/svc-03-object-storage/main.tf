# SVC-03 Object Storage (Garage) — three unprivileged LXC nodes on the shared-
# services VLAN 20, one Compose stack each. Spec: .kiro/specs/svc-03-object-storage.
#
# IDENTITY: catalog slot svc03, instances 01/02/03, no component (the three nodes
# are identical cluster members) -> hostnames svc03-20-01..03 (NET-00 §4).
# ADDRESSING: ledger indices 12/13/14 -> 10.0.20.22/23/24 (NET-00 §3), VMIDs
# computed 20012/20013/20014 (ADR-0022). Each node keeps Garage data + metadata on
# a DEDICATED data volume (mount point), not on the rootfs (requirements §7).
locals {
  # The Host_Index_Authority ledger (ADR-0015): read-only input, never state.
  _host_index_ledger   = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))
  _vlan_id             = 20
  _vlan20_entries      = local._host_index_ledger[local._vlan_id]
  _vlan20_host_indices = [for e in local._vlan20_entries : e.host_index]

  # Each node's ledger index, selected by (svc_code svc03, instance N). Key-safe
  # lookup(): role_code rows carry no svc_code key.
  _node1_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if lookup(e, "svc_code", null) == "svc03" && lookup(e, "instance", null) == 1
  ])
  _node2_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if lookup(e, "svc_code", null) == "svc03" && lookup(e, "instance", null) == 2
  ])
  _node3_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if lookup(e, "svc_code", null) == "svc03" && lookup(e, "instance", null) == 3
  ])

  # This root's EXPLICIT indices, cross-checked against the ledger below.
  node1_host_index = 12 # -> 10.0.20.22
  node2_host_index = 13 # -> 10.0.20.23
  node3_host_index = 14 # -> 10.0.20.24

  # Peer set per node = every other VLAN-20 index (module Guard 4).
  node1_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.node1_host_index
  ]
  node2_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.node2_host_index
  ]
  node3_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.node3_host_index
  ]
}

# ===========================================================================
# GARAGE NODE 1 — svc03-20-01, 10.0.20.22, host_index 12
# ===========================================================================
module "node1" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared" # shared platform service -> tag proj-shared-garage
  service_name = "garage" # -> tags ["proj-shared-garage"]; inventory service_role "garage"
  svc_code     = "svc03"  # stable catalog slot (NET-00 §4)
  vlan_id      = local._vlan_id
  host_index   = local.node1_host_index
  instance     = "01" # -> svc03-20-01

  peer_host_indices = local.node1_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the container resource below
}

# ===========================================================================
# GARAGE NODE 2 — svc03-20-02, 10.0.20.23, host_index 13
# ===========================================================================
module "node2" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared" # shared platform service -> tag proj-shared-garage
  service_name = "garage" # -> tags ["proj-shared-garage"]; inventory service_role "garage"
  svc_code     = "svc03"  # stable catalog slot (NET-00 §4)
  vlan_id      = local._vlan_id
  host_index   = local.node2_host_index
  instance     = "02" # -> svc03-20-02

  peer_host_indices = local.node2_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the container resource below
}

# ===========================================================================
# GARAGE NODE 3 — svc03-20-03, 10.0.20.24, host_index 14
# ===========================================================================
module "node3" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared" # shared platform service -> tag proj-shared-garage
  service_name = "garage" # -> tags ["proj-shared-garage"]; inventory service_role "garage"
  svc_code     = "svc03"  # stable catalog slot (NET-00 §4)
  vlan_id      = local._vlan_id
  host_index   = local.node3_host_index
  instance     = "03" # -> svc03-20-03

  peer_host_indices = local.node3_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the container resource below
}

# Ledger/root drift guard (a `module` block cannot carry a lifecycle
# precondition, so this no-side-effect anchor hosts the cross-checks).
resource "terraform_data" "ledger_index_assert" {
  input = {
    node1_host_index   = local.node1_host_index
    node1_ledger_index = local._node1_ledger_index
    node2_host_index   = local.node2_host_index
    node2_ledger_index = local._node2_ledger_index
    node3_host_index   = local.node3_host_index
    node3_ledger_index = local._node3_ledger_index
  }

  lifecycle {
    precondition {
      condition     = local.node1_host_index == local._node1_ledger_index
      error_message = "SVC-03 node 1 host_index (${local.node1_host_index}) does not match the host-indices.yaml ledger entry for svc03 instance 1 on VLAN 20 (${local._node1_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
    precondition {
      condition     = local.node2_host_index == local._node2_ledger_index
      error_message = "SVC-03 node 2 host_index (${local.node2_host_index}) does not match the host-indices.yaml ledger entry for svc03 instance 2 on VLAN 20 (${local._node2_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
    precondition {
      condition     = local.node3_host_index == local._node3_ledger_index
      error_message = "SVC-03 node 3 host_index (${local.node3_host_index}) does not match the host-indices.yaml ledger entry for svc03 instance 3 on VLAN 20 (${local._node3_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
  }
}

# Garage node 1 container. STATEFUL (object data + metadata on the data
# volume), so prevent_destroy: a deliberate replacement goes through SVC-03's own
# clean-slate contract. Unprivileged, nesting for the in-container Docker engine,
# no swap (ADR-0006 posture kept uniform).
resource "proxmox_virtual_environment_container" "node1" {
  description = "SVC-03 Garage object storage node 1 of 3 (replication factor 3). Managed by Terraform; see .kiro/specs/svc-03-object-storage."

  node_name = var.proxmox_node_name
  # Operator override wins when non-zero; otherwise the deterministic module VMID
  # (vlan_id * 1000 + host_index = 20012, ADR-0022).
  vm_id = var.garage_node1_vmid != 0 ? var.garage_node1_vmid : module.node1.vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # requirements §7
  }

  memory {
    dedicated = 2048 # requirements §7 (2-4 GB)
    swap      = 0
  }

  # Rootfs holds only the OS, Docker and the Compose stack.
  disk {
    datastore_id = var.garage_datastore_id
    size         = 8
  }

  # Dedicated data volume for Garage data + metadata (requirements §7).
  mount_point {
    volume = var.garage_datastore_id
    size   = "${var.garage_data_disk_gb}G"
    path   = "/var/lib/garage"
  }

  operating_system {
    template_file_id = var.garage_template_file_id
    type             = var.garage_template_os_type
  }

  initialization {
    hostname = module.node1.hostname # svc03-20-01

    ip_config {
      ipv4 {
        address = module.node1.ip_config.ipv4.address # 10.0.20.22/24 (computed)
        gateway = module.node1.ip_config.ipv4.gateway # 10.0.20.1
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
  # NOT also 802.1Q-tag (no vlan_id here). Static address from cloud-init only.
  network_interface {
    name   = "veth0"
    bridge = "p20"
  }

  tags = module.node1.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
    prevent_destroy = true
  }
}

# Garage node 2 container. STATEFUL (object data + metadata on the data
# volume), so prevent_destroy: a deliberate replacement goes through SVC-03's own
# clean-slate contract. Unprivileged, nesting for the in-container Docker engine,
# no swap (ADR-0006 posture kept uniform).
resource "proxmox_virtual_environment_container" "node2" {
  description = "SVC-03 Garage object storage node 2 of 3 (replication factor 3). Managed by Terraform; see .kiro/specs/svc-03-object-storage."

  node_name = var.proxmox_node_name
  # Operator override wins when non-zero; otherwise the deterministic module VMID
  # (vlan_id * 1000 + host_index = 20013, ADR-0022).
  vm_id = var.garage_node2_vmid != 0 ? var.garage_node2_vmid : module.node2.vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # requirements §7
  }

  memory {
    dedicated = 2048 # requirements §7 (2-4 GB)
    swap      = 0
  }

  # Rootfs holds only the OS, Docker and the Compose stack.
  disk {
    datastore_id = var.garage_datastore_id
    size         = 8
  }

  # Dedicated data volume for Garage data + metadata (requirements §7).
  mount_point {
    volume = var.garage_datastore_id
    size   = "${var.garage_data_disk_gb}G"
    path   = "/var/lib/garage"
  }

  operating_system {
    template_file_id = var.garage_template_file_id
    type             = var.garage_template_os_type
  }

  initialization {
    hostname = module.node2.hostname # svc03-20-02

    ip_config {
      ipv4 {
        address = module.node2.ip_config.ipv4.address # 10.0.20.23/24 (computed)
        gateway = module.node2.ip_config.ipv4.gateway # 10.0.20.1
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
  # NOT also 802.1Q-tag (no vlan_id here). Static address from cloud-init only.
  network_interface {
    name   = "veth0"
    bridge = "p20"
  }

  tags = module.node2.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
    prevent_destroy = true
  }
}

# Garage node 3 container. STATEFUL (object data + metadata on the data
# volume), so prevent_destroy: a deliberate replacement goes through SVC-03's own
# clean-slate contract. Unprivileged, nesting for the in-container Docker engine,
# no swap (ADR-0006 posture kept uniform).
resource "proxmox_virtual_environment_container" "node3" {
  description = "SVC-03 Garage object storage node 3 of 3 (replication factor 3). Managed by Terraform; see .kiro/specs/svc-03-object-storage."

  node_name = var.proxmox_node_name
  # Operator override wins when non-zero; otherwise the deterministic module VMID
  # (vlan_id * 1000 + host_index = 20014, ADR-0022).
  vm_id = var.garage_node3_vmid != 0 ? var.garage_node3_vmid : module.node3.vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # requirements §7
  }

  memory {
    dedicated = 2048 # requirements §7 (2-4 GB)
    swap      = 0
  }

  # Rootfs holds only the OS, Docker and the Compose stack.
  disk {
    datastore_id = var.garage_datastore_id
    size         = 8
  }

  # Dedicated data volume for Garage data + metadata (requirements §7).
  mount_point {
    volume = var.garage_datastore_id
    size   = "${var.garage_data_disk_gb}G"
    path   = "/var/lib/garage"
  }

  operating_system {
    template_file_id = var.garage_template_file_id
    type             = var.garage_template_os_type
  }

  initialization {
    hostname = module.node3.hostname # svc03-20-03

    ip_config {
      ipv4 {
        address = module.node3.ip_config.ipv4.address # 10.0.20.24/24 (computed)
        gateway = module.node3.ip_config.ipv4.gateway # 10.0.20.1
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
  # NOT also 802.1Q-tag (no vlan_id here). Static address from cloud-init only.
  network_interface {
    name   = "veth0"
    bridge = "p20"
  }

  tags = module.node3.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
    prevent_destroy = true
  }
}
