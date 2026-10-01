# Shared proxmox-compute module — version and provider pinning.
#
# This module is the single seam through which every VM/LXC in the platform
# is provisioned. It turns a project's assigned `vlan_id` (flowing in from the
# per-project root) into a computed static host IP and a computed hostname,
# and rejects any operator-supplied IP literal (Requirement 2.4, NET-00 §3–§6).
#
# Provider: bpg/proxmox ONLY, explicitly version-pinned to match the roots
# (mirrors infra/platform-foundation/versions.tf) — no second Proxmox
# provider, no Kubernetes (tech.md, catalog decision F3).
#
# NOTE: a module does not declare a `backend`; state lives in the calling
# root. Only the provider pin and required_version are mirrored here.

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Keep in lockstep with the roots' pin (~> 0.111.x patch line).
      # Bump deliberately after re-checking the registry (per tech.md).
      version = "~> 0.111.1"
    }
  }
}
