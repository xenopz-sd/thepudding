variable "proxmox_node_name" {
  description = "Name of the Proxmox VE node the DNS LXC (svc10-20-01) is created on. Supplied per-cluster at apply time."
  type        = string
}

variable "svc10_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template (Debian 12 / Ubuntu 24.04) sourced via
    `pveam`, form `<datastore>:vztmpl/<file_name>`. Docker + Compose are installed per host
    by the `common` role (ADR-0003), not pre-baked.
  EOT
  type        = string
  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.svc10_template_file_id))
    error_message = "svc10_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>'."
  }
}

variable "svc10_template_os_type" {
  description = "operating_system.type of the template above: debian or ubuntu."
  type        = string
  default     = "debian"
  validation {
    condition     = contains(["debian", "ubuntu"], var.svc10_template_os_type)
    error_message = "svc10_template_os_type must be 'debian' or 'ubuntu' (tech.md sanctioned LXC template families)."
  }
}

variable "svc10_datastore_id" {
  description = "Proxmox storage pool for the LXC root filesystem (PF §6/§7 standard: local-zfs)."
  type        = string
  default     = "local-zfs"
}

variable "svc10_vmid" {
  description = <<-EOT
    OPERATOR OVERRIDE for the DNS LXC's Proxmox VMID (ADR-0022 grandfather posture).
    0 (default) uses the module-computed deterministic VMID (vlan_id * 1000 + host_index =
    20 * 1000 + 15 = 20015); non-zero wins and is used verbatim.
  EOT
  type        = number
  default     = 0
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into the DNS LXC's root account so Ansible can
    reach it after `terraform apply`. PUBLIC keys only; sourced from TF_VAR_operator_ssh_public_keys,
    never committed. Default [] is a strict no-op (the LXC is simply SSH-unreachable until supplied).
  EOT
  type        = list(string)
  default     = []
}
