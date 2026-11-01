# infra/projects/<slug>/db/main.tf  —  SVC01_Consumer_Root (per-project SVC-01
# consumer database root).  TEMPLATE/EXAMPLE instance for the test slug
# `dronefleet` (project-consumer-onboarding, Task 6.1; ADR-0019).
#
# ===========================================================================
# What this root is (ADR-0019 — reuse, do NOT fork a DB module)
# ===========================================================================
# This is the thin per-project `db/` root that gives a Consumer_Project its OWN
# PostgreSQL guest on its OWN project VLAN (100+, NEVER VLAN 20). It does NOT
# re-declare the DB container or re-derive addressing: it REUSES the existing
# reusable SVC-01 root (`infra/projects/svc-01-sql-database`) as a module,
# threading in only the per-project inputs. The SVC-01 module already owns the
# proxmox-compute invocation, the computed host IP / hostname / tags, the
# ledger-coordinated peer set, and `prevent_destroy` on the stateful guest
# (Req 3.3, 3.4, 3.6, 3.9) — this wrapper's whole job is to RESOLVE the inputs
# the right way (registry for vlan_id, ledger for host_index) and give the DB
# its own `<slug>-db` Terraform state (versions.tf, Req 3.5).
#
# WHY A SEPARATE `db/` ROOT (not folded into `<slug>-infra`): one state per
# service keeps `terraform destroy` of the DB from touching the project's
# network slice, and vice versa (integration-boundaries §5, ADR-0019 alt C).
# The network slice (`<slug>-infra`) stays the `infra/projects/<slug>/` root
# (the _TEMPLATE); the DB is this `db/` subdirectory root.
#
# WHY `dronefleet` AS THE SCAFFOLD SLUG: the repo scaffolds per-project roots by
# COPYING a representative example (the `_TEMPLATE` convention). This directory
# IS that example, instantiated for the registered test slug `dronefleet`
# (projects.yaml: dronefleet -> vlan_id 100). Copy this `db/` directory to
# `infra/projects/<your-slug>/db/` and change `var.project_slug`'s default (or
# pass `-var project_slug=<slug>`); everything else — vlan_id, host_index, host
# IP — is DERIVED, never hand-edited (Req 2.3, 3.8).

locals {
  # -------------------------------------------------------------------------
  # Registry-authoritative vlan_id resolution (Req 2.1, 3.1; mirrors
  # _TEMPLATE/main.tf). The vlan_id is READ from the committed VLAN-ID registry
  # (projects.yaml) by matching this root's project_slug — it is NEVER a free
  # operator input (Req 2.3). Relative path walks from
  # infra/projects/<slug>/db/ back to infra/platform-foundation/.
  # -------------------------------------------------------------------------
  registry = yamldecode(file("${path.module}/../../../platform-foundation/projects.yaml"))

  # The sole registry entry for this slug (one() => null if absent; raises on a
  # duplicate slug, which the onboarding script's duplicate guard prevents).
  entry   = one([for p in local.registry.projects : p if p.slug == var.project_slug])
  vlan_id = local.entry != null ? local.entry.vlan_id : null

  # -------------------------------------------------------------------------
  # Host_Index_Authority: this guest's own host_index (Assigned_Index_View) and
  # the peer set (Peer_Set_View) are READ from the committed ledger
  # (host-indices.yaml) rather than taken as a free operator input (Req 3.8).
  # The SVC-01 module ALSO derives its peer set from the ledger internally; we
  # read the ledger here only to resolve THIS guest's own host_index and to let
  # a precondition cross-check the root against the ledger. The module's own
  # ledger-derived peer_host_indices feeds its proxmox-compute Guard 4 (Req 3.7).
  # -------------------------------------------------------------------------
  host_index_ledger = yamldecode(file("${path.module}/../../../platform-foundation/host-indices.yaml"))

  # yamldecode renders integer top-level keys as strings.
  vlan_key     = local.vlan_id != null ? tostring(local.vlan_id) : ""
  vlan_present = contains(keys(local.host_index_ledger), local.vlan_key)
  vlan_entries = local.vlan_present ? local.host_index_ledger[local.vlan_key] : []

  # This guest's own ledger row: svc_code == svc01 and (for the single-SVC-01
  # project-VLAN case) no component. Resolve its host_index as the Assigned
  # index. `one()` returns the sole match, or null if the ledger has no such row
  # (caught by the precondition below).
  #
  # ADR-0021: a VLAN may now also carry NON-catalog rows keyed on `role_code`
  # (e.g. the per-project `workload` guest) with NO `svc_code` key. Read the
  # identity key-safely with lookup(e, "svc_code", null) so iterating a mixed
  # svc_code/role_code VLAN does not raise "Missing map element" on a role_code
  # row — those rows simply don't match svc01 and are skipped.
  assigned_host_index = one([
    for e in local.vlan_entries : e.host_index
    if lookup(e, "svc_code", null) == "svc01" && lookup(e, "component", null) == null
  ])
}

