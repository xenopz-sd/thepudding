# SVC-09 Reverse Proxy (Traefik) Terraform root — OUTPUT CONTRACT.
#
# These values are this root's public, non-secret contract, consumed by:
#   * the Ansible dynamic inventory generator, which renders host/network facts
#     from Terraform outputs after every successful apply (PF FR-5,
#     integration-boundaries §4) — via the generic `inventory_hosts` list, and
#   * every other service's Ansible role, which consumes `traefik_internal_ip`
#     to confirm it is on a Docker network Traefik can reach (SVC-09 §8).
#
# All values are formula-derived, non-secret configuration — no ACME key, DNS
# API token, or ZITADEL client secret is ever exposed as a Terraform output.
#
# The IP is NOT written as a literal: it re-exposes the address the shared
# `proxmox-compute` module already COMPUTED via `cidrhost("10.0.20.0/24", 10 +
# host_index)` — 10.0.20.12 for host_index 2 (moved from the colliding index 0 /
# 10.0.20.10; NET-00 §3). The module's `ipv4_address` output is the BARE address
# (no `/24`), the form the Ansible inventory wants, so it is surfaced verbatim.

output "traefik_internal_ip" {
  description = "The Traefik LXC's (svc09-20-01) computed static IPv4 (bare address, no CIDR suffix). Consumed by the Ansible dynamic inventory and by every other service's role to confirm reachability of the shared ingress (SVC-09 §8, NET-00 §3)."
  value       = module.traefik.ipv4_address
}

# The dashboard hostname is rendered by the Ansible `traefik_install` role from
# `platform_domain` (an Ansible-layer value), so this root exposes only the
# computed host IP + port; the operator reaches the dashboard via the internal
# route `https://traefik.<platform_domain>/dashboard/` (SVC-09 FR-6/FR-10). The
# port is surfaced here as a non-secret fact for the inventory/handoff banner.
output "traefik_dashboard_internal_endpoint" {
  description = "The Traefik dashboard's internal endpoint host:port on VLAN 20 (SVC-09 §8). Non-secret. Until SVC-06 is deployed the dashboard is reached only via an internal Traefik route protected by the (inert) forward-auth middleware — never an anonymous :8080 (SVC-09 FR-6/FR-10). The public hostname is composed by the Ansible role from platform_domain."
  value       = "${module.traefik.ipv4_address}:443"
}

# Generic per-host inventory surface consumed by the Ansible inventory generator
# (ansible/inventory/generate_inventory.py; PF §8, integration-boundaries §4).
# One object per provisioned guest, built from the proxmox-compute module output
# for the computed facts, with vmid + node_name from the CONCRETE container
# resource (not the module, whose vmid is a null stand-in). Non-secret facts
# only. Mirrors the SVC-07 root's inventory_hosts schema exactly so the shared
# generator keys on it with no change.
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}) consumed by the Ansible inventory generator. Built from module.traefik for the computed facts; vmid + node_name come from the concrete container resource so the real, provider-assigned VMID + node flow downstream to Ansible with no operator lookup. Non-secret facts only."
  value = [
    {
      hostname     = module.traefik.hostname
      ipv4_address = module.traefik.ipv4_address
      service_role = module.traefik.service_role
      project_slug = module.traefik.project_slug
      vmid         = proxmox_virtual_environment_container.traefik.vm_id
      node_name    = proxmox_virtual_environment_container.traefik.node_name
    },
  ]
}
