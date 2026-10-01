# SVC-07 Secrets Manager (OpenBao) Terraform root — version and backend pinning.
#
# SVC-07 is a SHARED PLATFORM SERVICE, not a per-project resource (design.md
# "Overview"). Per structure.md, a shared platform service gets its own
# Terraform root with a GitLab-managed state name equal to the service slug —
# here `svc-07-secrets-manager`. This root will declare exactly two
# `proxmox_virtual_environment_container` resources (the primary OpenBao LXC and
# the seal-only Transit unsealer LXC) via the shared `proxmox-compute` module in
# a later task (2.1/2.2); this file only establishes the provider pin, the
# Terraform version floor, and the state backend (task 1.1).
#
# It is deliberately separate — with its own state name `svc-07-secrets-manager`
# — from the platform-foundation root (`platform-foundation`), from every
# per-project root (`<slug>-infra`), and from every other service root, so that a
# `terraform destroy` of this root removes only the two OpenBao LXCs and can
# never touch the platform-foundation SDN objects or any other root's resources
# (Requirement 9.6, 9.7; NET-00 §6, PF §4).
#
# Provider: bpg/proxmox ONLY, explicitly version-pinned (tech.md, catalog
# decision F3 — no second Proxmox provider, no Kubernetes). Pinned to the same
# `~> 0.111.1` line as every sibling root and the shared proxmox-compute module
# (infra/platform-foundation/versions.tf, infra/projects/_TEMPLATE/versions.tf,
# infra/modules/proxmox-compute/versions.tf) so all roots and the module move
# together; bump deliberately after re-checking the registry (per tech.md).

terraform {
  # Pin the CLI to a modern line that supports the LXC/container resources and
  # `-backend-config` partial backends used below.
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Same pin as the platform-foundation root, the per-project template, and
      # the shared proxmox-compute module: keep on the 0.111.x patch line.
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, HTTP backend (PF decision F1, FR-2).
  #
  # State name for this root is `svc-07-secrets-manager` — unique across all
  # roots and distinct from `platform-foundation`, from every `<slug>-infra`
  # per-project state, and from every other service root's state name
  # (Requirement 9.6). Because a `backend` block may not contain interpolations,
  # the concrete address, credentials, and lock/unlock endpoints are supplied at
  # `terraform init` time via `-backend-config` (partial backend). The state
  # name `svc-07-secrets-manager` is fixed and MUST be used verbatim in the
  # address passed at init — see the root README/runbook (task 13.1). In GitLab
  # CI these are derived from CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN so no
  # PAT is needed for same-project state (PF §7).
  #
  # Example init (documented in the root README, not committed here):
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-07-secrets-manager" \
  #     -backend-config="lock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-07-secrets-manager/lock" \
  #     -backend-config="unlock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-07-secrets-manager/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" \
  #     -backend-config="unlock_method=DELETE" \
  #     -backend-config="retry_wait_min=5"
  backend "http" {
    # Intentionally empty: this is a partial backend. All attributes are
    # provided via -backend-config at init time (see above). The state name
    # `svc-07-secrets-manager` is enforced by the address passed at init,
    # keeping it unique across all roots (Requirement 9.6).
  }
}
