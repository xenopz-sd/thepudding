# SVC-01 SQL Database Terraform root — OUTPUT CONTRACT.
#
# Non-secret, formula-derived values consumed by the Ansible dynamic inventory
# generator (via the generic inventory_hosts list) and by the consuming project's
# own stack (db_host/db_port/db_engine) when templating connection details into
# OpenBao. NO secret (password) is ever a Terraform output.

output "db_host" {
  description = "The DB LXC's computed static IPv4 (bare address, no CIDR suffix). Consumed by the Ansible inventory and by the consuming project to reach the wire-protocol endpoint on its VLAN (SVC-01 §8, NET-00 §3)."
  value       = module.db.ipv4_address
}

output "db_port" {
  description = "The DB wire-protocol port: 5432 (postgres) or 3306 (mariadb), derived from db_engine. Not proxied by Traefik (wire protocol, SVC-01 §12 exception)."
  value       = local.svc01_db_port
}

output "db_engine" {
  description = "The selected engine (postgres | mariadb), echoed for the consuming project / downstream config."
  value       = var.db_engine
}

# Generic per-host inventory surface the Ansible generator keys on (PF §8,
# integration-boundaries §4). vmid + node_name from the concrete container
# resource (not the module's null stand-in). Non-secret facts only.
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. Non-secret facts only."
  value = [
    {
      hostname     = module.db.hostname
      ipv4_address = module.db.ipv4_address
      service_role = module.db.service_role
      project_slug = module.db.project_slug
      vmid         = proxmox_virtual_environment_container.db.vm_id
      node_name    = proxmox_virtual_environment_container.db.node_name
    },
  ]
}
