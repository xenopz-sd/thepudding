# SVC-17/18 Observability (Prometheus + Grafana + Loki) Terraform root —
# Proxmox provider wiring.
#
# Mirrors infra/projects/svc-09-reverse-proxy/providers.tf. Authentication is
# API-token based, per PF FR-1: Terraform authenticates as the dedicated service
# account (`terraform@pve`) using an API token, NOT the root password.
#
# This root attaches each observability LXC's NIC to the shared VLAN-20 SDN VNet
# (main.tf), so at apply time the `terraform@pve` custom PVE role must already
# carry `SDN.Allocate` (create/manage, ADR-0001) + `SDN.Use` (guest-NIC attach
# to the existing VNet `p20`, ADR-0009). Those grants are owned by the Platform
# Foundation layer and are a documented precondition here — NEITHER declared,
# modified, NOR re-granted by this root. This root only *consumes* the identity;
# if a privilege is absent the apply fails with an authorization error and no
# container is created.

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
