# SVC-03 Object Storage (Garage) Terraform root — version and backend pinning.
#
# A SHARED PLATFORM SERVICE (VLAN 20). Its own Terraform root whose GitLab-managed
# state name is the directory name, `svc-03-object-storage` (supplied by the
# `-backend-config=address=...` passed at init; this partial block stays empty).
# The root declares THREE `proxmox_virtual_environment_container` resources (the
# Garage nodes) via the shared `proxmox-compute` module.
# Spec: .kiro/specs/svc-03-object-storage.
terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # Partial backend config: address/credentials are injected at `terraform init`
  # (GitLab-managed HTTP state in CI, or local_backend_override.tf for a homelab
  # run). Never hardcoded here.
  backend "http" {
  }
}
