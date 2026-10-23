# SVC-01 SQL Database (PostgreSQL / MariaDB) Terraform root — version + backend.
#
# SVC-01 is a DEDICATED-PER-PROJECT service (domain-rules §3), NOT a shared
# VLAN-20 instance. This root is REUSABLE: the same code provisions a dedicated
# database LXC for any project, parameterized by `project_slug` + `vlan_id`
# (variables.tf). Each project INSTANCE gets its OWN GitLab-managed state name
# `<slug>-svc01-postgres`, so a `terraform destroy` for one project's DB can
# never touch another's (integration-boundaries §5, domain-rules §2).
#
# The state name is NOT fixed in the backend block (a `backend` block may not
# contain interpolations and must be literal). Because this one root serves many
# project instances, the concrete per-instance state name is supplied at
# `terraform init` via `-backend-config=address=.../terraform/state/<slug>-svc01-postgres`
# — the installer (svc01_provision) derives `<slug>` from the project_slug. The
# gitignored local_backend_override.tf path keeps per-instance state isolated via
# distinct working copies / state files for a solo/offline apply.
#
# Provider: bpg/proxmox ONLY, pinned to the same `~> 0.111.1` line as every
# sibling root + the shared proxmox-compute module (tech.md, catalog F3).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, partial HTTP backend (PF decision F1, FR-2).
  # Per-instance state name `<slug>-svc01-postgres` is supplied at init via
  # -backend-config (see the header note); it is NOT literal here because one
  # reusable root serves many project instances, each needing its own isolated
  # state. Example init (documented in the root README, not committed):
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/<slug>-svc01-postgres" \
  #     -backend-config="lock_address=.../<slug>-svc01-postgres/lock" \
  #     -backend-config="unlock_address=.../<slug>-svc01-postgres/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" -backend-config="unlock_method=DELETE"
  backend "http" {
    # Intentionally empty: partial backend. The per-instance state name
    # <slug>-svc01-postgres is enforced by the address passed at init, keeping
    # each project's DB state isolated across all roots (integration-boundaries §5).
  }
}
