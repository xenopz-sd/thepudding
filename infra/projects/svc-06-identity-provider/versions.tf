# SVC-06 Identity Provider (ZITADEL) Terraform root — version and backend pinning.
#
# SVC-06 is a SHARED PLATFORM SERVICE (one ZITADEL instance for the whole
# platform; per-project isolation is logical via ZITADEL organizations, NOT a
# per-project VLAN — requirements §4/§11). Per structure.md, a shared platform
# service gets its own Terraform root with a GitLab-managed state name equal to
# the service slug — here `svc-06-identity-provider`. This root declares exactly
# ONE `proxmox_virtual_environment_container` (the ZITADEL LXC) via the shared
# `proxmox-compute` module.
#
# The ZITADEL BACKING DATABASE is deliberately NOT declared here. It is a
# separate SVC-01 instance (`project_slug=shared-zitadel`) with its OWN isolated
# state `shared-zitadel-svc01-postgres`, provisioned by the installer's
# svc06_provision role as a CHILD svc-01-bootstrap run (design.md §3.3). Folding
# the DB into this root would collapse SVC-01's per-instance state isolation
# (integration-boundaries §5), so this root owns only the ZITADEL guest.
#
# Provider: bpg/proxmox ONLY, pinned `~> 0.111.1` (same line as every sibling
# root + the shared module; bump deliberately per tech.md).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, HTTP backend (PF decision F1, FR-2). State
  # name `svc-06-identity-provider` — unique across all roots and DISTINCT from
  # the backing DB's SVC-01 state `shared-zitadel-svc01-postgres` (two isolated
  # states). A `backend` block may not contain interpolations, so the concrete
  # address/credentials/lock endpoints are supplied at `terraform init` via
  # `-backend-config` (partial backend); the state name is enforced by the
  # address passed at init.
  #
  # Example init (documented in the root README, not committed here):
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-06-identity-provider" \
  #     -backend-config="lock_address=.../svc-06-identity-provider/lock" \
  #     -backend-config="unlock_address=.../svc-06-identity-provider/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" \
  #     -backend-config="unlock_method=DELETE" \
  #     -backend-config="retry_wait_min=5"
  backend "http" {
    # Intentionally empty: partial backend. All attributes via -backend-config
    # at init. The state name `svc-06-identity-provider` is enforced by the
    # address passed at init, keeping it unique across all roots.
  }
}
