# Per-project Terraform root — Proxmox provider wiring.
#
# Mirrors infra/platform-foundation/providers.tf. Authentication is API-token
# based, per PF FR-1: Terraform authenticates as the dedicated service account
# (`terraform@pve`) using an API token, NOT the root password. For the SDN work
# this project root performs (VNet + subnet + applier, task 4.2), the
# `terraform@pve` custom PVE role must additionally carry the `SDN.Allocate`
# privilege — that grant is owned by the Platform Foundation layer and is
# neither declared nor modified here (Requirement 7.3, NET-00 §6). This root
# only *consumes* the identity.

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

  # SSH is optional for the SDN resources this root manages (VNet/subnet/applier
  # are API-driven), so no ssh {} block is configured here. It can be added by a
  # later task only if a resource in this root requires it.
}
