# SVC-01 SQL Database Terraform root — LXC declaration (reusable, per-project).
#
# Declares exactly ONE `proxmox_virtual_environment_container` — the dedicated
# database LXC for ONE project instance — plus an OPT-IN, default-OFF firewall
# rules resource scoping inbound 5432 to the project subnet. Addressing/hostname/
# tags come from the shared `proxmox-compute` module (consumed UNMODIFIED),
# parameterized by var.project_slug + var.vlan_id. No operator IP literal.

# svc_code=svc01, project_slug=<slug>, vlan_id=<vlan>, host_index -> computed
# ipv4 10.0.<vlan>.1X, hostname "svc01-<vlan>-01", tags ["proj-<slug>-postgres"]
# (NET-00 §3-§4). The service_name is "postgres" or "mariadb" per db_engine so
# the tag reflects the engine, while svc_code stays the stable catalog slot
# (hostname survives an engine swap — NET-00 §4).
module "db" {
  source = "../../modules/proxmox-compute"

  project_slug = var.project_slug
  service_name = var.db_engine # -> tag proj-<slug>-postgres | proj-<slug>-mariadb
  svc_code     = "svc01"       # stable catalog slot -> hostname svc01-<vlan>-01
  vlan_id      = var.vlan_id
  host_index   = var.host_index
  instance     = "01"

  # Host_Index_Authority peer set (fix-vlan-host-index-collision, C9): every OTHER
  # host_index on this DB's VLAN, so the module's Guard-4 plan-time precondition
  # rejects a within-VLAN host-index collision before any Proxmox resource. On a
  # single-guest project VLAN with no ledger entry this is [] (strict no-op =
  # today's behavior); on a shared VLAN (e.g. 20, where this DB is host_index 4)
  # it is the ledger-derived set excluding this guest's own index. Derived in
  # variables.tf locals (explicit var.peer_host_indices override wins).
  peer_host_indices = local.svc01_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection on the guest below
}

# The dedicated database container. Unprivileged, nesting (for in-LXC Docker),
# tier-sized, on the project's SDN VNet p<vlan>, stock template (Docker +
# Compose per host by the common role, ADR-0003). prevent_destroy because a DB
# is a live, stateful store — never silently replaced by an apply.
resource "proxmox_virtual_environment_container" "db" {
  description = "SVC-01 dedicated ${var.db_engine} database for project '${var.project_slug}'. Managed by Terraform; see .kiro/specs/svc-01-sql-database."

  node_name = var.proxmox_node_name
  vm_id     = var.svc01_vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = local.svc01_cpu_cores
  }

  memory {
    dedicated = local.svc01_memory_mb
    swap      = 0 # per-container swap cap (ADR-0006) — a DB holds credentials in RAM
  }

  disk {
    datastore_id = var.svc01_datastore_id
    size         = local.svc01_disk_gb
  }

  operating_system {
    template_file_id = var.svc01_template_file_id
    type             = var.svc01_template_os_type
  }

  initialization {
    hostname = module.db.hostname # svc01-<vlan>-01

    ip_config {
      ipv4 {
        address = module.db.ip_config.ipv4.address # computed 10.0.<vlan>.1X/24
        gateway = module.db.ip_config.ipv4.gateway # 10.0.<vlan>.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys
      }
    }
  }

  # Attach to the project's SDN VNet p<vlan>. The VLAN-zone VNet applies the tag
  # itself; the NIC must NOT set a NIC-level vlan_id (double-tag -> Proxmox
  # rejects start). The VNet is created by the project root / platform foundation
  # (for p20), referenced here. SDN IPAM/DHCP stays disabled (NET-00 §5).
  network_interface {
    name   = "veth0"
    bridge = local.svc01_vnet_id # p<vlan>; VNet applies the tag, NIC must not
  }

  tags = module.db.tags # proj-<slug>-postgres | proj-<slug>-mariadb

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
    # SEATBELT: a live, stateful database. Never silently destroy/replace it on
    # an apply (that would discard the data volume). prevent_destroy hard-errors
    # a destroy/replace plan; a deliberate replacement requires consciously
    # removing this block. See the SVC-01 INSTALL-RUNBOOK destructive-plan note.
    prevent_destroy = true
  }
}

# OPT-IN, default-OFF guest firewall rules scoping inbound 5432 to the project
# subnet (defence-in-depth; primary isolation is the VLAN-per-project L2 model +
# the private VLAN-bound port binding). REVERSIBLE and inert until the operator
# enables the pve-firewall master switch — mirroring ADR-0008's gated posture, so
# a merge never flips guest firewalling on (which can disturb unprivileged-LXC
# egress). Declared only when enable_firewall_rules is true.
resource "proxmox_virtual_environment_firewall_rules" "db" {
  count = var.enable_firewall_rules ? 1 : 0

  node_name    = var.proxmox_node_name
  container_id = var.svc01_vmid

  rule {
    type    = "in"
    action  = "ACCEPT"
    source  = local.svc01_app_subnet_cidr
    dport   = tostring(local.svc01_db_port)
    proto   = "tcp"
    comment = "SVC-01 ${var.db_engine} ${local.svc01_db_port}/tcp from project subnet only (defence-in-depth; VLAN L2 isolation is primary)"
  }

  depends_on = [proxmox_virtual_environment_container.db]
}
