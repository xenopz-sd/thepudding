# infra/projects/<slug>/workload/main.tf  —  Workload_Consumer_Root (per-project
# OPTIONAL consumer workload guest).  TEMPLATE/EXAMPLE instance inherited by
# every project (consumer-last-mile-hello-workload §B, task 2.2; ADR-0021).
#
# ===========================================================================
# What this root is
# ===========================================================================
# This is the thin per-project `workload/` root that gives a Consumer_Project an
# OPTIONAL guest on its OWN project VLAN (100+, NEVER VLAN 20) to run its
# application Compose stack (the OpenBao Agent sidecar + the workload container).
# It is the §B "where does my app run?" answer: a provisioned, ledgered,
# documented workload host.
#
# It MIRRORS the sibling `db/` root's wiring exactly (registry-resolved vlan_id,
# ledger-read host_index, ledger-derived peer set, fail-closed preconditions),
# with three deliberate differences dictated by the requirements:
#
#   1. The guest is OPTIONAL and count-gated — `count = var.workload_enabled ?
#      var.workload_count : 0` (default DISABLED). A project with no workload
#      block provisions nothing here and behaves exactly as before (Req 2.3 —
#      strict no-op).
#   2. Guest identity is the ADR-0021 NON-CATALOG role code `role_code =
#      "workload"` (NOT an `svc_code`). A workload guest occupies no catalog
#      SVC-NN slot; `role_code` is the sanctioned, module-validated non-catalog
#      path, so `svc_code` keeps its single "stable catalog slot" meaning. The
#      module builds the hostname `workload-<vlan_id>-<instance>` from it and
#      Guard 3/5 accept + enforce it.
#   3. The guest is STATELESS — NO `prevent_destroy`, NO lifecycle anchor
#      (Req 2.5). It is torn down with the project by `terraform destroy` /
#      the consumer clean-slate. The DB guest remains the sole stateful anchor.
#
# WHY A SEPARATE `workload/` ROOT (not folded into `<slug>-infra` or `db/`):
# one state per service keeps `terraform destroy` of the workload from touching
# the project's network slice or its stateful DB, and vice versa
# (integration-boundaries §5, mirroring ADR-0019's rationale for the `db/`
# split). The network slice (`<slug>-infra`) stays the `infra/projects/<slug>/`
# root (the _TEMPLATE top level); the DB is the `db/` subroot; the workload is
# THIS `workload/` subroot with its own `<slug>-workload` state (versions.tf).
#
# WHY `dronefleet` AS THE SCAFFOLD SLUG: the repo scaffolds per-project roots by
# COPYING a representative example (the `_TEMPLATE` convention). Copy this
# `workload/` directory to `infra/projects/<your-slug>/workload/` and change
# `var.project_slug`'s default (or pass `-var project_slug=<slug>`); everything
# else — vlan_id, host_index, host IP, hostname — is DERIVED, never hand-edited
# (mirrors the `db/` root).

