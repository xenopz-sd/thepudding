# infra/projects/<slug>/db/outputs.tf  —  SVC01_Consumer_Root output contract.
#
# Re-exposes the reused SVC-01 module's non-secret, formula-derived outputs for
# the Ansible dynamic inventory generator and the Consumer_Onboarding_Orchestrator
# (which needs db_host/db_port to template the connection into OpenBao). Plus the
# resolved vlan_id/host_index for the orchestrator's handoff summary. NO secret
# (DB password) is ever a Terraform output — it is written to OpenBao by the
# Ansible layer (Req 8.1).

output "db_host" {
  description = "The DB LXC's computed static IPv4 (bare address). = cidrhost(\"10.0.<vlan_id>.0/24\", 10 + host_index) (NET-00 §3, Req 3.4)."
  value       = module.db.db_host
}

output "db_port" {
  description = "The DB wire-protocol port: 5432 (postgres) or 3306 (mariadb). Not proxied by Traefik (SVC-01 §12 exception)."
  value       = module.db.db_port
}

output "db_engine" {
  description = "The selected engine (postgres | mariadb), echoed for downstream config."
  value       = module.db.db_engine
}

output "vlan_id" {
  description = "The project's VLAN ID, RESOLVED from the registry for project_slug (Req 2.1, 3.1). Never an operator input."
  value       = local.vlan_id
}

output "host_index" {
  description = "The DB guest's host_index, READ from the Host_Index_Authority ledger (Req 3.8). Never an operator input."
  value       = local.assigned_host_index
}

output "consumer_db_state_name" {
  description = "The GitLab-managed Terraform state name for this root, `<slug>-db` (Req 3.5) — distinct from `<slug>-infra` and every service state. Echoed so the orchestrator can reference it in the handoff summary."
  value       = "${var.project_slug}-db"
}

# The generic per-host inventory surface the Ansible generator keys on (PF §8,
# integration-boundaries §4), passed straight through from the SVC-01 module.
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. Non-secret facts only."
  value       = module.db.inventory_hosts
}
