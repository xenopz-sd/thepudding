# SVC-09 Reverse Proxy (Traefik) Terraform root — guest-provisioning inputs.
#
# These inputs supply the parts of an LXC guest that the shared `proxmox-compute`
# module deliberately does NOT carry (node/template/datastore/vmid/sizing). That
# module (consumed here UNMODIFIED) owns only the computed addressing contract —
# it turns `vlan_id` + `host_index` into a computed static IPv4, a machine-facing
# hostname, and the Proxmox `tags` value, and declares no IP-literal input.
#
# There is deliberately NO IP-literal input: the Traefik LXC's address
# (10.0.20.12) is COMPUTED by the module via `cidrhost("10.0.20.0/24", 10 +
# host_index)`, never chosen by an operator (NET-00 §3). The host_index itself
# (2 for Traefik, moved from the colliding 0) is sourced from the committed
# host-index ledger in main.tf, not declared as an operator variable here.

variable "proxmox_node_name" {
  description = <<-EOT
    Name of the Proxmox VE node the Traefik LXC (svc09-20-01) is created on.
    Supplied per-cluster at apply time, not hardcoded.
  EOT
  type        = string
}

variable "traefik_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template — a Debian 12 (or Ubuntu 24.04
    LTS) standard template sourced via `pveam` (tech.md "Base templates"). Docker
    + the Compose plugin are NOT pre-baked; the `common` Ansible role installs
    them per host at provision time (ADR-0003). Form
    `<datastore>:vztmpl/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.traefik_template_file_id))
    error_message = "traefik_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>' (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."
  }
}

variable "traefik_template_os_type" {
  description = <<-EOT
    The operating_system.type reported for the LXC template above — `debian` for
    a Debian 12 template or `ubuntu` for an Ubuntu 24.04 LTS template. Kept as an
    explicit input so the resource's declared OS type tracks whichever template
    family the cluster stocks, rather than being hardcoded.
  EOT
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.traefik_template_os_type)
    error_message = "traefik_template_os_type must be 'debian' (Debian 12) or 'ubuntu' (Ubuntu 24.04 LTS) — the two sanctioned LXC template families (tech.md)."
  }
}

variable "traefik_datastore_id" {
  description = <<-EOT
    Proxmox storage pool for the LXC root filesystem. Per PF §6/§7 the platform
    standard is `local-zfs`. The Traefik LXC gets a 4 GB rootfs on this datastore
    (SVC-09 requirements §7). The acme.json certificate store lives on this
    persistent rootfs, never tmpfs (SVC-09 requirements §7).
  EOT
  type        = string
  default     = "local-zfs"
}

variable "traefik_vmid" {
  description = <<-EOT
    Proxmox VMID for the Traefik LXC (svc09-20-01). Supplied explicitly so the
    SVC-09 container gets a stable, non-colliding ID assigned by the
    operator/allocation scheme rather than auto-assigned.
  EOT
  type        = number
}

variable "letsencrypt_email" {
  description = <<-EOT
    Contact e-mail for the Let's Encrypt ACME account used by the public cert
    resolver (SVC-09 requirements §8, FR-2). Non-secret. Consumed by the Ansible
    `traefik_install` role when it templates the `letsencrypt` certificatesResolver;
    it does NOT affect the Terraform plan and is carried here only so the single
    cluster.env drives both layers. Defaults to an empty string: when empty, the
    real ACME resolver is not usable and the self-signed TLS mock
    (`traefik_tls_self_signed`, default off) is the interim path (mock-stand-ins.md).
  EOT
  type        = string
  default     = ""
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into the Traefik LXC's root account so
    Ansible can reach it over SSH after `terraform apply`. PUBLIC keys only —
    never a private key/password/secret. Sourced from a Terraform variable /
    gitignored `.env` (TF_VAR_operator_ssh_public_keys), NEVER committed as a
    literal. Default [] is a strict no-op: the LXC provisions exactly as before
    and is simply SSH-unreachable until keys are supplied.
  EOT
  type        = list(string)
  default     = []
}
