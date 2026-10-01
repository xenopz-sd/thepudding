# SVC-07 Secrets Manager (OpenBao) Terraform root — guest-provisioning inputs.
#
# These inputs supply the parts of an LXC guest that the shared `proxmox-compute`
# module deliberately does NOT carry. That module (consumed here UNMODIFIED)
# owns only the computed *addressing* contract — it turns `vlan_id` + `host_index`
# into a computed static IPv4, a machine-facing hostname, and the Proxmox `tags`
# value, and by design declares no `node_name`, template, datastore, cpu, memory,
# or IP-literal input (see infra/modules/proxmox-compute/locals.tf "MODULE SCOPE"
# and variables.tf's closing note). The guest resource that consumes those
# computed values therefore lives in THIS root (main.tf), and the placement /
# sizing / template inputs it needs are declared here.
#
# There is deliberately NO IP-literal input: the two OpenBao LXCs' addresses
# (10.0.20.10 primary, 10.0.20.11 unsealer) are COMPUTED by the module via
# `cidrhost("10.0.20.0/24", 10 + host_index)`, never chosen by an operator
# (Requirement 9.2, 9.3; NET-00 §3). This root only supplies host_index (0 and 1).

variable "proxmox_node_name" {
  description = <<-EOT
    Name of the Proxmox VE node the two OpenBao LXCs are created on. Both the
    primary (svc07-20-01) and the Transit unsealer (svc07-unsealer-20-01) are
    created on this node; anti-affinity across multiple physical hosts is out of
    scope at this cluster scale (design.md "Single-node Raft now, 3-node HA
    deferred"). Supplied per-cluster at apply time, not hardcoded.
  EOT
  type        = string
}

variable "openbao_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template used for BOTH OpenBao LXCs — a
    Debian 12 (or Ubuntu 24.04 LTS) standard template sourced via `pveam`
    (Requirement 9.4, 9.5; tech.md "Base templates"). Docker + the Compose plugin
    are NOT pre-baked into the template; the `common` Ansible role installs them
    per host at provision time (ADR-0003, superseding ADR-0002 — the earlier
    pre-baked `debian-12-docker` template was abandoned). Form
    `<datastore>:<content_type>/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`. The primary and
    unsealer share the same template family (Requirement 9.5).
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.openbao_template_file_id))
    error_message = "openbao_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>' (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."
  }
}

variable "openbao_template_os_type" {
  description = <<-EOT
    The operating_system.type reported for the LXC template above — `debian` for
    a Debian 12 template or `ubuntu` for an Ubuntu 24.04 LTS template
    (Requirement 9.4/9.5 template family). Kept as an explicit input so the
    resource's declared OS type tracks whichever template family the cluster
    actually stocks, rather than being hardcoded.
  EOT
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.openbao_template_os_type)
    error_message = "openbao_template_os_type must be 'debian' (Debian 12) or 'ubuntu' (Ubuntu 24.04 LTS) — the two sanctioned LXC template families (tech.md)."
  }
}

variable "openbao_datastore_id" {
  description = <<-EOT
    Proxmox storage pool for the LXC root filesystems. Per PF §6/§7 the platform
    standard is `local-zfs`. The primary gets a 20 GB rootfs and the unsealer an
    8 GB rootfs on this datastore (Requirement 9.2, 9.3).
  EOT
  type        = string
  default     = "local-zfs"
}

variable "openbao_primary_vmid" {
  description = <<-EOT
    Proxmox VMID for the primary OpenBao LXC (svc07-20-01). Supplied explicitly
    so the two SVC-07 containers get stable, non-colliding IDs assigned by the
    operator/allocation scheme rather than auto-assigned. The unsealer's VMID is
    a separate input (openbao_unsealer_vmid).
  EOT
  type        = number
}

variable "openbao_unsealer_vmid" {
  description = <<-EOT
    Proxmox VMID for the Transit unsealer OpenBao LXC (svc07-unsealer-20-01).
    Supplied explicitly so the two SVC-07 containers get stable, non-colliding
    IDs assigned by the operator/allocation scheme rather than auto-assigned.
    Must differ from openbao_primary_vmid.
  EOT
  type        = number
}
variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into BOTH OpenBao LXCs' root accounts so
    Ansible can reach them over SSH after `terraform apply`. PUBLIC keys only —
    never a private key/password/secret. Sourced from a Terraform variable /
    gitignored `.env` (TF_VAR_operator_ssh_public_keys), NEVER committed as a
    literal (Req 1.6). Default [] is a strict no-op: the LXCs provision exactly as
    before and are simply SSH-unreachable until keys are supplied (Req 1.3, FD.3).
  EOT
  type        = list(string)
  default     = []
}