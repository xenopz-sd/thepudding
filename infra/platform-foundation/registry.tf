# infra/platform-foundation/registry.tf
#
# Reads the VLAN-ID registry (projects.yaml) as the single in-repo source of
# VLAN-ID truth (NET-00 §7, design.md "Data Models"). Exposes:
#   - a decoded local for downstream use (applier hashing, tooling),
#   - a slug -> vlan_id map output for tooling and the Ansible inventory side,
#   - a stable content hash of the registry that the SDN applier's
#     replace_triggered_by watches (task 1.2), so a registry change re-commits
#     the SDN configuration.
#
# This file does NOT declare the SDN zone or applier (task 1.2) and does NOT
# declare provider/version/backend config (task 1.1, versions.tf/providers.tf).

locals {
  # Parse the committed registry file. yamldecode keeps the registry as the
  # single source of truth and avoids a hidden pipeline-variable indirection
  # (design.md "How a Project Root reads its vlan_id").
  registry = yamldecode(file("${path.module}/projects.yaml"))

  # All registry entries (list of { slug, vlan_id, onboarding_date }).
  registry_projects = local.registry.projects

  # slug -> vlan_id map, consumed by tooling and exposed as an output below.
  slug_to_vlan_id = {
    for p in local.registry_projects : p.slug => p.vlan_id
  }

  # Stable content hash of the registry file. The single cluster-wide
  # proxmox_sdn_applier (task 1.2) watches this via replace_triggered_by so
  # that any change to projects.yaml re-triggers an SDN commit.
  registry_hash = filesha256("${path.module}/projects.yaml")
}

output "slug_to_vlan_id" {
  description = "Map of project slug to assigned VLAN ID, decoded from projects.yaml. Consumed by tooling and the Ansible dynamic-inventory side."
  value       = local.slug_to_vlan_id
}

output "registry_hash" {
  description = "SHA-256 of projects.yaml. Watched by the SDN applier's replace_triggered_by so a registry change re-commits the SDN configuration."
  value       = local.registry_hash
}
