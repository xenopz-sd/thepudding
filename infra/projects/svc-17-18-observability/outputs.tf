# SVC-17/18 Observability (Prometheus + Grafana + Loki) Terraform root — OUTPUT
# CONTRACT.
#
# These values are this root's public, non-secret contract, consumed by:
#   * the Ansible dynamic inventory generator, which renders host/network facts
#     from Terraform outputs after every successful apply (PF FR-5,
#     integration-boundaries §4) — via the generic `inventory_hosts` list, and
#   * every other service's Ansible role: `loki_push_url` is the internal Loki
#     push endpoint every service's Promtail ships logs to, and `grafana_url` is
#     the external (Traefik-fronted) Grafana address operators browse to
#     (SVC-17/18 §8, FR-13).
#
# All values are formula-derived, non-secret configuration — no credential,
# datasource password, or admin token is ever exposed as a Terraform output.
#
# IPs are NOT written as literals: they re-expose the addresses the shared
# `proxmox-compute` module already COMPUTED via
# `cidrhost("10.0.20.0/24", 10 + host_index)` — 10.0.20.18 / .19 / .20 for host
# indices 8 / 9 / 10 (NET-00 §3). Each module's `ipv4_address` output is the
# BARE address (no `/24`), the form the Ansible inventory wants, so it is
# surfaced verbatim.

output "prometheus_internal_ip" {
  description = "The Prometheus LXC's (svc17-prometheus-20-01) computed static IPv4 (bare address, no CIDR suffix). Non-secret internal VLAN-20 fact (NET-00 §3)."
  value       = module.prometheus.ipv4_address
}

output "grafana_internal_ip" {
  description = "The Grafana LXC's (svc17-grafana-20-01) computed static IPv4 (bare address, no CIDR suffix). Non-secret internal VLAN-20 fact (NET-00 §3)."
  value       = module.grafana.ipv4_address
}

# The Grafana dashboard is reached externally only via Traefik (SVC-09); the
# Host rule is `grafana.<platform_domain>` (SVC-17/18 FR-13, §8). This root
# exposes the composed external URL as a non-secret fact for the inventory /
# handoff banner. platform_domain is an Ansible-layer value carried through this
# root so a single cluster.env drives both layers.
output "grafana_url" {
  description = "The external Grafana URL fronted by Traefik — https://grafana.<platform_domain> (SVC-17/18 FR-13, §8). Non-secret; the actual Host rule/labels are rendered by the Ansible observability_stack role."
  value       = "https://grafana.${var.platform_domain}"
}

output "loki_internal_ip" {
  description = "The Loki LXC's (svc18-loki-20-01) computed static IPv4 (bare address, no CIDR suffix). Non-secret internal VLAN-20 fact (NET-00 §3)."
  value       = module.loki.ipv4_address
}

# Loki's push API is INTERNAL-only on VLAN 20 (never fronted by Traefik) — every
# other service's Promtail ships logs here (SVC-17/18 §8). The URL is composed
# from the module's computed IP so no address literal appears; plaintext HTTP is
# acceptable here because this is Compose-/VLAN-internal traffic on 10.0.20.20
# (security-standards "TLS and transport security").
output "loki_push_url" {
  description = "The internal Loki push endpoint consumed by every other service's Promtail — http://<loki_ip>:3100/loki/api/v1/push (SVC-17/18 §8). Internal VLAN-20 only, never via Traefik. Non-secret."
  value       = "http://${module.loki.ipv4_address}:3100/loki/api/v1/push"
}

# Generic per-host inventory surface consumed by the Ansible inventory generator
# (ansible/inventory/generate_inventory.py; PF §8, integration-boundaries §4).
# One object per provisioned guest (prometheus, grafana, loki), built from the
# proxmox-compute module output for the computed facts, with vmid + node_name
# from the CONCRETE container resource (not the module, whose vmid is a null
# stand-in). Non-secret facts only. Mirrors the SVC-09/SVC-07 root's
# inventory_hosts schema exactly so the shared generator keys on it with no
# change — extended here from one entry to three, one per observability guest.
output "inventory_hosts" {
  description = "Generic per-host inventory surface (list of {hostname, ipv4_address, service_role, project_slug, vmid, node_name}), one object per observability guest (prometheus, grafana, loki), consumed by the Ansible inventory generator. Built from each proxmox-compute module for the computed facts; vmid + node_name come from the concrete container resource so the real, provider-assigned VMID + node flow downstream to Ansible with no operator lookup. Non-secret facts only."
  value = [
    {
      hostname     = module.prometheus.hostname
      ipv4_address = module.prometheus.ipv4_address
      service_role = module.prometheus.service_role
      project_slug = module.prometheus.project_slug
      vmid         = proxmox_virtual_environment_container.prometheus.vm_id
      node_name    = proxmox_virtual_environment_container.prometheus.node_name
    },
    {
      hostname     = module.grafana.hostname
      ipv4_address = module.grafana.ipv4_address
      service_role = module.grafana.service_role
      project_slug = module.grafana.project_slug
      vmid         = proxmox_virtual_environment_container.grafana.vm_id
      node_name    = proxmox_virtual_environment_container.grafana.node_name
    },
    {
      hostname     = module.loki.hostname
      ipv4_address = module.loki.ipv4_address
      service_role = module.loki.service_role
      project_slug = module.loki.project_slug
      vmid         = proxmox_virtual_environment_container.loki.vm_id
      node_name    = proxmox_virtual_environment_container.loki.node_name
    },
  ]
}
