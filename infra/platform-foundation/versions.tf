# Platform-Foundation Terraform root — version and backend pinning.
#
# This root owns the cluster-wide, non-per-project network fabric:
# the single Proxmox SDN VLAN zone and its applier (declared in later tasks).
# It is deliberately separate from every per-project root under
# infra/projects/<slug>/ so that one project's `terraform destroy` can never
# touch shared or other-project resources (NET-00 §6, PF §4).
#
# Provider: bpg/proxmox ONLY, explicitly version-pinned (tech.md, catalog
# decision — no second Proxmox provider, no Kubernetes).

terraform {
  # Pin the CLI to a modern line that supports the SDN resources and
  # `-backend-config` partial backends used below.
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Current stable release on the Terraform Registry at authoring time.
      # `~>` keeps us on the 0.111.x patch line; bump deliberately after
      # re-checking the registry for a newer stable release (per tech.md).
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, HTTP backend (PF decision F1, FR-2).
  #
  # State name for this root is `platform-foundation` — distinct from every
  # per-project `<slug>-infra` state name (Requirement 6.3). The concrete
  # address, credentials, and lock/unlock endpoints are supplied at
  # `terraform init` time via `-backend-config` (partial backend), because a
  # `backend` block may not contain interpolations. In GitLab CI these are
  # derived from CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN so no PAT is
  # needed for same-project state (PF §7).
  #
  # Example init (see the root README / runbook, not committed here):
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/platform-foundation" \
  #     -backend-config="lock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/platform-foundation/lock" \
  #     -backend-config="unlock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/platform-foundation/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" \
  #     -backend-config="unlock_method=DELETE" \
  #     -backend-config="retry_wait_min=5"
  backend "http" {
    # Intentionally empty: this is a partial backend. All attributes are
    # provided via -backend-config at init time (see above). The state name
    # `platform-foundation` is enforced by the address passed at init.
  }
}
