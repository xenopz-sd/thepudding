# SVC-07 Secrets Manager (OpenBao) Terraform root — Proxmox provider wiring.
#
# Mirrors infra/platform-foundation/providers.tf and
# infra/projects/_TEMPLATE/providers.tf. Authentication is API-token based, per
# PF FR-1: Terraform authenticates as the dedicated service account
# (`terraform@pve`) using an API token, NOT the root password.
#
# This root attaches the two OpenBao LXCs' NICs to the shared VLAN-20 SDN VNet
# (tasks 2.1/2.2), so at apply time the `terraform@pve` custom PVE role must
# already carry the `SDN.Allocate` privilege on `/sdn`. That grant is owned by
# the Platform Foundation layer (PF FR-1, ADR-0001) and is a documented
# precondition here — it is NEITHER declared, modified, NOR re-granted by this
# root (Requirement 9.8; design.md "Deployment/Configuration Impact — SDN
# precondition"). This root only *consumes* the identity; if the privilege is
# absent the apply fails with an authorization error and no container is created
# (Requirement 9.8).

variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint, e.g. https://pve.example.internal:8006/. No trailing path beyond the host/port."
  type        = string
}

variable "proxmox_api_token" {
  description = <<-EOT
    Proxmox API token for the `terraform@pve` service account, in the
    bpg/proxmox form `USER@REALM!TOKENID=UUID`
    (e.g. `terraform@pve!ci=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`).
    Supplied at runtime from a GitLab CI/CD protected + masked variable
    (PF FR-8, §11) — never committed. Marked sensitive so it is not echoed
    in plan/apply output or logs.
  EOT
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification of the Proxmox API certificate. Keep false in production; set true only for a lab endpoint using a self-signed cert."
  type        = bool
  default     = false
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure

  # The two OpenBao LXC container resources (tasks 2.1/2.2) may require an SSH
  # connection for file-provisioning of container-level settings depending on
  # the bpg/proxmox operations used; if so, an ssh {} block is added by that
  # later task. It is intentionally omitted here so task 1.1 introduces no
  # setting a subsequent task has not yet justified.
}
