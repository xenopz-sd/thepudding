# SVC-17/18 Observability (Prometheus + Grafana + Loki) Terraform root —
# version and backend pinning.
#
# SVC-17/18 is a SHARED PLATFORM SERVICE, not a per-project resource (design.md
# "Overview"). Per structure.md, a shared platform service gets its own
# Terraform root with a GitLab-managed state name equal to the service slug —
# here `svc-17-18-observability`. This root declares THREE
# `proxmox_virtual_environment_container` resources (the Prometheus, Grafana, and
# Loki LXCs) via three blocks over the shared `proxmox-compute` module (main.tf);
# this file only establishes the provider pin, the Terraform version floor, and
# the state backend.
#
# It is deliberately separate — with its own state name `svc-17-18-observability` —
# from the platform-foundation root (`platform-foundation`), from every
# per-project root (`<slug>-infra`), and from every other service root
# (`svc-07-secrets-manager`, `svc-09-reverse-proxy`, …), so that a
# `terraform destroy` of this root removes only the three observability LXCs and
# can never touch the platform-foundation SDN objects or any other root's
# resources (NET-00 §6, PF §4; integration-boundaries §5).
#
# Provider: bpg/proxmox ONLY, explicitly version-pinned (tech.md, catalog
# decision F3 — no second Proxmox provider, no Kubernetes). Pinned to the same
# `~> 0.111.1` line as every sibling root and the shared proxmox-compute module
# so all roots and the module move together; bump deliberately after re-checking
# the registry (per tech.md).

terraform {
  required_version = ">= 1.6.0, < 2.0.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111.1"
    }
  }

  # GitLab-managed Terraform state, HTTP backend (PF decision F1, FR-2).
  #
  # State name for this root is `svc-17-18-observability` — unique across all
  # roots and distinct from `platform-foundation`, every `<slug>-infra`, and
  # every other service root's state name. Because a `backend` block may not
  # contain interpolations, the concrete address/credentials/lock endpoints are
  # supplied at `terraform init` via `-backend-config` (partial backend). In
  # GitLab CI these derive from CI_API_V4_URL / CI_PROJECT_ID / CI_JOB_TOKEN
  # (PF §7).
  #
  # Example init (documented in the root README, not committed here):
  #   terraform init \
  #     -backend-config="address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-17-18-observability" \
  #     -backend-config="lock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-17-18-observability/lock" \
  #     -backend-config="unlock_address=${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/terraform/state/svc-17-18-observability/lock" \
  #     -backend-config="username=gitlab-ci-token" \
  #     -backend-config="password=${CI_JOB_TOKEN}" \
  #     -backend-config="lock_method=POST" \
  #     -backend-config="unlock_method=DELETE" \
  #     -backend-config="retry_wait_min=5"
  backend "http" {
    # Intentionally empty: partial backend. All attributes provided via
    # -backend-config at init. The state name `svc-17-18-observability` is
    # enforced by the address passed at init, keeping it unique across all roots.
  }
}