locals {
  # -------------------------------------------------------------------------
  # Registry-authoritative vlan_id resolution (mirrors db/main.tf). The vlan_id
  # is READ from the committed VLAN-ID registry (projects.yaml) by matching this
  # root's project_slug — it is NEVER a free operator input. Relative path walks
  # from infra/projects/<slug>/workload/ back to infra/platform-foundation/.
  # -------------------------------------------------------------------------
  registry = yamldecode(file("${path.module}/../../../platform-foundation/projects.yaml"))

  # The sole registry entry for this slug (one() => null if absent; raises on a
  # duplicate slug, which the onboarding script's duplicate guard prevents).
  entry   = one([for p in local.registry.projects : p if p.slug == var.project_slug])
  vlan_id = local.entry != null ? local.entry.vlan_id : null

  # -------------------------------------------------------------------------
  # Host_Index_Authority: this guest's own host_index (Assigned_Index_View) and
  # the peer set (Peer_Set_View) are READ from the committed ledger
  # (host-indices.yaml) rather than taken as a free operator input (ADR-0015),
  # exactly as the db/ root does. host_index is NOT hardcoded to 1 — it is read
  # from the VLAN's `role_code: workload` row.
  # -------------------------------------------------------------------------
  host_index_ledger = yamldecode(file("${path.module}/../../../platform-foundation/host-indices.yaml"))

  # yamldecode renders integer top-level keys as strings.
  vlan_key     = local.vlan_id != null ? tostring(local.vlan_id) : ""
  vlan_present = contains(keys(local.host_index_ledger), local.vlan_key)
  vlan_entries = local.vlan_present ? local.host_index_ledger[local.vlan_key] : []

  # This guest's own ledger row: the ADR-0021 role_code == "workload" row (NO
  # svc_code key, NO component). Resolve its host_index as the Assigned index.
  # `one()` returns the sole match, or null if the ledger has no such row
  # (caught by the precondition below).
  #
  # ADR-0021 key-safe iteration (mirrors db/main.tf): a VLAN carries a MIX of
  # catalog `svc_code` rows (the DB guest) and non-catalog `role_code` rows (this
  # workload guest). Read BOTH identity keys with lookup(e, ..., null) so
  # iterating the mixed VLAN never raises "Missing map element" on a row that
  # lacks one key — a role_code row has no svc_code and vice versa.
  assigned_host_index = one([
    for e in local.vlan_entries : e.host_index
    if lookup(e, "role_code", null) == "workload" && lookup(e, "component", null) == null
  ])

  # Peer_Set_View: every OTHER host_index already allocated on this project VLAN
  # (every row's host_index except THIS workload guest's own). Threaded into the
  # module's peer_host_indices so the plan-time Guard 4 collision precondition is
  # fed (ADR-0015). On dronefleet VLAN 100 this is the DB guest's index 0, so the
  # workload guest at index 1 (10.0.100.11) vs the DB at index 0 (10.0.100.10)
  # is cross-checked at plan time. Mirrors the db/ root's derivation; here we
  # exclude the `role_code: workload` row (this guest) rather than the svc01 row.
  #
  # tonumber() COERCION GUARD (mirrors the svc-01 root): once the shared ledger
  # carries more than one top-level VLAN key whose entry-lists differ in shape
  # (VLAN 20's rows carry component/anchor; a project VLAN's rows may not),
  # terraform coerces every scalar — including host_index — to STRING. tonumber()
  # keeps the derived peer indices numeric so the module's Guard 4
  # (`!contains(peer, host_index)`) compares number-to-number.
  peer_host_indices = [
    for e in local.vlan_entries : tonumber(e.host_index)
    if !(lookup(e, "role_code", null) == "workload" && lookup(e, "component", null) == null)
  ]
}

# ---------------------------------------------------------------------------
# Fail CLOSED before any resource is created if the slug is unregistered or —
# WHEN the workload guest is ENABLED — the ledger does not record a host_index
# for the `role_code: workload` row on this VLAN. When the guest is DISABLED
# (the default), the host_index precondition is a no-op: a project that declares
# no workload needs no ledger row (Req 2.3 strict no-op). Uses the built-in
# terraform_data as a precondition anchor (no extra provider, no real infra),
# mirroring db/main.tf.
# ---------------------------------------------------------------------------
resource "terraform_data" "assert_resolvable" {
  lifecycle {
    precondition {
      condition     = local.vlan_id != null
      error_message = "Slug '${var.project_slug}' is not present in infra/platform-foundation/projects.yaml; onboard it via the onboarding script + merge request first (NET-00 §7)."
    }
    precondition {
      # Only required when the workload guest is actually being provisioned.
      condition     = !(var.workload_enabled) || local.assigned_host_index != null
      error_message = "workload_enabled=true but no host_index is recorded for role_code=workload on VLAN ${local.vlan_id} in infra/platform-foundation/host-indices.yaml (the Host_Index_Authority). Add the project-VLAN workload ledger row before provisioning the workload guest — the index is read from the ledger, never defaulted (ADR-0015, ADR-0021)."
    }
  }
}

