# SVC-13 — Self-Hosted CI Runners Requirements

## 1. Overview

SVC-13 is the self-hosted CI runner fleet — **GitLab Runner**, Docker executor — that executes every pipeline job defined across this catalog's requirements documents: Terraform plan/apply, Ansible playbook runs, Docker image builds, and ML training/inference jobs. Because the platform's pipelines are hosted on GitLab or GitHub per the user's own requirement, GitLab Runner is the default execution agent for GitLab-hosted pipelines; it also supports GitHub Actions workflows via its `docker` executor when registered against a self-hosted GitHub Actions runner setup, though the primary target is GitLab CI. It is Tier 1 / platform-foundation-adjacent: every other service's "CI/CD pipeline" section (§10 in each requirements doc) assumes this fleet exists and is reachable. GPU-tagged runners specifically serve the ML-video and ML-text project types (model training/fine-tuning jobs, batch inference smoke tests); non-GPU runners serve every project type including embedded/drone (cross-compilation, firmware packaging) and generic backend (application builds/tests).

## 2. Scope

**In scope:**
- A fleet of self-hosted GitLab Runner instances (Docker executor) registered against the platform's GitLab (self-hosted or gitlab.com, per F1's sovereignty note).
- At least one GPU-attached runner for ML training/inference CI jobs (Terraform plan/apply for GPU VM provisioning, model smoke tests, lightweight fine-tuning validation).
- Automated runner registration via Terraform/Ansible using runner authentication tokens — no manual token copy-paste into `config.toml`.
- Runner tagging convention so pipelines can target the right runner class (`docker`, `gpu`, `terraform`).

**Out of scope:**
- The GitLab server itself (self-hosted GitLab CE/EE vs. gitlab.com SaaS) — that is a platform-foundation decision (F1), not this document.
- Kubernetes executor (explicitly excluded — F3 mandates Docker Compose only, no Kubernetes/k3s layer; the Docker executor, not the Kubernetes executor, is used throughout).
- GitHub Actions self-hosted runner binary configuration — if GitHub is chosen as the pipeline host for a given project instead of GitLab, that project's requirements doc must specify the GitHub Actions runner setup separately; this document covers the GitLab Runner default.

## 3. Technology selection

