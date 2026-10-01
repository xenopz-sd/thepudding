# Per-project Terraform root — version and backend pinning.
#
# This is a TEMPLATE root. Copy the whole `infra/projects/_TEMPLATE/` directory
# to `infra/projects/<slug>/` for a real project, where <slug> matches an entry
# in the platform-foundation registry (infra/platform-foundation/projects.yaml).
# Nothing in this template hardcodes the slug — the slug is supplied as the
# `project_slug` input variable (see variables.tf), and the state name is derived
# from it at `terraform init` time via -backend-config (see below).
#
# This root owns exactly one project's VNet + subnet + its own applier (task 4.2)
# and reads that project's vlan_id from the shared registry (main.tf). It is
# deliberately separate — with its own GitLab-managed state name `<slug>-infra` —
# from the platform-foundation root and from every other project root, so that one
# project's `terraform destroy` can never touch shared or other-project resources
# (NET-00 §6, PF §4, Requirement 6.3).
#
# Provider: bpg/proxmox ONLY, explicitly version-pinned (tech.md, catalog
# decision — no second Proxmox provider, no Kubernetes). Mirrors the pin in
# infra/platform-foundation/versions.tf so both roots move together.

terraform {
  # Pin the CLI to a modern line that supports the SDN resources and
  # `-backend-config` partial backends used below.
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Same pin as the platform-foundation root: keep on the 0.111.x patch
      # line; bump deliberately after re-checking the registry (per tech.md).
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, HTTP backend (PF decision F1, FR-2).
  #
  # State name for this root is `<slug>-infra` — unique per project and distinct
  # from the platform-foundation root's `platform-foundation` state name and from
  # every other project's `<slug>-infra` name (Requirement 6.3). The concrete
  # address, credentials, and lock/unlock endpoints are supplied at
  # `terraform init` time via `-backend-config` (partial backend), because a
  # `backend` block may not contain interpolations — so the slug cannot be
  # baked in here and MUST be passed at init. In GitLab CI these are derived from
  # CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN so no PAT is needed for
  # same-project state (PF §7).
  #
  # Example init (see the root README / runbook, not committed here). Substitute
  # the real slug for <slug>; the `-infra` suffix is what makes the state name
  # unique per project and distinct from `platform-foundation`:
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-infra" \
  #     -backend-config="lock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-infra/lock" \
  #     -backend-config="unlock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-infra/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" \
  #     -backend-config="unlock_method=DELETE" \
  #     -backend-config="retry_wait_min=5"
  backend "http" {
    # Intentionally empty: this is a partial backend. All attributes are
    # provided via -backend-config at init time (see above). The state name
    # `<slug>-infra` is enforced by the address passed at init, keeping it
    # unique per project (Requirement 6.3).
  }
}
