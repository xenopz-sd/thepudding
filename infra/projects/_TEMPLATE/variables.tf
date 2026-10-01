# Per-project Terraform root — project-identifying input.
#
# `project_slug` is the ONLY project-identifying input this root accepts
# (design.md "Components/Interfaces — Project Root"). Crucially, the root does
# NOT accept `vlan_id` as a free operator input: the vlan_id is derived by
# looking the slug up in the shared registry (main.tf, Requirement 5.4). This is
# the "read, don't choose" contract — no human ever picks a VLAN ID here.

variable "project_slug" {
  description = <<-EOT
    The project's slug. Must exactly match a `slug` entry already recorded in
    the platform-foundation registry (infra/platform-foundation/projects.yaml),
    which was assigned once by the onboarding script + CI merge-request flow
    (NET-00 §7). The slug is lowercase, hyphenated, and <=20 characters (PF §4).
    This root reads the project's vlan_id from the registry using this slug; if
    the slug is absent, the plan/apply fails (Requirement 5.5, FD.3) rather than
    provisioning an unregistered VLAN.
  EOT
  type        = string

  validation {
    # Mirror the registry's own slug rule (PF §4): lowercase letters/digits,
    # hyphen-separated, <=20 chars. This is a fast local guard; the
    # authoritative membership check is the registry precondition in main.tf.
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.project_slug))
    error_message = "project_slug must be lowercase alphanumeric with internal hyphens and at most 20 characters (PF §4)."
  }
}
