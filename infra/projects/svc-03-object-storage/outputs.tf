output "garage_node_ips" {
  description = "The three Garage nodes' computed static IPv4 addresses (bare), in node order. Non-secret."
  value = [
    module.node1.ipv4_address,
    module.node2.ipv4_address,
    module.node3.ipv4_address,
  ]
}

output "garage_s3_endpoint" {
  description = "Direct VLAN-20 S3 endpoint of node 1 (plain HTTP, internal). External clients use the Traefik route https://s3.<platform_domain>."
  value       = "http://${module.node1.ipv4_address}:3900"
}

output "garage_admin_endpoint" {
  description = "Direct VLAN-20 admin API endpoint of node 1 (bearer admin token required, except /health and /metrics)."
  value       = "http://${module.node1.ipv4_address}:3903"
}

output "inventory_hosts" {
  description = "Generic per-host inventory surface ({hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by ansible/inventory/generate_inventory.py. Non-secret facts only."
  value = [
    {
      hostname     = module.node1.hostname
      ipv4_address = module.node1.ipv4_address
      service_role = module.node1.service_role
      project_slug = module.node1.project_slug
      vmid         = proxmox_virtual_environment_container.node1.vm_id
      node_name    = proxmox_virtual_environment_container.node1.node_name
    },
    {
      hostname     = module.node2.hostname
      ipv4_address = module.node2.ipv4_address
      service_role = module.node2.service_role
      project_slug = module.node2.project_slug
      vmid         = proxmox_virtual_environment_container.node2.vm_id
      node_name    = proxmox_virtual_environment_container.node2.node_name
    },
    {
      hostname     = module.node3.hostname
      ipv4_address = module.node3.ipv4_address
      service_role = module.node3.service_role
      project_slug = module.node3.project_slug
      vmid         = proxmox_virtual_environment_container.node3.vm_id
      node_name    = proxmox_virtual_environment_container.node3.node_name
    },
  ]
}
