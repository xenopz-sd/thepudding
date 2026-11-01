# infra/projects/<slug>/workload/versions.tf  —  Workload_Consumer_Root version
# + backend (consumer-last-mile-hello-workload §B, task 2.2).
#
# ===========================================================================
# State-name isolation (Req 2.5)
# ===========================================================================
# This per-project workload root gets its OWN GitLab-managed Terraform state name
# `<slug>-workload` — DISTINCT from:
#   * the project's network-slice state `<slug>-infra` (the infra/projects/<slug>/
#     top-level root), and
#   * the project's DB state `<slug>-db` (the db/ subroot), and
#   * every shared-service state (platform-foundation, svc-07-secrets-manager,
#     svc-09-reverse-proxy, …).
# One state per service is what makes `terraform destroy` of the workload safe —
# it can never touch the network slice or the stateful DB, and vice versa
# (integration-boundaries §5, PF FR-2). The stateless workload guest is torn down
# with the project by destroying THIS state (Req 2.5).
#
# The state name is NOT literal in the backend block (a `backend` block may not
# contain interpolations). Because this `workload/` directory is COPIED per
# project, the concrete `<slug>-workload` state name is supplied at
# `terraform init` via `-backend-config=address=.../terraform/state/<slug>-workload`.
# In GitLab CI the address/credentials derive from CI_API_V4_URL / CI_PROJECT_ID /
# CI_JOB_TOKEN. The gitignored local_backend_override.tf keeps per-instance state
# isolated via a distinct local state file for a solo/offline apply.
#
# Example init (documented, not committed — substitute the real slug):
#   terraform init \
#     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-workload" \
#     -backend-config="lock_address=.../<slug>-workload/lock" \
#     -backend-config="unlock_address=.../<slug>-workload/lock" \
#     -backend-config="username=gitlab-ci-token" \
#     -backend-config="password=${CI_JOB_TOKEN}" \
#     -backend-config="lock_method=POST" -backend-config="unlock_method=DELETE"
#
# Provider: bpg/proxmox ONLY, pinned to the same `~> 0.111.1` line as the db/
# root + proxmox-compute (tech.md, catalog F3).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, partial HTTP backend (PF decision F1, FR-2).
  # The per-instance state name `<slug>-workload` is enforced by the address
  # passed at init (see header), keeping the workload state isolated from
  # `<slug>-infra`, `<slug>-db`, and every service state.
  backend "http" {
    # Intentionally empty: partial backend. State name `<slug>-workload` supplied
    # via -backend-config at init.
  }
}
