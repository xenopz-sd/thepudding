variable "proxmox_node_name" {
  description = "Name of the Proxmox VE node the three Garage LXCs (svc03-20-01..03) are created on. Supplied per-cluster at apply time."
  type        = string
}

variable "garage_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template (Debian 12 / Ubuntu 24.04) sourced via
    `pveam`, form `<datastore>:vztmpl/<file_name>`. Docker + Compose are installed per host
    by the `common` role (ADR-0003), not pre-baked.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.garage_template_file_id))
    error_message = "garage_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>'."
  }
}

variable "garage_template_os_type" {
  description = "operating_system.type of the template above: debian or ubuntu."
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.garage_template_os_type)
    error_message = "garage_template_os_type must be 'debian' or 'ubuntu' (tech.md sanctioned LXC template families)."
  }
}

variable "garage_datastore_id" {
  description = "Proxmox storage pool for the LXC root filesystems AND the dedicated data volumes (PF §6/§7 standard: local-zfs; local-lvm where local-zfs is absent)."
  type        = string
  default     = "local-zfs"
}

variable "garage_data_disk_gb" {
  description = "Size in GB of each node's dedicated Garage data volume (mounted at /var/lib/garage, holds data + metadata). Requirements §6: disk, not compute, is the sizing driver."
  type        = number
  default     = 20

  validation {
    condition     = var.garage_data_disk_gb >= 4
    error_message = "garage_data_disk_gb must be at least 4."
  }
}

# OPERATOR OVERRIDES for the three VMIDs (ADR-0022 grandfather posture). 0 (default)
# uses the module-computed deterministic VMID (vlan_id * 1000 + host_index =
# 20012 / 20013 / 20014); non-zero wins and is used verbatim.
variable "garage_node1_vmid" {
  description = "VMID override for svc03-20-01 (0 = computed 20012)."
  type        = number
  default     = 0
}

variable "garage_node2_vmid" {
  description = "VMID override for svc03-20-02 (0 = computed 20013)."
  type        = number
  default     = 0
}

variable "garage_node3_vmid" {
  description = "VMID override for svc03-20-03 (0 = computed 20014)."
  type        = number
  default     = 0
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into each Garage LXC's root account so Ansible can
    reach it after `terraform apply`. PUBLIC keys only; sourced from TF_VAR_operator_ssh_public_keys,
    never committed. Default [] is a strict no-op (the LXCs are simply SSH-unreachable until supplied).
  EOT
  type        = list(string)
  default     = []
}
