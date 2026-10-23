# SVC-06 Identity Provider (ZITADEL) Terraform root — guest-provisioning inputs.
#
# These inputs supply the parts of the ZITADEL LXC that the shared
# `proxmox-compute` module deliberately does NOT carry (that module owns only the
# computed addressing contract: vlan_id + host_index -> computed static IPv4,
# hostname, tags). There is deliberately NO IP-literal input — the ZITADEL guest
# address is COMPUTED by the module via cidrhost("10.0.20.0/24", 10 + host_index)
# (NET-00 §3). The BACKING DB is NOT provisioned here (separate SVC-01 instance,
# design.md §3.3), so no db_* inputs appear in this root.

variable "proxmox_node_name" {
  description = "Name of the Proxmox VE node the ZITADEL LXC is created on. Supplied per-cluster at apply time, not hardcoded."
  type        = string
}

variable "zitadel_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template for the ZITADEL LXC — a Debian 12
    (or Ubuntu 24.04 LTS) standard template via `pveam` (tech.md "Base
    templates"). Docker + Compose are NOT pre-baked; the `common` role installs
    them per host at provision time (ADR-0003). Form
    `<datastore>:vztmpl/<file_name>`, e.g.
    `local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst`.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.zitadel_template_file_id))
    error_message = "zitadel_template_file_id must be a Proxmox LXC template volume ID of the form '<datastore>:vztmpl/<file_name>'."
  }
}

variable "zitadel_template_os_type" {
  description = "operating_system.type for the LXC template above — `debian` (Debian 12) or `ubuntu` (Ubuntu 24.04 LTS)."
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.zitadel_template_os_type)
    error_message = "zitadel_template_os_type must be 'debian' or 'ubuntu' (the two sanctioned LXC template families, tech.md)."
  }
}

variable "zitadel_datastore_id" {
  description = "Proxmox storage pool for the ZITADEL LXC root filesystem. Platform standard is `local-zfs` (PF §6/§7)."
  type        = string
  default     = "local-zfs"
}

variable "zitadel_vmid" {
  description = <<-EOT
    Proxmox VMID for the ZITADEL LXC (svc06-20-01). Supplied explicitly so the
    container gets a stable, non-colliding ID from the operator/allocation scheme
    rather than auto-assigned. MUST differ from the backing DB's VMID
    (svc06_db_vmid in the installer) and every other VLAN-20 guest.
  EOT
  type        = number
}

variable "zitadel_host_index" {
  description = <<-EOT
    Zero-based host position within VLAN 20 for the ZITADEL guest. The module
    computes the static IPv4 as cidrhost("10.0.20.0/24", 10 + host_index). MUST
    differ from the backing DB's host_index (svc06_db_host_index=4 in the
    installer) so the two guests do not collide. Default 5 -> 10.0.20.15.
  EOT
  type        = number
  default     = 5

  validation {
    condition     = var.zitadel_host_index >= 0
    error_message = "zitadel_host_index is a zero-based position and must be >= 0 (NET-00 §3)."
  }
}

variable "zitadel_version" {
  description = <<-EOT
    ZITADEL image tag echo (e.g. `v4.5.6`), forward-compatibility surface for an
    operator/CI to record which pinned image this apply corresponds to. The
    actual image pin that the container runs is set in the `zitadel-server`
    Ansible role (deployment is a Compose stack, not a Terraform-managed image);
    this is a documentation/output echo only. Never `latest` (tech.md).
  EOT
  type        = string
  default     = "v4"
}

variable "platform_domain" {
  description = <<-EOT
    The platform domain ZITADEL's Traefik ingress serves it under; the OIDC
    issuer is `https://auth.<platform_domain>` (zitadel_issuer_url output). A
    non-secret placeholder supplied per environment (e.g. platform.example.test
    in dev). The actual Traefik Host rule is applied by the zitadel-server role's
    labels and MUST match this exactly (requirements §6/§12 — a domain mismatch
    breaks redirect URIs and token issuance).
  EOT
  type        = string
  default     = "platform.example.test"
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into the ZITADEL LXC's root account so
    Ansible can reach it over SSH after apply. PUBLIC keys only — never a private
    key/password/secret. Sourced from TF_VAR_operator_ssh_public_keys (a
    gitignored .env), NEVER committed. Default [] is a strict no-op: the LXC
    provisions exactly as before and is simply SSH-unreachable until keys are
    supplied.
  EOT
  type        = list(string)
  default     = []
}
