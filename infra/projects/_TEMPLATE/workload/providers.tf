# infra/projects/<slug>/workload/providers.tf  —  Workload_Consumer_Root
# provider wiring.
#
# API-token auth as `terraform@pve` (PF FR-1), never the root password. This
# root attaches the workload LXC's NIC to the project's SDN VNet `p<vlan>`, so at
# apply time `terraform@pve` must carry `SDN.Allocate` (ADR-0001) + `SDN.Use`
# (ADR-0009) — owned by platform-foundation, a documented precondition, neither
# declared nor re-granted here. The project VNet itself is created by the
# project's own `infra/projects/<slug>/` network-slice root (sdn.tf); this
# `workload/` root only references it.

variable "proxmox_endpoint" {
  description = "Proxmox VE API endpoint, e.g. https://pve.example.internal:8006/."
  type        = string
}

variable "proxmox_api_token" {
  description = <<-EOT
    Proxmox API token for `terraform@pve`, form `USER@REALM!TOKENID=UUID`.
    Supplied at runtime from a GitLab CI/CD protected + masked variable (PF FR-8)
    — never committed. Sensitive so it is not echoed in plan/apply output.
  EOT
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification of the Proxmox API cert. Keep false in production; true only for a lab self-signed endpoint."
  type        = bool
  default     = false
}

provider "proxmox" {
  endpoint  = var.proxmox_endpoint
  api_token = var.proxmox_api_token
  insecure  = var.proxmox_insecure
}
