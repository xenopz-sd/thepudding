# infra/projects/<slug>/workload/outputs.tf  —  Workload_Consumer_Root outputs.
#
# Re-exposes the workload guest's non-secret, formula-derived facts for the
# Ansible dynamic inventory generator and the Consumer_Onboarding_Orchestrator.
# Every output is NULL/empty when the workload guest is DISABLED
# (var.workload_enabled=false => count 0 => the module/resource lists are empty),
# so a project that declares no workload emits no workload facts (Req 2.3).
#
# `one(module.workload[*].x)` returns the sole element of a 1-element list, or
# NULL for a 0-element list — the exact "null when disabled, value when enabled"
# shape for the single-guest path. `try(...[0], null)` is the equivalent guard on
# the concrete container resource's attributes. NO secret is ever an output.

output "workload_enabled" {
  description = "Whether the optional workload guest is provisioned by this root (echoes var.workload_enabled). false => every other output here is null/empty."
  value       = var.workload_enabled
}

output "workload_ipv4_address" {
  description = "The workload guest's computed static IPv4 (bare address) = cidrhost(\"10.0.<vlan_id>.0/24\", 10 + ledger_index) (NET-00 §3). NULL when the guest is disabled."
  value       = one(module.workload[*].ipv4_address)
}

output "workload_hostname" {
  description = "The workload guest's machine-facing hostname `workload-<vlan_id>-<instance>` (ADR-0021 role-code convention). NULL when the guest is disabled."
  value       = one(module.workload[*].hostname)
}

output "vlan_id" {
  description = "The project's VLAN ID, RESOLVED from the registry for project_slug. Never an operator input. Present regardless of whether the workload guest is enabled."
  value       = local.vlan_id
}

output "host_index" {
  description = "The workload guest's host_index, READ from the Host_Index_Authority ledger (role_code=workload row). NULL when the VLAN has no such ledger row (e.g. the guest is disabled and no row was added)."
  value       = local.assigned_host_index
}

output "consumer_workload_state_name" {
  description = "The GitLab-managed Terraform state name for this root, `<slug>-workload` — distinct from `<slug>-infra`, `<slug>-db`, and every service state (one-state-per-service). Echoed for the orchestrator's handoff summary."
  value       = "${var.project_slug}-workload"
}

# Generic per-host inventory surface the Ansible generator keys on (PF §8,
# integration-boundaries §4). EMPTY list when the guest is disabled, so the
# inventory simply gains no workload host. vmid + node_name come from the
# concrete container resource (not the module's null stand-in). Non-secret only.
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. EMPTY when the workload guest is disabled. Non-secret facts only."
  value = [
    for i in range(length(proxmox_virtual_environment_container.workload)) : {
      hostname     = module.workload[i].hostname
      ipv4_address = module.workload[i].ipv4_address
      service_role = module.workload[i].service_role
      project_slug = module.workload[i].project_slug
      vmid         = proxmox_virtual_environment_container.workload[i].vm_id
      node_name    = proxmox_virtual_environment_container.workload[i].node_name
    }
  ]
}
