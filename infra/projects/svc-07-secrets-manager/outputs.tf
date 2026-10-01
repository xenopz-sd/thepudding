# SVC-07 Secrets Manager (OpenBao) Terraform root — OUTPUT CONTRACT (task 2.3,
# Requirement 9.7; design.md "Components/Interfaces — SVC-07 Terraform root",
# Outputs).
#
# These three values are this root's public, non-secret contract, consumed by:
#   * the Ansible dynamic inventory generator, which renders host/network facts
#     from Terraform outputs after every successful apply (PF FR-5,
#     integration-boundaries §4), and
#   * downstream OpenBao-Agent bootstraps on every other host, which need the
#     primary's API address:port to authenticate via AppRole and pull their own
#     scoped secrets at container-start time (integration-boundaries §2).
#
# All three are formula-derived, non-secret configuration — no Root_Token,
# Bootstrap_Transit_Token, TLS key, or dynamic credential is ever exposed as a
# Terraform output.
#
# The two IPs are NOT written as literals here: they re-expose the addresses the
# shared `proxmox-compute` module already COMPUTED via
# `cidrhost("10.0.20.0/24", 10 + host_index)` — 10.0.20.10 for the primary
# (host_index 0) and 10.0.20.11 for the unsealer (host_index 1) (Requirement 9.2,
# 9.3; NET-00 §3). The module's `ipv4_address` output is the BARE address (no
# `/24` CIDR suffix — that suffix lives only on `ip_config.ipv4.address`), which
# is exactly the form the Ansible inventory wants, so it is surfaced verbatim.

output "openbao_internal_ip" {
  description = "The primary OpenBao LXC's (svc07-20-01) computed static IPv4 (10.0.20.10), bare address with no CIDR suffix. Consumed by the Ansible dynamic inventory and by downstream OpenBao-Agent bootstraps as the OpenBao API host (Requirement 9.7, NET-00 §3)."
  value       = module.primary.ipv4_address
}

output "openbao_api_port" {
  description = "The OpenBao API/listener port (8200) served behind Traefik on the primary. Combined with openbao_internal_ip by downstream OpenBao-Agent bootstraps to reach the API (Requirement 9.7)."
  value       = 8200
}

output "openbao_unsealer_internal_ip" {
  description = "The Transit unsealer LXC's (svc07-unsealer-20-01) computed static IPv4 (10.0.20.11), bare address with no CIDR suffix. Consumed by the Ansible dynamic inventory (Requirement 9.7, NET-00 §3)."
  value       = module.unsealer.ipv4_address
}
# Generic per-host inventory surface consumed by the Ansible inventory generator
# (ansible/inventory/generate_inventory.py; PF §8, integration-boundaries §4). One
# object per provisioned guest, built from the proxmox-compute module outputs.
# Non-secret host/network facts only. Any root that wants to be inventoried emits
# an `inventory_hosts` output of THIS schema; the generator keys on it (not on the
# service-specific openbao_* outputs above). `vmid` is the module's null stand-in
# for now — the generator tolerates a null vmid (Q3).
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. Built from module.primary/module.unsealer for the computed facts; vmid and node_name come from the CONCRETE container resources (not the module, whose vmid is a null stand-in — the shared proxmox-compute module computes addresses/hostnames only and owns no guest resource). This makes the real, provider-confirmed VMID + node flow downstream to Ansible automatically, with no operator VMID lookup (Fix 1, ADR-0007; a deterministic module-computed VMID scheme is the separate Fix-2 follow-up). Non-secret facts only."
  value = [
    {
      hostname     = module.primary.hostname
      ipv4_address = module.primary.ipv4_address
      service_role = module.primary.service_role
      project_slug = module.primary.project_slug
      # Real, provider-assigned VMID + node from the concrete container resource,
      # NOT module.primary.vmid (a documented null stand-in). Terraform carries
      # the ID downstream so Ansible never has to look it up (Fix 1).
      vmid      = proxmox_virtual_environment_container.primary.vm_id
      node_name = proxmox_virtual_environment_container.primary.node_name
    },
    {
      hostname     = module.unsealer.hostname
      ipv4_address = module.unsealer.ipv4_address
      service_role = module.unsealer.service_role
      project_slug = module.unsealer.project_slug
      vmid         = proxmox_virtual_environment_container.unsealer.vm_id
      node_name    = proxmox_virtual_environment_container.unsealer.node_name
    },
  ]
}