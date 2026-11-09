# Shared dev mail catcher (Mailpit) Terraform root — version and backend pinning.
#
# A SHARED PLATFORM SERVICE (VLAN 20), not a per-project resource. Per
# structure.md it gets its own Terraform root whose GitLab-managed state name is
# the directory name, `mailcatcher` (the `-backend-config=address=...` passed at
# init supplies it; this partial block stays empty). The root declares exactly ONE
# `proxmox_virtual_environment_container` (the catcher LXC) via the shared
# `proxmox-compute` module. Spec: .kiro/specs/dev-mail-catcher, ADR-0025.
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
