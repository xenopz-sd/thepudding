# Shared proxmox-compute module — OUTPUT CONTRACT.
#
# Exposes the five contract outputs from design.md's proxmox-compute
# input/output table: hostname, ipv4_address, vmid, service_role,
# project_slug — consumed by the Ansible dynamic inventory generator (PF §8,
# integration-boundaries §4) — plus the derived helpers a consuming guest
# resource needs to inject the static address via cloud-init (ip_config,
# gateway, subnet_cidr, tags).
#
# STATUS (task 7.2 done; 7.3 preconditions pending):
#   - project_slug : REAL — echoes the input directly, final.
#   - hostname     : REAL — `<svc-code>[-<component>]-<vlan_id>-<instance>`
#                    (Req 3.1, 3.2), computed in locals.tf.
#   - ipv4_address : REAL — cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)
#                    (Req 2.1, 2.3), computed in locals.tf.
#   - ip_config    : REAL — cloud-init initialization.ip_config.ipv4 structure
#                    (CIDR address + gateway) a guest resource injects (Req 2.5).
#   - tags         : REAL — ["proj-<slug>-<service>"], distinct from hostname
#                    (Req 3.4).
#   - vmid         : stand-in (null) — no guest resource exists in this module's
#                    current scope; set from the created VM/LXC in a later
#                    increment (see locals.tf scope decision).
#   - service_role : REAL — the service identity the Ansible inventory groups by.
#
# The input-guard preconditions (reject vlan_id == 10, within-VLAN hostname
# collision, host-IP exhaustion) are task 7.3 and are not yet added.

output "hostname" {
  description = "Machine-facing hostname `<svc-code>[-<component>]-<vlan_id>-<instance>` (NET-00 §4, Req 3.1/3.2)."
  value       = local.hostname
}

output "ipv4_address" {
  description = "Computed static IPv4 `cidrhost(\"10.0.<vlan_id>.0/24\", 10 + host_index)` (NET-00 §3, Requirement 2.1/2.3). Bare address (no CIDR suffix) for the Ansible inventory."
  value       = local.ipv4_address
}

output "ip_config" {
  description = <<-EOT
    Cloud-init `initialization.ip_config.ipv4` structure a consuming guest
    resource injects: `{ ipv4 = { address = "10.0.<vlan_id>.<host>/24",
    gateway = "10.0.<vlan_id>.1" } }` (Requirement 2.5, NET-00 §3). The address
    is Terraform-computed, never operator-supplied (Requirement 2.4); the
    Proxmox SDN built-in IPAM/DHCP is deliberately left disabled.
  EOT
  value       = local.ip_config
}

output "gateway" {
  description = "Subnet gateway `10.0.<vlan_id>.1` (NET-00 §3), for a consuming guest's cloud-init and route config."
  value       = local.gateway
}

output "subnet_cidr" {
  description = "The host's `/24` subnet `10.0.<vlan_id>.0/24` (NET-00 §3)."
  value       = local.subnet_cidr
}

output "tags" {
  description = "Proxmox `tags` list `[\"proj-<slug>-<service>\"]` (PF §4, Requirement 3.4). Human-facing, project-correlated — DISTINCT from the machine-facing, VLAN-correlated hostname."
  value       = local.tags
}

output "vmid" {
  description = "Proxmox VMID of the provisioned guest. Stand-in (null) until the guest resource is wired in a later increment; see the scope decision in locals.tf."
  value       = local.vmid
}

output "service_role" {
  description = "Service role the Ansible dynamic inventory groups this host by (PF §8, integration-boundaries §4)."
  value       = local.service_role
}

output "project_slug" {
  description = "Project identity, echoed back for the Ansible inventory (PF §8)."
  value       = var.project_slug
}

output "ssh_public_keys" {
  description = "Echo of the ssh_public_keys input (forward-compat). This module performs no injection — it declares no guest resource yet; a consuming root injects these into its guest's initialization.user_account.keys. When a future increment gives this module its own guest resource, injection moves inward with no contract change."
  value       = var.ssh_public_keys
}
