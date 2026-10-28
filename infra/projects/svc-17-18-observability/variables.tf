# SVC-17/18 Observability (Prometheus + Grafana + Loki) Terraform root —
# guest-provisioning inputs.
#
# These inputs supply the parts of each LXC guest that the shared
# `proxmox-compute` module deliberately does NOT carry
# (node/template/datastore/vmid/sizing). That module (consumed here UNMODIFIED)
# owns only the computed addressing contract — it turns `vlan_id` + `host_index`
# into a computed static IPv4, a machine-facing hostname, and the Proxmox `tags`
# value, and declares no IP-literal input.
#
# There is deliberately NO IP-literal input: each guest's address
# (10.0.20.18 / .19 / .20) is COMPUTED by the module via
# `cidrhost("10.0.20.0/24", 10 + host_index)`, never chosen by an operator
# (NET-00 §3). The host_index values (prometheus=8, grafana=9, loki=10) and the
# per-guest peer set are sourced from the committed Host_Index_Authority ledger
# (infra/platform-foundation/host-indices.yaml, ADR-0015) in main.tf, not
# declared as operator variables here.

variable "proxmox_node_name" {
  description = <<-EOT
    Name of the Proxmox VE node the three observability LXCs
    (svc17-prometheus-20-01, svc17-grafana-20-01, svc18-loki-20-01) are created
    on. Supplied per-cluster at apply time, not hardcoded.
  EOT
  type        = string
}

# --- Prometheus guest (svc17-prometheus-20-01) template ----------------------

variable "prometheus_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template for the Prometheus guest — a
    Debian 12 (or Ubuntu 24.04 LTS) standard template sourced via `pveam`
    (tech.md "Base templates"). Docker + the Compose plugin are NOT pre-baked;
    the `common` Ansible role installs them per host at provision time
    (ADR-0003). Form `<datastore>:vztmpl/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.prometheus_template_file_id))
    error_message = "prometheus_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>' (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."
  }
}

variable "prometheus_template_os_type" {
  description = <<-EOT
    The operating_system.type reported for the Prometheus LXC template above —
    `debian` for a Debian 12 template or `ubuntu` for an Ubuntu 24.04 LTS
    template. Kept as an explicit input so the resource's declared OS type tracks
    whichever template family the cluster stocks, rather than being hardcoded.
  EOT
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.prometheus_template_os_type)
    error_message = "prometheus_template_os_type must be 'debian' (Debian 12) or 'ubuntu' (Ubuntu 24.04 LTS) — the two sanctioned LXC template families (tech.md)."
  }
}

variable "prometheus_datastore_id" {
  description = <<-EOT
    Proxmox storage pool for the Prometheus LXC root filesystem. Per PF §6/§7 the
    platform standard is `local-zfs`.
  EOT
  type        = string
  default     = "local-zfs"
}

variable "prometheus_vmid" {
  description = <<-EOT
    Proxmox VMID for the Prometheus LXC (svc17-prometheus-20-01). Supplied explicitly so
    the container gets a stable, non-colliding ID assigned by the
    operator/allocation scheme rather than auto-assigned.
  EOT
  type        = number
}

# --- Grafana guest (svc17-grafana-20-01) template ----------------------------

variable "grafana_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template for the Grafana guest — a Debian
    12 (or Ubuntu 24.04 LTS) standard template sourced via `pveam` (tech.md
    "Base templates"). Docker + the Compose plugin are NOT pre-baked; the
    `common` Ansible role installs them per host at provision time (ADR-0003).
    Form `<datastore>:vztmpl/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.grafana_template_file_id))
    error_message = "grafana_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>' (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."
  }
}

variable "grafana_template_os_type" {
  description = <<-EOT
    The operating_system.type reported for the Grafana LXC template above —
    `debian` for a Debian 12 template or `ubuntu` for an Ubuntu 24.04 LTS
    template. Kept as an explicit input so the resource's declared OS type tracks
    whichever template family the cluster stocks, rather than being hardcoded.
  EOT
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.grafana_template_os_type)
    error_message = "grafana_template_os_type must be 'debian' (Debian 12) or 'ubuntu' (Ubuntu 24.04 LTS) — the two sanctioned LXC template families (tech.md)."
  }
}

variable "grafana_datastore_id" {
  description = <<-EOT
    Proxmox storage pool for the Grafana LXC root filesystem. Per PF §6/§7 the
    platform standard is `local-zfs`.
  EOT
  type        = string
  default     = "local-zfs"
}

variable "grafana_vmid" {
  description = <<-EOT
    Proxmox VMID for the Grafana LXC (svc17-grafana-20-01). Supplied explicitly so the
    container gets a stable, non-colliding ID assigned by the operator/allocation
    scheme rather than auto-assigned.
  EOT
  type        = number
}

# --- Loki guest (svc18-loki-20-01) template ----------------------------------

variable "loki_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template for the Loki guest — a Debian 12
    (or Ubuntu 24.04 LTS) standard template sourced via `pveam` (tech.md "Base
    templates"). Docker + the Compose plugin are NOT pre-baked; the `common`
    Ansible role installs them per host at provision time (ADR-0003). Form
    `<datastore>:vztmpl/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.loki_template_file_id))
    error_message = "loki_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>' (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."
  }
}

variable "loki_template_os_type" {
  description = <<-EOT
    The operating_system.type reported for the Loki LXC template above — `debian`
    for a Debian 12 template or `ubuntu` for an Ubuntu 24.04 LTS template. Kept as
    an explicit input so the resource's declared OS type tracks whichever template
    family the cluster stocks, rather than being hardcoded.
  EOT
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.loki_template_os_type)
    error_message = "loki_template_os_type must be 'debian' (Debian 12) or 'ubuntu' (Ubuntu 24.04 LTS) — the two sanctioned LXC template families (tech.md)."
  }
}

variable "loki_datastore_id" {
  description = <<-EOT
    Proxmox storage pool for the Loki LXC root filesystem. Per PF §6/§7 the
    platform standard is `local-zfs`.
  EOT
  type        = string
  default     = "local-zfs"
}

variable "loki_vmid" {
  description = <<-EOT
    Proxmox VMID for the Loki LXC (svc18-loki-20-01). Supplied explicitly so the
    container gets a stable, non-colliding ID assigned by the operator/allocation
    scheme rather than auto-assigned.
  EOT
  type        = number
}

# --- Cross-guest inputs ------------------------------------------------------

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into each observability LXC's root
    account so Ansible can reach it over SSH after `terraform apply`. PUBLIC keys
    only — never a private key/password/secret. Sourced from a Terraform variable
    / gitignored `.env` (TF_VAR_operator_ssh_public_keys), NEVER committed as a
    literal. Default [] is a strict no-op: the LXCs provision exactly as before
    and are simply SSH-unreachable until keys are supplied.
  EOT
  type        = list(string)
  default     = []
}

variable "platform_domain" {
  description = <<-EOT
    Base platform domain used to build the Grafana Traefik Host rule
    (`grafana.<platform_domain>`, SVC-17/18 requirements FR-13). Non-secret. Does
    NOT affect the Terraform plan — carried here only so the single cluster.env
    drives both the Terraform and Ansible layers; consumed by the Ansible
    `observability_stack` role when it templates the Grafana Traefik labels.
  EOT
  type        = string
}