# ---------------------------------------------------------------------------
# Fail CLOSED before any resource is created if the slug is unregistered or the
# ledger does not record a host_index for this guest (Req 3.7 at the root layer:
# a missing ledger index fails the plan rather than defaulting to a colliding 0;
# the proxmox-compute Guard 4 is the cross-root collision backstop). Uses the
# built-in terraform_data as a precondition anchor (no extra provider, no real
# infrastructure), mirroring _TEMPLATE/main.tf.
# ---------------------------------------------------------------------------
resource "terraform_data" "assert_resolvable" {
  lifecycle {
    precondition {
      condition     = local.vlan_id != null
      error_message = "Slug '${var.project_slug}' is not present in infra/platform-foundation/projects.yaml; onboard it via the onboarding script + merge request first (NET-00 §7, Req 2.2)."
    }
    precondition {
      condition     = local.assigned_host_index != null
      error_message = "No host_index is recorded for svc_code=svc01 on VLAN ${local.vlan_id} in infra/platform-foundation/host-indices.yaml (the Host_Index_Authority). Add the project-VLAN ledger row before provisioning the consumer DB — the index is read from the ledger, never defaulted (Req 3.7, 3.8, ADR-0015)."
    }
  }
}

# ---------------------------------------------------------------------------
# REUSE the existing reusable SVC-01 root as a module (ADR-0019). The module
# owns the proxmox-compute invocation, computed addressing, ledger peer-set
# derivation, prevent_destroy, and the generated-password->OpenBao handoff
# (via its Ansible layer). This wrapper supplies only the resolved inputs.
#
#   project_slug -> tag proj-<slug>-postgres, DB name/role defaults  (Req 3.6)
#   vlan_id      -> Registry-resolved, 100+ (module validation rejects 30-99 /
#                   management 10; proxmox-compute Guard 1 rejects 10)  (Req 3.2)
#   host_index   -> Host_Index_Authority ledger (not operator-chosen) (Req 3.8)
#   svc_code     -> fixed svc01 inside the module -> hostname svc01-<vlan>-01 (Req 3.6)
#   host IP      -> cidrhost("10.0.<vlan>.0/24", 10 + host_index)      (Req 3.4)
#
# peer_host_indices is left at its default so the SVC-01 module DERIVES the peer
# set from the ledger itself for this vlan_id, excluding this guest's own svc01
# row (Req 3.7). On single-guest VLAN 100 that derived set is empty => Guard 4
# no-op (backward-compatible). A second root later on VLAN 100 reads this guest's
# ledger index as its peer and collides at plan time, not live.
# ---------------------------------------------------------------------------
module "db" {
  source = "../../svc-01-sql-database"

  project_slug = var.project_slug
  vlan_id      = local.vlan_id
  host_index   = local.assigned_host_index

  db_engine = var.db_engine
  tier      = var.tier

  proxmox_node_name      = var.proxmox_node_name
  svc01_vmid             = var.svc01_vmid
  svc01_template_file_id = var.svc01_template_file_id
  svc01_template_os_type = var.svc01_template_os_type
  svc01_datastore_id     = var.svc01_datastore_id

  operator_ssh_public_keys = var.operator_ssh_public_keys

  # Provider wiring threaded through to the reused root.
  proxmox_endpoint  = var.proxmox_endpoint
  proxmox_api_token = var.proxmox_api_token
  proxmox_insecure  = var.proxmox_insecure
}
