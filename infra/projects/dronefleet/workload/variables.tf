# infra/projects/<slug>/workload/variables.tf  —  Workload_Consumer_Root inputs.
#
# The ONLY project-identifying input is project_slug. CRUCIALLY, this root does
# NOT accept vlan_id or host_index as free operator inputs — vlan_id is RESOLVED
# from the registry (main.tf) and host_index is READ from the Host_Index_Authority
# ledger (main.tf, ADR-0015). Everything else is per-cluster plumbing (node,
# template, vmid, storage) threaded into the count-gated workload guest, mirroring
# the sibling db/ root's input surface.
#
# The workload guest is OPTIONAL: `workload_enabled` defaults to FALSE and
# `workload_count` to 1. A project that does not enable it provisions nothing
# here (Req 2.3 strict no-op). dronefleet keeps workload_enabled=false by default
# — this reference root does NOT enable the guest on a bare apply.

variable "project_slug" {
  description = <<-EOT
    The Consumer_Project's slug. Must exactly match a `slug` entry already
    recorded in infra/platform-foundation/projects.yaml, assigned once by the
    onboarding script + CI MR flow (NET-00 §7). Lowercase, hyphenated, <=20
    chars (PF §4). This root reads the project's vlan_id from the registry and —
    when the workload is enabled — its workload host_index from host-indices.yaml
    using this slug; if either is absent the plan fails rather than provisioning
    with a guessed VLAN or a colliding default index.

    Defaulted to the scaffold/test slug `dronefleet` so this example `workload/`
    root is self-contained; a real per-project copy overrides it (or passes
    `-var project_slug=<slug>`).
  EOT
  type        = string
  default     = "dronefleet"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.project_slug))
    error_message = "project_slug must be lowercase alphanumeric with internal hyphens and at most 20 characters (PF §4)."
  }
}

variable "workload_enabled" {
  description = <<-EOT
    OPT-IN (default FALSE): provision the optional consumer workload guest on the
    project VLAN. When false (the default), this root creates NO workload guest
    and behaves exactly as before the consumer last-mile feature — a strict no-op
    (Req 2.3). The consumer orchestrator sets this from the manifest's
    `workload.enabled` block; a bare `terraform apply` of this reference root
    leaves it off.
  EOT
  type        = bool
  default     = false
}

variable "workload_count" {
  description = <<-EOT
    Number of workload guests to provision WHEN workload_enabled is true. Default
    1 (the single-guest validated path). The effective guest count is
    `workload_enabled ? workload_count : 0`, so this is ignored while the guest is
    disabled. count > 1 is schema-accepted but reserved for a later increment
    (design.md Component 1); the ledger/instance wiring here assumes a single
    workload guest.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.workload_count >= 1
    error_message = "workload_count must be a positive integer (>= 1)."
  }
}

variable "tier" {
  description = <<-EOT
    Sizing tier for the workload guest: `small` (1 vCPU / 2 GB / 20 GB) or
    `medium` (4 vCPU / 8 GB / 100 GB). Maps to cpu/memory/disk via locals below;
    each is individually overridable for an off-tier size. Mirrors the db/ root's
    tier bands (reused DATABASE_TIERS sizing).
  EOT
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.tier)
    error_message = "tier must be 'small' or 'medium'."
  }
}

variable "cpu_cores" {
  description = "Override vCPU count. Null (default) -> the tier default (small=1, medium=4)."
  type        = number
  default     = null
}

variable "memory_mb" {
  description = "Override RAM (MB). Null (default) -> the tier default (small=2048, medium=8192)."
  type        = number
  default     = null
}

variable "disk_gb" {
  description = "Override rootfs size (GB). Null (default) -> the tier default (small=20, medium=100)."
  type        = number
  default     = null
}

variable "proxmox_node_name" {
  description = "Proxmox VE node the workload LXC is created on. Supplied per-cluster at apply time. Required only when the workload guest is enabled."
  type        = string
  default     = ""
}

variable "workload_vmid" {
  description = "Proxmox VMID for the workload LXC. Supplied explicitly so instances get stable, non-colliding IDs. Required only when the workload guest is enabled."
  type        = number
  default     = 0
}

variable "workload_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template (Docker + Compose installed per
    host by the `common` role, ADR-0003). Form `<datastore>:vztmpl/<file>`.
    Defaulted empty; validated non-empty only via the apply path when the guest
    is enabled (an empty value with the guest disabled is a no-op).
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.workload_template_file_id == "" || can(regex("^[^:]+:vztmpl/.+", var.workload_template_file_id))
    error_message = "workload_template_file_id, when set, must be '<datastore>:vztmpl/<file_name>'."
  }
}

variable "workload_template_os_type" {
  description = "operating_system.type for the template — `debian` (default) or `ubuntu`."
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.workload_template_os_type)
    error_message = "workload_template_os_type must be 'debian' or 'ubuntu'."
  }
}

variable "workload_datastore_id" {
  description = "Proxmox storage pool for the workload LXC rootfs (default local-zfs per PF §6/§7)."
  type        = string
  default     = "local-zfs"
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into the workload LXC root account so
    Ansible can reach it. PUBLIC keys only; sourced from
    TF_VAR_operator_ssh_public_keys / gitignored .env, never committed. Default
    [] = strict no-op (SSH-unreachable until supplied).
  EOT
  type        = list(string)
  default     = []
}

locals {
  # Tier -> default sizing (mirrors the db/ root). Individual overrides win when
  # non-null.
  _tier_defaults = {
    small  = { cpu = 1, mem = 2048, disk = 20 }
    medium = { cpu = 4, mem = 8192, disk = 100 }
  }
  workload_cpu_cores = var.cpu_cores != null ? var.cpu_cores : local._tier_defaults[var.tier].cpu
  workload_memory_mb = var.memory_mb != null ? var.memory_mb : local._tier_defaults[var.tier].mem
  workload_disk_gb   = var.disk_gb != null ? var.disk_gb : local._tier_defaults[var.tier].disk

  # The project's SDN VNet this workload guest attaches to — referenced, not
  # created (the project's network-slice root owns VNet creation). p<vlan>.
  workload_vnet_id = "p${local.vlan_id}"
}
