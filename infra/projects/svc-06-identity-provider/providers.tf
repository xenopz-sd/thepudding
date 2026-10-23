# SVC-06 Identity Provider (ZITADEL) Terraform root — Proxmox provider wiring.
#
# Mirrors infra/projects/svc-07-secrets-manager/providers.tf. Authentication is
# API-token based (PF FR-1): Terraform authenticates as `terraform@pve` using an
# API token, never the root password.
#
# This root attaches the ZITADEL LXC's NIC to the shared VLAN-20 SDN VNet `p20`,
# so at apply time the `terraform@pve` custom PVE role must already carry
# `SDN.Allocate` (create/manage, ADR-0001) + `SDN.Use` (guest-NIC attach,
# ADR-0009) on `/sdn`. Those grants are owned by the Platform Foundation layer
# and are a documented PRECONDITION here — neither declared, modified, nor
# re-granted by this root. If absent, the apply fails with an authorization
# error and no container is created.

variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint, e.g. https://pve.example.internal:8006/. No trailing path beyond the host/port."
  type        = string
}

variable "proxmox_api_token" {
  description = <<-EOT
    Proxmox API token for the `terraform@pve` service account, in the
    bpg/proxmox form `USER@REALM!TOKENID=UUID`. Supplied at runtime from a
    GitLab CI/CD protected + masked variable (PF FR-8, §11) — never committed.
    Marked sensitive so it is not echoed in plan/apply output or logs.
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
}
