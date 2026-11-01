# Shared proxmox-compute module — INPUT CONTRACT (task 7.1 scaffold).
#
# This file defines ONLY the input side of the module's input/output contract
# per design.md "Shared proxmox-compute module — input/output contract".
# The computed addressing / hostname / cloud-init logic is task 7.2 and the
# input-guard preconditions are task 7.3 — neither is implemented here.
#
# Design contract fields (in-direction):
#   project_slug | service_name | {svc_code XOR role_code} | vlan_id |
#   host_index | instance | component (optional)
#
# Guest identity (ADR-0021): a guest is identified by EXACTLY ONE of a catalog
# `svc_code` (`svcNN`) OR a non-catalog `role_code` (a role name such as
# `workload`), never both and never neither. `svc_code` keeps its single
# "stable catalog slot" meaning; `role_code` is the first-class non-catalog
# path. The exactly-one-of check is a cross-variable precondition (Guard 5).
# Deliberately ABSENT: any operator-supplied IP literal input. IPs are
# computed by the module (task 7.2) via the cidrhost() formula, never chosen
# by an operator (Requirement 2.4, NET-00 §3). See the note at the bottom.

variable "project_slug" {
  description = <<-EOT
    Project identity (lowercase, hyphenated, <=20 chars — PF §4). Drives the
    Proxmox `tags` attribute `proj-<slug>-<service>` (distinct from the
    hostname) and is echoed back as an output for the Ansible inventory.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.project_slug))
    error_message = "project_slug must be lowercase alphanumeric/hyphen, 1-20 chars, not starting or ending with a hyphen (PF §4)."
  }
}

variable "service_name" {
  description = <<-EOT
    Service name used to build the human-facing Proxmox `tags` value
    `proj-<slug>-<service>` (PF §4). Distinct from `svc_code`, which drives
    the machine-facing hostname.
  EOT
  type        = string
}

variable "svc_code" {
  description = <<-EOT
    Catalog stable service-slot code rendered lowercase with no hyphen, e.g.
    `svc01`, `svc14` (NET-00 §4). Drives the machine-facing hostname
    `<svc-code>-<vlan_id>-<instance>`. MUST be derived from the stable
    catalog slot number, never a product/technology name (Requirement 3.5),
    so a technology swap within a slot never forces a rename.

    OPTIONAL (default null) per ADR-0021: a guest is identified by EXACTLY ONE
    of `svc_code` (a catalog service) OR `role_code` (a non-catalog, role-named
    guest such as a consumer workload). When supplied it MUST be the strict
    `svcNN` catalog form; `role_code` is the path for non-catalog guests. The
    exactly-one-of cross-variable check is enforced in preconditions.tf (Guard
    5) because a variable `validation` block cannot reference another variable.
  EOT
  type        = string
  default     = null

  validation {
    # When supplied, svc_code stays strictly the stable catalog slot `svcNN`
    # (NET-00 §4, Req 3.5). A null is allowed through here (the guest is then
    # identified by role_code instead); the exactly-one-of guard lives in
    # preconditions.tf Guard 5 (ADR-0021).
    condition     = var.svc_code == null || can(regex("^svc[0-9]{2}$", var.svc_code))
    error_message = "svc_code, when set, must be the stable catalog slot rendered as 'svcNN' (lowercase, two digits), e.g. svc01 (NET-00 §4, Requirement 3.5). For a non-catalog, role-named guest use role_code instead (ADR-0021)."
  }
}

variable "role_code" {
  description = <<-EOT
    NON-CATALOG role-code guest identity (ADR-0021). The sanctioned way to name
    a guest that is NOT a catalog SVC-NN service — e.g. a per-project consumer
    `workload` guest. Drives the machine-facing hostname
    `<role_code>-<vlan_id>-<instance>` exactly as `svc_code` would, but is a
    DISTINCT concept: it is keyed on a stable role name (also swap-stable),
    never a catalog slot, so `svc_code` keeps its single "stable catalog slot"
    meaning and is never overloaded.

    OPTIONAL (default null). A guest supplies EXACTLY ONE of `svc_code` OR
    `role_code` — never both, never neither (enforced in preconditions.tf
    Guard 5). When supplied it must be a lowercase role name that is NOT itself
    a catalog `svcNN` code (that is what `svc_code` is for).
  EOT
  type        = string
  default     = null

  validation {
    # A role_code (when supplied) must be a lowercase alphanumeric role name
    # starting with a letter, AND must NOT be a catalog `svcNN` form — a
    # `svcNN` literal belongs in `svc_code`, not here (ADR-0021). A null is
    # allowed through (the guest is then identified by svc_code); the
    # exactly-one-of guard lives in preconditions.tf Guard 5.
    condition = var.role_code == null || (
      can(regex("^[a-z][a-z0-9]*$", var.role_code)) &&
      !can(regex("^svc[0-9]{2}$", var.role_code))
    )
    error_message = "role_code, when set, must be a lowercase role name matching '^[a-z][a-z0-9]*$' (e.g. 'workload') and must NOT be a catalog 'svcNN' code — a catalog slot goes in svc_code, not role_code (ADR-0021)."
  }
}

variable "vlan_id" {
  description = <<-EOT
    The project's assigned VLAN ID, flowing in from the per-project root
    (which reads it from projects.yaml). An integer in 100-254 for a project
    VLAN, or 20 for the shared-services tier. VLAN 10 (management) is a
    rejected placement — the rejecting precondition is added in task 7.3
    (Requirement 1.7). The full addressing/hostname derivation from this
    value is task 7.2.
  EOT
  type        = number
}