**Chosen:** GitLab Runner, Docker executor — the default self-hosted CI runner, matching the GitLab/GitHub-hosted pipeline requirement. Docker image: [`gitlab/gitlab-runner`](https://hub.docker.com/r/gitlab/gitlab-runner) (Ubuntu-based, ~470 MB, or the `alpine` variant at ~270 MB) — see [GitLab's own container installation docs](https://docs.gitlab.com/runner/install/docker/). License: MIT (the runner binary itself, regardless of GitLab Inc.'s own product licensing tiers for the server).

**Origin flag:** GitLab Inc. is headquartered in San Francisco, California, **US**, and is Nasdaq-listed ([GitLab Inc. — Wikipedia](https://en.wikipedia.org/wiki/GitLab)), so this is flagged per the catalog's European-sourcing principle — **though notably, GitLab was founded by Dmitriy Zaporozhets (Ukraine) and Sytse "Sid" Sijbrandij (Netherlands)**, giving it a genuinely European founding lineage even though current corporate HQ and stock listing are American. The Runner binary itself is MIT-licensed, fully open source, and runs entirely under this platform's own control with no phone-home dependency on GitLab Inc.'s infrastructure beyond the API calls needed to fetch/report jobs — so the *sovereignty* exposure is limited to "GitLab.com SaaS, if used as the server, is US-hosted," not to the runner fleet itself, which is 100% self-hosted here regardless.

**Fully-sovereign alternative (documented, not deployed by default):** **Forgejo + Forgejo Actions** — self-hosted forge, a drop-in alternative to GitLab if full independence from GitLab Inc./GitHub-Microsoft is later desired. Origin: **Codeberg e.V., Berlin, Germany**, a nonprofit ([Forgejo](https://forgejo.org/), [What is Codeberg?](https://docs.codeberg.org/getting-started/what-is-codeberg/)). Forgejo Actions is API-compatible with a meaningful subset of GitHub Actions workflow syntax and uses its own runner binary (`forgejo-runner`, also Docker-executor-based), so migrating job definitions is more tractable than migrating away from GitLab's `.gitlab-ci.yml` pipeline syntax entirely. This alternative is **not** the default today because it would mean abandoning the already-chosen GitLab/GitHub pipeline hosting decision — it is recorded here strictly as the documented off-ramp per the catalog's instruction, not something this deployment builds now.

## 4. Multi-tenancy model

**Pattern:** Shared platform instance (per catalog §1) — one runner fleet serves all projects; there is no per-project dedicated runner (though a project with unusual isolation needs, e.g. handling especially sensitive credentials, may register a project-scoped runner as an exception).

**Isolation implementation:**
- Runners are registered at the **group** level in GitLab (not instance-wide), so a compromised or misconfigured pipeline in one project cannot pick up jobs from an unrelated project's group.
- Docker executor jobs run with `privileged = false` wherever possible and each job's filesystem is ephemeral (a fresh container per job, no persistent job-to-job state) — this is the primary tenancy-isolation mechanism, since the Docker executor by design does not share job workspaces between unrelated pipelines.
- Runner **tags** (`docker`, `gpu`, `terraform-plan`) are a routing mechanism, not an isolation boundary — any project in the runner's registered group can target any tag it's authorized to use; project-level `protected` and `run_untagged` runner settings control which pipelines can actually dispatch to which runner.
- Secrets a job needs are scoped per-project via SVC-07's JWT/OIDC trust (SVC-07 §11) — the runner itself never holds a shared secret usable across projects.

## 5. Functional requirements

- **FR-1:** The runner fleet MUST register using **runner authentication tokens** (`glrt-` prefix), not the legacy registration-token workflow, per [GitLab's migration guidance](https://docs.gitlab.com/ci/runners/new_creation_workflow/) — legacy registration tokens are deprecated and this platform should not build new automation against a workflow already scheduled for removal.
- **FR-2:** Runner registration MUST be fully non-interactive (`gitlab-runner register --non-interactive --url ... --token ... --executor docker ...`), per [GitLab Runner's registration docs](https://docs.gitlab.com/runner/register/), invoked by Ansible with zero manual prompts.
- **FR-3:** At least one runner in the fleet MUST be tagged `gpu` and configured with `gpus = "all"` (and `service_gpus = "all"` if GPU access inside CI `services:` containers is needed) in the `[runners.docker]` config section, per [GitLab's GPU configuration docs](https://docs.gitlab.com/runner/configuration/gpus/), requiring the NVIDIA Container Toolkit installed on the runner's host.
- **FR-4:** Non-GPU runners MUST be tagged `docker` (general-purpose) and, optionally, `terraform` for jobs that specifically need the Terraform plan/apply toolchain pre-cached.
- **FR-5:** Runners MUST use the Docker executor exclusively (no shell executor, no Kubernetes executor), consistent with F3.
- **FR-6:** The runner fleet MUST support concurrent job execution (`concurrent` setting in `config.toml`) sized to the host's actual CPU/RAM capacity, not left at the default of 1.
- **FR-7:** Runner registration and tag assignment MUST be reproducible from Terraform/Ansible alone — destroying and recreating a runner VM/LXC must result in an identical, correctly-tagged runner with no manual dashboard steps.

## 6. Non-functional requirements

- **Availability target:** 95%+ during business hours is acceptable for a homelab/office CI fleet; brief runner downtime delays pipelines but does not lose data, since jobs simply queue.
- **Performance/sizing:** the `gitlab-runner` binary itself is lightweight (~470 MB Ubuntu image / ~270 MB Alpine image per [GitLab's Docker install docs](https://docs.gitlab.com/runner/install/docker/)), but the *jobs* it runs (Terraform plans, Docker builds, ML training smoke tests) can be resource-intensive — sizing is driven by expected job concurrency and job type, not by the runner binary's own footprint.
- **Backup/DR:** runner registration state (`config.toml`) is not itself precious — it is fully regenerable from Terraform/Ansible outputs and a fresh runner authentication token, so DR is "redeploy," not "restore from backup."
- **Data retention:** job logs live in GitLab itself (subject to GitLab's own retention settings), not on the runner; the runner's local Docker build cache is ephemeral and not backed up.

## 7. Infrastructure architecture

- **Compute unit:** **VM**, not LXC, for GPU-tagged runners — per F2, GPU passthrough requires full kernel isolation, which LXC does not reliably provide for PCIe passthrough on Proxmox. Non-GPU general-purpose runners MAY use LXC for lighter weight, but a VM is also acceptable and often simpler operationally since the Docker executor needs a real Docker daemon with standard cgroup/namespace behavior that some LXC configurations complicate (nested containerization caveats).
- **Minimum resource spec (general-purpose Docker-executor runner, LXC or VM):** 4 vCPU, 8 GB RAM, 60 GB disk (local-zfs) — sized to run 2-4 concurrent moderate CI jobs (Terraform plan, small Docker builds) without starving each other; disk is generous to accommodate Docker image layer caching.
- **Minimum resource spec (GPU-tagged runner, VM with PCIe passthrough):** 8 vCPU, 32 GB RAM, 200 GB disk (local-zfs, NVMe-backed if available), plus one passthrough-capable NVIDIA GPU (consumer-grade, e.g. an RTX-class card, is sufficient for CI-scale smoke tests and small fine-tuning jobs — full-scale training happens on SVC-23's dedicated model-serving infrastructure, not on the CI runner itself).
- **Network placement:** platform-core VLAN, static IP reservation; the runner reaches GitLab's API over HTTPS (outbound only — no inbound port needs to be opened, since runners poll for jobs rather than receiving pushed connections) and reaches Harbor (SVC-14) and OpenBao (SVC-07) on the internal network for image pulls and secret fetches.
- **Storage:** local-zfs for the Docker daemon's image/layer cache; NVMe-backed storage strongly recommended for the GPU runner given large ML framework image sizes (multi-GB CUDA base images).

## 8. Terraform scope

- **Module inputs:** `vmid`, `hostname` (`svc13-runner-01`, `svc13-runner-gpu-01`), `vlan_tag`, `static_ip`, `cpu_cores`, `memory_mb`, `disk_gb`, `proxmox_node`, `gpu_pci_id` (for GPU runners only), `runner_tags` (list, e.g. `["docker"]` or `["gpu", "ml"]`), `gitlab_group_id` (which GitLab group to register against).
- **Resources created:** one `proxmox_virtual_environment_vm` (GPU runners, with a `hostpci` block for passthrough) or `proxmox_virtual_environment_container` (general-purpose runners) per runner, via the bpg/proxmox provider; a GitLab-managed Terraform state entry keyed `svc-13-ci-runners`. Terraform also calls the GitLab API (via the `gitlabhq/gitlab` Terraform provider) to create the runner record and obtain its authentication token, storing that token directly into OpenBao rather than in Terraform state/outputs in plaintext.
- **Outputs:** `runner_internal_ip` (per runner), `runner_tags` — consumed by the Ansible dynamic inventory (F4) so the registration playbook knows which host to configure with which tags.

## 9. Ansible scope

- **Roles:** `gitlab_runner_install` (installs the `gitlab/gitlab-runner` Docker Compose stack or native package), `gitlab_runner_register` (runs `gitlab-runner register --non-interactive` using the authentication token fetched from OpenBao, applying the correct `--tag-list` and `--docker-*` flags per host role), `gitlab_runner_gpu_toolkit` (installs the NVIDIA driver + NVIDIA Container Toolkit on GPU-tagged hosts, idempotent via package-manager state checks).
- **Idempotency:** the registration playbook checks `gitlab-runner list` / the presence of a matching entry in `config.toml` before re-registering, so re-running Ansible against an already-registered runner is a no-op rather than creating duplicate runner entries.
- **Config files templated:** `config.toml` (`[[runners]]` blocks with `executor = "docker"`, `concurrent`, `[runners.docker]` with `gpus`/`service_gpus` where applicable).
- **Secrets injected from SVC-07:** the runner authentication token is fetched from OpenBao (`secret/data/platform/gitlab-runner/<runner-hostname>`) at registration time by the Ansible role — **no manual token copy-paste ever occurs**, satisfying the task's explicit requirement; the token is written directly into `config.toml` by the templated task, not passed through shell history or CI logs.

## 10. CI/CD pipeline

- **Stages:** `lint` (terraform fmt/validate, ansible-lint) → `plan` → manual approval → `apply` → `smoke test` (a trivial pipeline job dispatches to each newly-registered runner by tag and confirms it completes; for the GPU runner specifically, the smoke test runs `nvidia-smi` inside a CI job per [GitLab's own GPU verification guidance](https://docs.gitlab.com/runner/configuration/gpus/)).
- **Runs on:** this is somewhat self-referential — the *first* runner in the fleet must be bootstrapped by a pipeline running on GitLab.com's shared runners or a manually-triggered admin workstation run, since no self-hosted runner yet exists to build itself; subsequent runner additions/replacements run on the existing fleet.
- **State backend:** GitLab-managed Terraform state, per F1.

## 11. Secrets & credentials

- **What secrets exist:** the runner authentication token (`glrt-` prefix, one per registered runner), the GitLab API token used by Terraform's GitLab provider to create runner records programmatically, `DOCKER_AUTH_CONFIG` for pulling private images from Harbor (SVC-14) during job execution.
- **Where generated:** runner authentication tokens are created via the GitLab API (`POST /runners` or the group-level runner creation endpoint) by Terraform, using an admin/group-owner API token that itself lives in OpenBao.
- **Rotation:** runner authentication tokens are rotated whenever a runner is destroyed/recreated (new token each time, since Terraform requests a fresh one); the GitLab API token used by Terraform is rotated quarterly.
- **How they reach the service:** fetched from OpenBao by the Ansible `gitlab_runner_register` role at registration time, using the same JWT/OIDC or Ansible-Vault-bootstrapped access pattern described in SVC-07 §11.

## 12. Security & hardening baseline

- TLS everywhere: runners communicate with GitLab exclusively over HTTPS; the runner-to-GitLab connection is outbound-initiated (poll model), so no inbound TLS termination is needed on the runner itself.
- Least-privilege: Docker executor jobs run `privileged = false` by default; privileged mode is granted only per-project, per-job-tag exception (e.g. a project specifically needing Docker-in-Docker for its own image builds gets a separate `docker-privileged` tagged runner, isolated from the general pool).
- Firewall/VLAN rules: runners need only outbound HTTPS to GitLab, Harbor, and OpenBao — no inbound ports required, minimizing attack surface.
- CVE scanning: the `gitlab/gitlab-runner` image itself, and every job image a pipeline pulls, passes through Harbor's (SVC-14) proxy-cache with Trivy scanning applied.
- Auth via SVC-06: runner *management* (registering/removing runners, viewing fleet status) is done via the GitLab UI/API, itself gated by GitLab's own auth — if GitLab is configured to delegate SSO to ZITADEL, that inherits automatically; this is noted as a GitLab-server-level concern, not something this runner-fleet document configures directly.

## 13. Observability hooks

- Each `gitlab-runner` process exposes a Prometheus metrics endpoint (`--listen-address` flag, e.g. `:9252`) scraped by SVC-17.
- Runner job logs stream to GitLab directly (not to Loki) as that is GitLab's own log-handling path; however, the runner *daemon's* own operational logs (registration events, executor errors) are shipped to SVC-18/Loki via Promtail.
- Key alerts to define in Alertmanager: `gitlab_runner_offline` (a registered runner has not polled GitLab within an expected interval), `gitlab_runner_job_queue_depth_high` (jobs queuing because no runner with the needed tag is available — signals fleet under-capacity), `gitlab_runner_gpu_unavailable` (the GPU runner's `nvidia-smi` health check, run periodically as a scheduled pipeline, fails).

## 14. Acceptance criteria

- [ ] At least one general-purpose Docker-executor runner and one GPU-tagged runner provisioned via Terraform and registered via Ansible with zero manual token entry.
- [ ] A test pipeline job targeting the `docker` tag completes successfully.
- [ ] A test pipeline job targeting the `gpu` tag runs `nvidia-smi` successfully inside the CI job container.
- [ ] Destroying and recreating a runner VM/LXC via Terraform results in a correctly re-registered, correctly-tagged runner with no manual intervention.
- [ ] Runner authentication tokens are never observed in plaintext in Git history, CI job logs, or Terraform state — verified by a repo/state grep as part of the smoke test.
- [ ] Prometheus scrape targets live for all runners; Alertmanager rule for `gitlab_runner_offline` fires correctly in a manual test (stop a runner's service).

## 15. Open questions / assumptions

- Assumes the platform's GitLab instance (self-hosted or gitlab.com) is already reachable and that a group-owner-level API token for runner creation exists in OpenBao before this fleet is bootstrapped — the GitLab server deployment itself is out of scope here (see §2).
- The exact GPU model/count available in the Proxmox cluster for passthrough is not yet specified — sizing in §7 assumes a single consumer-grade NVIDIA GPU is available; if none is available yet, the `gpu`-tagged runner deployment is deferred and ML CI jobs run CPU-only in the interim.
- Whether to also stand up a parallel Forgejo Actions runner (`forgejo-runner`) now, purely as a documented fallback rehearsal, or defer entirely until the sovereignty trade-off actually needs to be exercised, is left open for a human decision.
- Assumes Docker-in-Docker (DinD) is not needed for the *default* runner pool — if a project's pipeline needs to build container images itself (rather than just running pre-built images), that project's own CI/CD section should specify whether it needs a `docker-privileged`-tagged runner or a rootless-Buildkit-based alternative, which this document does not mandate by default.
