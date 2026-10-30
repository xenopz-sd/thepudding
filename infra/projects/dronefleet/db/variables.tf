# infra/projects/<slug>/db/variables.tf  —  SVC01_Consumer_Root inputs.
#
# The ONLY project-identifying input is project_slug. CRUCIALLY, this root does
# NOT accept vlan_id or host_index as free operator inputs — vlan_id is RESOLVED
# from the registry (main.tf, Req 2.3) and host_index is READ from the
# Host_Index_Authority ledger (main.tf, Req 3.8). Everything else is per-cluster
# plumbing (node, template, vmid, storage) threaded into the reused SVC-01
# module, mirroring the SVC-01 root's own input surface.

variable "project_slug" {
  description = <<-EOT
    The Consumer_Project's slug. Must exactly match a `slug` entry already
    recorded in infra/platform-foundation/projects.yaml, assigned once by the
    onboarding script + CI MR flow (NET-00 §7). Lowercase, hyphenated, <=20
    chars (PF §4). This root reads the project's vlan_id from the registry and
    its DB host_index from host-indices.yaml using this slug; if either is
    absent the plan fails (Req 2.2, 3.7) rather than provisioning with a guessed
    VLAN or a colliding default index.

    Defaulted to the scaffold/test slug `dronefleet` so this example `db/` root
    is self-contained; a real per-project copy overrides it (or passes
    `-var project_slug=<slug>`).
  EOT
  type        = string
  default     = "dronefleet"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.project_slug))
    error_message = "project_slug must be lowercase alphanumeric with internal hyphens and at most 20 characters (PF §4)."
  }
}

variable "db_engine" {
  description = "Relational engine threaded into the SVC-01 module: `postgres` (default) or `mariadb` (strict-EU, domain-rules §4). Drives the tag (proj-<slug>-postgres|-mariadb) and the wire port (5432|3306)."
  type        = string
  default     = "postgres"

  validation {
    condition     = contains(["postgres", "mariadb"], var.db_engine)
    error_message = "db_engine must be 'postgres' (default) or 'mariadb'."
  }
}

variable "tier" {
  description = "Sizing tier threaded into the SVC-01 module: `small` (1 vCPU/2 GB/20 GB) or `medium` (4/8192/100)."
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.tier)
    error_message = "tier must be 'small' or 'medium'."
  }
}

variable "proxmox_node_name" {
  description = "Proxmox VE node the DB LXC is created on. Supplied per-cluster at apply time."
  type        = string
}

variable "svc01_vmid" {
  description = "Proxmox VMID for the DB LXC. Supplied explicitly so instances get stable, non-colliding IDs."
  type        = number
}

variable "svc01_template_file_id" {
  description = "Proxmox volume ID of the STOCK LXC template (Docker + Compose installed per host by the `common` role, ADR-0003). Form `<datastore>:vztmpl/<file>`."
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.svc01_template_file_id))
    error_message = "svc01_template_file_id must be '<datastore>:vztmpl/<file_name>'."
  }
}

variable "svc01_template_os_type" {
  description = "operating_system.type for the template — `debian` (default) or `ubuntu`."
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.svc01_template_os_type)
    error_message = "svc01_template_os_type must be 'debian' or 'ubuntu'."
  }
}

variable "svc01_datastore_id" {
  description = "Proxmox storage pool for the LXC rootfs + DB data (default local-zfs per PF §6/§7)."
  type        = string
  default     = "local-zfs"
}

variable "operator_ssh_public_keys" {
  description = "Operator SSH PUBLIC key(s) injected into the DB LXC root account so Ansible can reach it. PUBLIC keys only; sourced from TF_VAR_operator_ssh_public_keys / gitignored .env, never committed. Default [] = SSH-unreachable until supplied."
  type        = list(string)
  default     = []
}