# ---------------------------------------------------------------------------
# Addressing/hostname/tags for the workload guest, from the shared
# `proxmox-compute` module (consumed UNMODIFIED), count-gated on
# var.workload_enabled × var.workload_count (default DISABLED).
#
# Identity: role_code = "workload" (ADR-0021 non-catalog path), NOT svc_code.
#   -> hostname  = workload-<vlan_id>-<instance>  (e.g. workload-100-01)
#   -> tags      = ["proj-<slug>-workload"]       (service_name = "workload")
#   -> ipv4      = cidrhost("10.0.<vlan>.0/24", 10 + ledger_index)
# These are the ADR-0021 SANCTIONED non-catalog convention — the module
# validates the role code, builds the hostname from it (locals.tf), Guard 3
# accepts the role-code hostname form, and Guard 5 enforces exactly-one-of
# svc_code/role_code. It is NOT a deviation.
#
# peer_host_indices is the ledger-derived set of OTHER indices on this VLAN
# (local.peer_host_indices), feeding the module's Guard-4 plan-time collision
# precondition (ADR-0015). On single-guest VLANs that set is empty => no-op.
# ---------------------------------------------------------------------------
module "workload" {
  source = "../../../modules/proxmox-compute"
  count  = var.workload_enabled ? var.workload_count : 0

  project_slug = var.project_slug
  service_name = "workload" # -> tag proj-<slug>-workload (ADR-0021 convention)
  role_code    = "workload" # ADR-0021 non-catalog identity -> hostname workload-<vlan>-01
  vlan_id      = local.vlan_id
  host_index   = local.assigned_host_index
  instance     = "01"

  peer_host_indices = local.peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injected on the guest below
}

# ---------------------------------------------------------------------------
# The workload container. Unprivileged LXC, nesting (for in-LXC Docker),
# tier-sized, on the project's SDN VNet p<vlan>, stock template (Docker +
# Compose per host by the common role, ADR-0003). Count-gated identically to the
# module above so the guest exists ONLY when workload_enabled is true.
#
# NO `prevent_destroy` / NO lifecycle anchor (Req 2.5): the workload is
# STATELESS and is torn down with the project. The DB guest (db/ root) remains
# the sole stateful anchor. A plain count-gated resource in the project's own
# `<slug>-workload` state => `terraform destroy` removes it cleanly, with no
# seatbelt to consciously remove first (contrast the DB guest's prevent_destroy).
# ---------------------------------------------------------------------------
resource "proxmox_virtual_environment_container" "workload" {
  count = var.workload_enabled ? var.workload_count : 0

  description = "Consumer workload guest for project '${var.project_slug}' (consumer-last-mile-hello-workload §B). Runs the application Compose stack (OpenBao Agent sidecar + workload container). Managed by Terraform; stateless, torn down with the project."

  node_name = var.proxmox_node_name
  vm_id     = var.workload_vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = local.workload_cpu_cores
  }

  memory {
    dedicated = local.workload_memory_mb
    swap      = 0 # per-container swap cap (ADR-0006); the agent sidecar holds rendered creds in RAM
  }

  disk {
    datastore_id = var.workload_datastore_id
    size         = local.workload_disk_gb
  }

  operating_system {
    template_file_id = var.workload_template_file_id
    type             = var.workload_template_os_type
  }

  initialization {
    hostname = module.workload[count.index].hostname # workload-<vlan>-01

    ip_config {
      ipv4 {
        address = module.workload[count.index].ip_config.ipv4.address # computed 10.0.<vlan>.11/24
        gateway = module.workload[count.index].ip_config.ipv4.gateway # 10.0.<vlan>.1
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
  # rejects start). The VNet is created by the project's network-slice root
  # (infra/projects/<slug>/ sdn.tf), referenced here. SDN IPAM/DHCP stays
  # disabled (NET-00 §5). Mirrors the db/ guest's NIC wiring.
  network_interface {
    name   = "veth0"
    bridge = local.workload_vnet_id # p<vlan>; VNet applies the tag, NIC must not
  }

  tags = module.workload[count.index].tags # proj-<slug>-workload

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id,
    ]
    # NOTE: intentionally NO prevent_destroy here (Req 2.5). The workload is
    # stateless and is torn down with the project — never anchored.
  }
}
