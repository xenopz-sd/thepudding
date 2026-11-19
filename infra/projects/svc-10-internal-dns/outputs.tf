output "dns_resolver_ip" {
  description = "The DNS LXC's (svc10-20-01) computed static IPv4 (bare address). The resolver address developer machines point the platform domain at. Non-secret."
  value       = module.dns.ipv4_address
}

output "inventory_hosts" {
  description = "Generic per-host inventory surface ({hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by ansible/inventory/generate_inventory.py. Non-secret facts only."
  value = [
    {
      hostname     = module.dns.hostname
      ipv4_address = module.dns.ipv4_address
      service_role = module.dns.service_role
      project_slug = module.dns.project_slug
      vmid         = proxmox_virtual_environment_container.dns.vm_id
      node_name    = proxmox_virtual_environment_container.dns.node_name
    },
  ]
}
