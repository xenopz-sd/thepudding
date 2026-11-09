output "mailcatcher_internal_ip" {
  description = "The catcher LXC's (mailcatcher-20-01) computed static IPv4 (bare address). ZITADEL's SMTP target and the Traefik route backend. Non-secret."
  value       = module.mailcatcher.ipv4_address
}

output "inventory_hosts" {
  description = "Generic per-host inventory surface ({hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by ansible/inventory/generate_inventory.py. Non-secret facts only."
  value = [
    {
      hostname     = module.mailcatcher.hostname
      ipv4_address = module.mailcatcher.ipv4_address
      service_role = module.mailcatcher.service_role
      project_slug = module.mailcatcher.project_slug
      vmid         = proxmox_virtual_environment_container.mailcatcher.vm_id
      node_name    = proxmox_virtual_environment_container.mailcatcher.node_name
    },
  ]
}
