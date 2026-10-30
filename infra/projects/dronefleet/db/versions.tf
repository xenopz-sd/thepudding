# infra/projects/<slug>/db/versions.tf  —  SVC01_Consumer_Root version + backend.
#
# ===========================================================================
# State-name isolation (Task 6.2, Req 3.5)
# ===========================================================================
# This per-project DB root gets its OWN GitLab-managed Terraform state name
# `<slug>-db` — DISTINCT from:
#   * the project's network-slice state `<slug>-infra` (the infra/projects/<slug>/
#     _TEMPLATE root), and
#   * every shared-service state (platform-foundation, svc-07-secrets-manager,
#     svc-09-reverse-proxy, …) and the reusable SVC-01 root's own
#     `<slug>-svc01-postgres` state.
# One state per service is what makes `terraform destroy` of the consumer DB
# safe — it can never touch the network slice or another service
# (integration-boundaries §5, PF FR-2, ADR-0019 alt C rejected).
#
# The state name is NOT literal in the backend block (a `backend` block may not
# contain interpolations). Because this `db/` directory is COPIED per project,
# the concrete `<slug>-db` state name is supplied at `terraform init` via
# `-backend-config=address=.../terraform/state/<slug>-db`. In GitLab CI the
# address/credentials derive from CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN.
# The gitignored local_backend_override.tf keeps per-instance state isolated via
# a distinct local state file for a solo/offline apply.
#
# Example init (documented, not committed — substitute the real slug):
#   terraform init \
#     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-db" \
#     -backend-config="lock_address=.../<slug>-db/lock" \
#     -backend-config="unlock_address=.../<slug>-db/lock" \
#     -backend-config="username=gitlab-ci-token" \
#     -backend-config="password=${CI_JOB_TOKEN}" \
#     -backend-config="lock_method=POST" -backend-config="unlock_method=DELETE"
#
# Provider: bpg/proxmox ONLY, pinned to the same `~> 0.111.1` line as the reused
# SVC-01 root + proxmox-compute (tech.md, catalog F3).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, partial HTTP backend (PF decision F1, FR-2).
  # The per-instance state name `<slug>-db` is enforced by the address passed at
  # init (see header), keeping the consumer DB state isolated from `<slug>-infra`
  # and every service state (Req 3.5).
  backend "http" {
    # Intentionally empty: partial backend. State name `<slug>-db` supplied via
    # -backend-config at init.
  }
}
