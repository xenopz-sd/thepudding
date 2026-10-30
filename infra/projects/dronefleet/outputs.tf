# infra/projects/_TEMPLATE/outputs.tf
#
# Project root outputs (task 4.3, Requirement 6.4; design.md
# "Components/Interfaces — Project Root", Outputs).
#
# These three values are the project root's public contract, consumed by:
#   * each SVC-xx service's own Terraform (which threads vlan_id into the shared
#     proxmox-compute module to compute host IPs and hostnames), and
#   * the Ansible dynamic inventory generator (which renders host/network facts
#     from Terraform outputs after every apply — PF FR-5, integration-boundaries §4).
#
# All three are pure formula-derived, non-secret configuration (Security AC 4):
#   * vlan_id     — the project's assigned VLAN ID, resolved from the registry
#                   (local.vlan_id, main.tf). Non-null is guaranteed by the
#                   terraform_data.assert_registered precondition (Requirement 5.5)
#                   and constrained to 100-254 by the VNet precondition (sdn.tf).
#   * subnet_cidr — 10.0.<vlan_id>.0/24 (local.subnet_cidr, sdn.tf; Requirement 2.1).
#   * gateway_ip  — 10.0.<vlan_id>.1   (local.gateway_ip,  sdn.tf; Requirement 2.1).
#
# They simply re-expose the locals already computed elsewhere in this root; the
# formula lives in one place (sdn.tf) and is surfaced here, not recomputed.

output "vlan_id" {
  description = "The project's assigned VLAN ID (integer, 100-254), resolved from the platform-foundation registry for var.project_slug. Consumed by each service's Terraform (threaded into proxmox-compute) and the Ansible dynamic inventory generator."
  value       = local.vlan_id
}

output "subnet_cidr" {
  description = "The project's /24 subnet CIDR, equal to 10.0.<vlan_id>.0/24 (NET-00 §3, Requirement 6.4). Non-secret, formula-derived from the assigned vlan_id."
  value       = local.subnet_cidr
}

output "gateway_ip" {
  description = "The project's subnet gateway address, equal to 10.0.<vlan_id>.1 (NET-00 §3, Requirement 6.4). Non-secret, formula-derived from the assigned vlan_id."
  value       = local.gateway_ip
}