variable "host_index" {
  description = <<-EOT
    Zero-based position of this host within its VLAN's host set. Task 7.2
    computes the static IPv4 as cidrhost("10.0.<vlan_id>.0/24", 10 +
    host_index), so index 0 -> .10 (Requirement 2.3). Host-IP exhaustion
    (10 + host_index > 254) is surfaced by a precondition in task 7.3
    (Requirement 2.6).
  EOT
  type        = number
  default     = 0

  validation {
    condition     = var.host_index >= 0
    error_message = "host_index is a zero-based position and must be >= 0 (Requirement 2.3)."
  }
}

variable "peer_host_indices" {
  description = <<-EOT
    The set of OTHER host indices already allocated on THIS guest's VLAN —
    every host_index on this VLAN except this guest's own. Supplied by the
    calling root from the committed per-VLAN host-index ledger
    (infra/platform-foundation/host-indices.yaml, the Host_Index_Authority),
    which is the single source of truth for within-VLAN host-index allocation
    across independent Terraform roots (a shared VLAN such as 20, or a project
    VLAN carrying more than one service root).

    Used by the plan-time precondition in preconditions.tf (Guard 4) to detect
    a within-VLAN host-index collision — two guests on one VLAN computing the
    same cidrhost("10.0.<vlan_id>.0/24", 10 + host_index) address — and FAIL AT
    PLAN TIME before any Proxmox resource is created (Requirement 1.2).

    Defaults to an empty list, which is a strict no-op: a single-guest root (or
    a root that is the sole occupant of its VLAN) passes nothing and applies
    exactly as before (backward-compatible, Requirement 1.2 backward-compat).
  EOT
  type        = list(number)
  default     = []

  validation {
    # Pure-input RANGE guard only: every supplied peer index is a zero-based
    # position and must be >= 0, mirroring host_index's own validation
    # (Requirement 2.3). Set-level collision detection against var.host_index
    # is a cross-value check and lives in the preconditions.tf Guard 4, not
    # here (validation blocks cannot reference other variables).
    condition     = alltrue([for i in var.peer_host_indices : i >= 0])
    error_message = "peer_host_indices entries are zero-based positions and must each be >= 0 (Requirement 2.3)."
  }
}

variable "instance" {
  description = <<-EOT
    Zero-padded two-digit replica counter for the hostname
    `<svc-code>-<vlan_id>-<instance>`, in the range "01".."99" (Requirement
    3.1, 3.3). A singleton within its VLAN is "01"; increment by 1 per
    additional replica of the same service in the same VLAN.
  EOT
  type        = string
  default     = "01"

  validation {
    condition     = can(regex("^[0-9]{2}$", var.instance)) && tonumber(var.instance) >= 1 && tonumber(var.instance) <= 99
    error_message = "instance must be a zero-padded two-digit counter in the range 01-99 (Requirement 3.1)."
  }
}

variable "component" {
  description = <<-EOT
    Optional lowercase component name. WHERE one catalog line item deploys
    more than one distinct running service, the hostname becomes
    `<svc-code>-<component>-<vlan_id>-<instance>` (Requirement 3.2, NET-00
    §4). Null/omitted for the common single-component case. Consumed by the
    hostname derivation in task 7.2.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.component == null || can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", var.component))
    error_message = "component, when set, must be lowercase alphanumeric/hyphen not starting or ending with a hyphen (NET-00 §4)."
  }
}

# ---------------------------------------------------------------------------
# Deliberately NOT declared: an operator-supplied IP literal input
# (e.g. `ip_config`, `ipv4_address`, `static_ip`). Requirement 2.4 mandates
# that the host address be derived SOLELY from Terraform-computed values and
# that any operator-supplied IP literal be rejected. Rather than accept such
# an input and then reject it, this module does not expose one at all — the
# address is computed in task 7.2 from `vlan_id` + `host_index`. This absence
# IS the contract.
# ---------------------------------------------------------------------------

variable "ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC keys to inject into a consuming guest's root account via
    the bpg/proxmox `initialization { user_account { keys = [...] } }` mechanism.
    PUBLIC keys only — never a private key, password, or other secret (Req 1.4).
    Defaults to an empty list, which is a strict no-op: a consuming root that does
    not set this applies exactly as before (backward-compatible, Req 1.1, 1.3).

    NOTE: this module declares no guest resource of its own yet (see the MODULE
    SCOPE note in locals.tf), so it does not itself inject these keys — it echoes
    them via the `ssh_public_keys` output for a consuming root to inject. When a
    later increment gives this module its own guest resource, the injection moves
    inward with no contract change.
  EOT
  type        = list(string)
  default     = []

  validation {
    # Public-key shape guard: each entry must look like an OpenSSH PUBLIC key
    # (ssh-ed25519 / ssh-rsa / ecdsa-sha2-*) and must NOT contain a PEM private-key
    # header — a private key or secret value is rejected before any apply (Req 1.4).
    condition = alltrue([
      for k in var.ssh_public_keys :
      can(regex("^(ssh-(ed25519|rsa)|ecdsa-sha2-\\S+)\\s+\\S+", k)) && !can(regex("PRIVATE KEY", k))
    ])
    error_message = "ssh_public_keys entries must be OpenSSH PUBLIC keys (ssh-ed25519 / ssh-rsa / ecdsa-sha2-*); a private-key or secret value is rejected (Req 1.4)."
  }
}
