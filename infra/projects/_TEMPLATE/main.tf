# infra/projects/_TEMPLATE/main.tf
#
# Registry-driven vlan_id lookup + registration precondition (task 4.1).
#
# This root derives its VLAN ID by reading the shared VLAN-ID registry
# (infra/platform-foundation/projects.yaml) and resolving the entry whose slug
# matches var.project_slug — it does NOT accept vlan_id as a free operator input
# (Requirement 5.4, design.md "How a Project Root reads its vlan_id"). Keeping the
# registry as the single in-repo source of VLAN-ID truth avoids a hidden
# pipeline-variable indirection (design.md "Data Models").
#
# The VNet, subnet, and this root's own applier are declared in task 4.2 (a later
# increment) and the vlan_id/subnet_cidr/gateway_ip outputs in task 4.3 — they are
# intentionally NOT in this file yet. This file is only the scaffold + lookup +
# "is this slug registered?" guard.

locals {
  # Parse the committed registry. Relative path walks back from this per-project
  # root (infra/projects/<slug>/) to the platform-foundation root
  # (infra/platform-foundation/), matching the design's illustrative pattern.
  registry = yamldecode(file("${path.module}/../../platform-foundation/projects.yaml"))

  # Resolve the single entry for this project's slug. `one()` returns the sole
  # matching element, or null if the comprehension is empty (slug absent). It
  # would raise if the registry somehow contained two entries for the same slug,
  # which the onboarding script's duplicate-slug guard (Requirement 5.3) prevents.
  entry = one([for p in local.registry.projects : p if p.slug == var.project_slug])

  # The project's assigned VLAN ID, or null when the slug is not registered.
  # Downstream resources (task 4.2) consume local.vlan_id; the precondition below
  # guarantees it is non-null before any resource is created.
  vlan_id = local.entry != null ? local.entry.vlan_id : null
}

# Fail fast, before any SDN resource is created, if the slug is not registered
# (Requirement 5.5, FD.3). `terraform_data` is a built-in resource (Terraform
# >= 1.4, well within this root's `>= 1.6.0` pin) used purely as a precondition
# anchor: it needs no extra provider (unlike null_resource, which would pull in
# hashicorp/null) and creates no real infrastructure. The plan/apply aborts with
# a clear, actionable message telling the operator to onboard the slug first.
resource "terraform_data" "assert_registered" {
  lifecycle {
    precondition {
      condition     = local.vlan_id != null
      error_message = "Slug '${var.project_slug}' is not present in infra/platform-foundation/projects.yaml; onboard it via the onboarding script + merge request first (NET-00 §7)."
    }
  }
}
