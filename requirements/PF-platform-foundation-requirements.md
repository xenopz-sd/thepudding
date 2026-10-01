# Platform Foundation — Requirements

## 1. Overview

The platform foundation is not a single deployable product; it is the set of conventions, shared tooling, and bootstrap flows that every other service in the catalog (SVC-01 through SVC-32) is built on top of. It defines how compute is provisioned on Proxmox (Terraform), how that compute is configured (Ansible), how the two talk to each other (dynamic inventory), what runtime every service uses (Docker Compose), how traffic reaches services (Traefik), and how the first secret ever reaches a freshly built VM/LXC (the GitLab CI → OpenBao bootstrap). All project types in scope for this catalog — embedded/Raspberry Pi/NXP, drone hardware, and ML for video/image/text — depend on this foundation indirectly: none of their dedicated-per-project services (SVC-01 Postgres, SVC-23 model serving, etc.) can be stood up without it. This document is the prerequisite read for every other `SVC-xx-*-requirements.md` in the catalog.

## 2. Scope

**In scope:**
- Terraform state backend convention (GitLab-managed HTTP backend) and the `bpg/proxmox` provider.
- Proxmox compute unit conventions: LXC vs. VM decision rule.
- Base template strategy (cloud-init images) for both VM and LXC.
- Docker Compose as the only in-guest runtime; Traefik as the only reverse-proxy entrypoint.
- Dynamic Ansible inventory generation from Terraform outputs.
- Secrets bootstrap flow: GitLab CI/CD variables → OpenBao (SVC-07) initial unseal/auth.
- Network/VLAN layout conventions and per-project resource naming conventions.
- Sovereignty trade-off of GitLab SaaS vs. self-hosted GitLab/Forgejo for Terraform state.

**Out of scope:**
- The internal implementation of any individual `SVC-xx` service (each has its own requirements doc).
- Kubernetes/k3s of any kind — explicitly excluded per catalog decision F3.
- Multi-site/multi-datacenter failover (single Proxmox cluster assumed for this homelab/office scale).
- Public cloud provisioning (AWS/Azure/GCP) — this foundation is Proxmox-only.

## 3. Technology selection

| Decision | Choice | Origin / rationale (carried from catalog) |
|---|---|---|
| IaC provider | [`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox/latest/docs) Terraform provider | Actively maintained, API-token based, SSH optional — already the catalog's chosen provider (catalog §1). |
| Terraform state backend | GitLab-managed Terraform state, native HTTP backend (`terraform { backend "http" {...} }`) | Decision F1: simplest path since GitLab already hosts CI. Supported natively by GitLab (self-managed and SaaS) via the `terraform/state/<name>` API endpoint ([GitLab Docs](https://docs.gitlab.com/user/infrastructure/iac/terraform_state/)); requires GitLab 18.3+ for the newer OpenTofu/Terraform state UI, but the HTTP backend itself has been supported since GitLab 13.x ([GitLab Docs](https://docs.gitlab.com/administration/terraform_state/)). |
| Compute unit | LXC by default; VM only for GPU passthrough or kernel isolation | Decision F2. LXC has near-zero overhead vs. a VM for stateless Compose workloads on a small cluster; VM is required whenever PCIe/GPU passthrough (`hostpci0`) or a different kernel/namespace boundary is needed (e.g. SVC-23 model serving with an NVIDIA GPU). |
| Runtime | Docker Compose per VM/LXC | Decision F3. No Kubernetes/k3s anywhere in the stack. |
| Reverse proxy | Traefik (SVC-09), Traefik Labs, Lyon, France 🇪🇺 | Already in use; single shared entrypoint for all HTTP(S) services. |
| Ansible inventory source | Terraform outputs rendered to a dynamic inventory file | Decision F4. |
| Secrets bootstrap | GitLab CI/CD variables → OpenBao (SVC-07) | Decision F5. |

**Alternative considered (not chosen as default):** a Kubernetes-based platform (k3s/k0s) was considered and rejected per decision F3 — the operational overhead of a cluster scheduler is not justified for a small homelab/office Proxmox cluster running a handful of concurrent projects; Docker Compose per VM/LXC plus Traefik gives equivalent routing/TLS ergonomics with far less to operate.

**Sovereignty flag:** if **gitlab.com SaaS** (rather than a self-hosted GitLab instance) is used as the Terraform state backend and CI host, Terraform state — which contains resource IDs, IPs, and in some cases sensitive attribute values — transits GitLab Inc.'s **US-hosted infrastructure**. This is an explicit trade-off accepted for simplicity today. If EU data sovereignty later becomes a hard requirement, the fully-European alternative is **self-hosted GitLab CE** (self-hosted, same HTTP Terraform backend, operator controls the region) or **[Forgejo](https://forgejo.org/)** (Codeberg e.V., Berlin, Germany 🇩🇪, MIT/GPLv3, fully self-hostable) with Forgejo Actions replacing GitLab CI and a self-hosted state backend (e.g. Forgejo's own artifact storage, or fall back to an S3-compatible backend on SVC-03 Garage). This mirrors the SVC-13 CI-runner catalog entry, which already lists Forgejo as the fully-sovereign alternative to GitLab Runner.

## 4. Multi-tenancy model

The platform foundation itself is **shared infrastructure** — there is exactly one Proxmox cluster, one GitLab (or Forgejo) instance, one Terraform state project hierarchy, and one Ansible control setup for the whole homelab/office. Per-project isolation is implemented one layer up, inside each `SVC-xx` service's own tenancy model (dedicated-per-project vs. shared-with-namespacing, per catalog §1). The foundation's job is to make that per-project isolation *possible and consistent*:

- Every project gets its own Terraform **root module directory** (e.g. `infra/projects/<slug>/`) and its own **GitLab-managed state name** (`<slug>-infra`), so a `terraform destroy` for one project cannot touch another's state.
- Every Proxmox resource (VM/LXC) created for a project is tagged with the project slug (Proxmox `tags` attribute, e.g. `proj-dronefleet`) so it is filterable in the Proxmox UI and in Ansible inventory group_vars.
- Naming convention (binding for all services): **`proj-<slug>-<service>`**, e.g. `proj-dronefleet-postgres`, `proj-mlvideo-redis`, `proj-shared-zitadel` for cluster-wide shared services. `<slug>` is lowercase, hyphenated, max 20 characters, assigned once per project at kickoff and never reused.
- This slug-based tag is distinct from, and complementary to, the **hostname**: the Proxmox `tags` attribute above is for human-facing filtering (Proxmox UI, Ansible group_vars), while the actual hostname/DNS record follows the VLAN-correlated `<svc-code>-<vlan_id>-<instance>` convention defined in [`NET-00-vlan-ip-addressing-plan.md`](./NET-00-vlan-ip-addressing-plan.md) §4. Both are set on the same resource; neither replaces the other.

## 5. Functional requirements

- **FR-1**: Terraform MUST authenticate to Proxmox via the `bpg/proxmox` provider using an API token (not root password), scoped to a dedicated Terraform service account (`terraform@pve`) with a custom role limited to VM/LXC/storage/network privileges.
- **FR-2**: Terraform state for every project and every shared service MUST be stored in a distinct GitLab-managed state name, addressable via `https://<gitlab-host>/api/v4/projects/<id>/terraform/state/<name>`.
- **FR-3**: Every new project MUST be bootstrapped from a Terraform module that accepts at minimum: `project_slug`, `environment` (dev/prod), `vlan_id`, `compute_unit` (`lxc`|`vm`), `template_id`, `cpu_cores`, `memory_mb`, `disk_gb`.
- **FR-4**: All VM and LXC templates MUST be built from cloud-init-enabled base images (see §7) so that Terraform can inject SSH keys, hostname, and network config at first boot without manual imaging.
- **FR-5**: A CI job MUST render a dynamic Ansible inventory (INI or YAML) from `terraform output -json` after every successful `apply`, grouping hosts by `project_slug` and by `service_role` tag.
- **FR-6**: Every service VM/LXC MUST run its workload as a Docker Compose stack (`docker compose up -d`) defined in a per-service `compose.yaml` checked into the project's Ansible role, with no manually-run `docker run` commands in production.
- **FR-7**: Every HTTP/HTTPS-exposed service MUST register with the shared Traefik instance via Docker labels (`traefik.enable=true`, `traefik.http.routers.<name>.rule=Host(...)`) rather than exposing host ports directly to the LAN.
- **FR-8**: The first secret used by any new VM/LXC (e.g. the OpenBao AppRole `role_id`/`secret_id` or an initial unseal-adjacent bootstrap token) MUST originate from a GitLab CI/CD protected variable, never committed to a repository.
- **FR-9**: Ansible playbooks MUST be idempotent — a second run against an already-configured host must report zero changes (`changed=0`) barring genuine drift.

## 6. Non-functional requirements

- **Availability**: the Proxmox cluster itself targets best-effort availability (single-site homelab/office, no multi-site failover in scope); individual services define their own SLAs in their own requirements docs.
- **Performance/sizing** (small homelab/office cluster, a handful of concurrent embedded/drone/ML projects): assume a Proxmox cluster of 1–3 physical nodes, each with 8–32 physical cores and 64–256 GB RAM, local-zfs or local-lvm storage, plus at least one GPU-capable node for VM-based ML workloads. Reserve headroom for at least 6–10 concurrent small LXC/VM service instances (Postgres, Redis, Garage, ZITADEL, Traefik, OpenBao, plus 2–3 project-dedicated stacks) alongside 1–2 GPU VMs.
- **Backup/DR**: the Proxmox host layer is backed up via Proxmox Backup Server (SVC-32); Terraform state is backed up implicitly by GitLab's own backup/retention (or the Forgejo instance's backup, if self-hosted) — this is a dependency to document, not something the foundation re-implements.
- **Data retention**: Terraform state history is retained per GitLab's default versioning for managed state (no separate lifecycle policy needed at this scale).

## 7. Infrastructure architecture

- **Compute unit choice**: LXC (unprivileged where possible) is the default for every service that does not need a distinct kernel or PCIe passthrough — this covers Postgres, Redis, Garage, ZITADEL, OpenBao, Traefik, GitLab Runner, Harbor, Prometheus/Grafana, Mosquitto, Mender, etc. VMs are reserved for: (a) GPU passthrough workloads (SVC-23 model serving, SVC-27 GPU scheduling), (b) anything requiring a different/newer kernel than the Proxmox host, (c) nested virtualization needs.
- **Base templates**: maintain two Proxmox template families, both cloud-init enabled:
  - **VM template**: Debian 12 ("bookworm") or Ubuntu 22.04/24.04 LTS generic cloud image (`.qcow2`), imported once via `qm importdisk`, converted to a Proxmox template, with `cloud-init` drive attached (`ide2`) for Terraform-injected SSH keys/network config.
  - **LXC template**: official Debian 12 or Ubuntu 24.04 standard LXC template downloaded via `pveam` from the default Proxmox template repository, with Docker + Docker Compose plugin pre-baked (via a templating/Ansible pre-seed step) to avoid re-installing Docker on every container.
- **Minimum resource spec per foundation component**:
  - Traefik LXC: 1 vCPU, 256 MB RAM, 4 GB disk.
  - OpenBao LXC: 1 vCPU, 512 MB–1 GB RAM, 8 GB disk (plus its own persistent storage backend, typically Postgres-backed — see SVC-07 doc).
  - GitLab Runner LXC (if self-hosted runners are used): 2 vCPU, 2–4 GB RAM, 20 GB disk (more if Docker-in-Docker build caches are kept locally).
- **Network placement**: superseded by the concrete VLAN allocation, CIDR/IP automation, hostname convention, and Proxmox SDN implementation defined in [`NET-00-vlan-ip-addressing-plan.md`](./NET-00-vlan-ip-addressing-plan.md) — read that document for the binding scheme. Summary only: VLAN 10 is management-only (never routed to project workloads), VLAN 20 hosts shared platform services, VLAN IDs 30–99 are a reserved buffer, and every project gets its own dedicated VLAN starting at 100 and incrementing sequentially, never reused. Every VLAN's subnet and every host's static IP are computed by Terraform, not chosen by an operator.
- **Storage**: `local-zfs` for all VM/LXC root disks (snapshot support needed for Proxmox Backup Server incremental backups); a separate ZFS dataset or NFS share (mounted from a NAS or a dedicated storage LXC) for larger bulk data (Garage's data volumes, Postgres Backup Server target datastore).

## 8. Terraform scope

- **Module inputs** (shared `proxmox-compute` module used by every service/project): `project_slug`, `service_name`, `compute_unit` (`lxc`|`vm`), `node_name`, `template_id`, `vmid` (or auto-assigned via `pm_api_id_range`), `cpu_cores`, `memory_mb`, `disk_gb`, `vlan_id`, `ip_config` (static CIDR computed per [`NET-00-vlan-ip-addressing-plan.md`](./NET-00-vlan-ip-addressing-plan.md) §3 — no manual DHCP reservation step), `ssh_public_keys`, `tags`, `hostname` (per NET-00 §4).
- **Resources created**: `proxmox_virtual_environment_container` (LXC) or `proxmox_virtual_environment_vm` (VM) from the `bpg/proxmox` provider; associated `proxmox_virtual_environment_file` resources for cloud-init snippets where needed.
- **Outputs**: `hostname`, `ipv4_address`, `vmid`, `service_role` tag, `project_slug` — consumed directly by the Ansible dynamic inventory generator (FR-5) and cross-referenced by other services' Terraform (e.g. SVC-01's Postgres host IP is read by a project's application-stack Terraform to template a connection string into OpenBao).
- Each project/service Terraform root MUST declare its own `backend "http"` block pointing at a unique GitLab-managed state name; no shared/global state file.

## 9. Ansible scope

- **Roles**: `common` (baseline hardening, timezone, unattended-upgrades, Docker + Compose plugin install), `traefik` (shared reverse proxy), `openbao-agent` (installs and configures the OpenBao Agent or the AppRole login sidecar used by every other service to fetch its secrets at container start), `docker-compose-app` (generic role that templates a `compose.yaml` and `.env` from Jinja2, given a service-specific variable set).
- **Idempotency**: every role MUST be safe to re-run; package installation uses `state: present` (not `latest`, to avoid unplanned drift), Compose stacks are reconciled via `community.docker.docker_compose_v2` module (declarative) rather than shell `docker compose up` calls.
- **Config files templated**: Traefik static/dynamic config (`traefik.yml`, per-service dynamic routers), Docker Compose `.env` files per service, OpenBao Agent config (`agent.hcl`) for the AppRole login and secret-templating (`template` stanzas writing rendered secrets into a service's `.env`).
- **Secrets injected from SVC-07**: no secret is ever templated directly from Ansible variables/vault files in this foundation; Ansible only configures the OpenBao Agent, which then pulls the actual secret values (DB passwords, API keys) from OpenBao at container-start time, per the bootstrap flow in §11.

## 10. CI/CD pipeline

Stages (GitLab CI, matches decision F1's GitLab-centric assumption; portable to GitHub Actions if the fully-sovereign Forgejo/GitHub path is chosen later):

1. **lint** — `terraform fmt -check`, `tflint`, `ansible-lint`.
2. **plan** — `terraform plan` against the project's GitLab-managed state (`TF_HTTP_ADDRESS` derived from `CI_API_V4_URL`), plan output posted as an MR comment.
3. **manual/auto approve** — `plan` on merge requests requires manual approval; `apply` on the default branch can be automatic for dev environments, manual-gated for anything touching shared platform services.
4. **apply** — `terraform apply -auto-approve` (only after approval gate), followed by the Ansible playbook run against the freshly rendered dynamic inventory.
5. **smoke test** — a lightweight post-deploy check (e.g. `curl -sf https://<service>.<domain>/healthz` through Traefik, or a Compose `docker compose ps` health check) that fails the pipeline if the service did not come up cleanly.

State backend reference: every stage that runs `terraform` uses the GitLab-managed HTTP backend per decision F1, authenticated via the pipeline's `CI_JOB_TOKEN` (no separate PAT needed for same-project state).

## 11. Secrets & credentials

- **What secrets exist at the foundation layer**: the Proxmox API token used by Terraform, the initial OpenBao AppRole `role_id`/`secret_id` (or root/recovery tokens used only once at OpenBao's own initial setup), and SSH keys injected into cloud-init templates.
- **Where generated**: the Proxmox API token is generated once manually by an operator in the Proxmox UI/CLI and stored as a GitLab CI/CD **protected + masked** variable (`PROXMOX_API_TOKEN`). OpenBao's own root/unseal material is generated at OpenBao's first initialization and is handled entirely outside this foundation flow (see SVC-07 doc); only the AppRole credentials used by Ansible/Terraform to *authenticate to* OpenBao live here.
- **Bootstrap flow (decision F5, exact sequence)**:
  1. GitLab CI/CD protected variables hold the Proxmox API token and the OpenBao AppRole `role_id` + `secret_id` for a narrowly-scoped "bootstrap" policy.
  2. The pipeline's `apply` stage uses the Proxmox token to create infrastructure via Terraform.
  3. The pipeline's post-apply Ansible run uses the OpenBao AppRole credentials (passed as CI variables, never written to disk in the repo) to have each new host's OpenBao Agent authenticate and pull its own scoped secrets (its DB password, its S3 keys, etc.).
  4. After first successful bootstrap, day-2 secret rotation happens entirely inside OpenBao (dynamic secrets/leases where supported) — GitLab CI variables are not touched again except when the bootstrap AppRole credentials themselves are rotated (recommended every 90 days).
- **Rotation**: the Proxmox API token and the bootstrap AppRole secret are rotated manually on a quarterly cadence (or immediately on suspected compromise); rotation is a two-step process (issue new, update GitLab variable, revoke old) to avoid a pipeline outage window.

## 12. Security & hardening baseline

- TLS everywhere is enforced at the Traefik layer (SVC-09): all internal service-to-service HTTP traffic that crosses a VLAN boundary is proxied through Traefik with automatic Let's Encrypt (or an internal ACME/EJBCA-issued cert per SVC-08) certificates; plaintext HTTP is only acceptable on the loopback/Compose-internal network within a single host.
- Least-privilege: the Terraform Proxmox API token uses a custom PVE role (not `Administrator`) scoped to VM/LXC/storage/network operations only; the OpenBao bootstrap AppRole has a policy that can only read the narrow set of secret paths needed for initial host bootstrap, never full root access.
- Firewall/VLAN rules: Proxmox's built-in firewall (or the physical switch's ACLs) restricts management-VLAN traffic to Proxmox hosts and the CI runner only; project VLANs cannot initiate connections to the management VLAN.
- CVE scanning: base VM/LXC template images are rebuilt monthly (or on critical CVE disclosure) from upstream Debian/Ubuntu cloud images; Docker images pulled by Compose stacks are scanned by Harbor's built-in Trivy scanner (SVC-14) before being promoted to a "trusted" project registry namespace.
- Auth via SVC-06 (ZITADEL): the Proxmox web UI itself is not federated (Proxmox has limited external-IdP support at the PVE-realm level and is out of scope to re-architect here), but every custom foundation-adjacent web UI (a homemade dashboard, if any) MUST use OIDC against ZITADEL rather than a local user database.

## 13. Observability hooks

- Every Proxmox host exports metrics via the built-in `pve-exporter` (or `node_exporter` installed alongside) for Prometheus scraping (SVC-17).
- Traefik exposes a Prometheus metrics endpoint (`--metrics.prometheus=true`, default path `/metrics` on its internal entrypoint) scraped by Prometheus.
- OpenBao exposes a `/v1/sys/metrics?format=prometheus` endpoint for Prometheus scraping once telemetry is enabled in its config.
- All Compose stack container logs are shipped via Promtail (or Docker's `json-file` driver tailed by Promtail) to Loki (SVC-18), tagged with `project_slug` and `service_role` labels for filtering.
- Key alerts to define in Alertmanager: Proxmox node down/degraded, ZFS pool degraded, any LXC/VM failing its Compose healthcheck for >5 minutes, Terraform state lock held for an abnormally long time (indicates a stuck pipeline), OpenBao sealed unexpectedly.

## 14. Acceptance criteria

- [ ] `bpg/proxmox` provider configured and authenticating via a scoped API token, verified with a `terraform plan` that reads existing cluster state without error.
- [ ] At least one VM template and one LXC template exist, both cloud-init enabled, both successfully cloned by a test Terraform apply.
- [ ] GitLab-managed Terraform state confirmed working end-to-end (init/plan/apply/state lock) for at least one project.
- [ ] Dynamic Ansible inventory correctly generated from `terraform output -json` and used successfully by a subsequent `ansible-playbook` run.
- [ ] Traefik deployed and successfully routing to at least one test backend service over HTTPS.
- [ ] OpenBao bootstrap flow demonstrated: a GitLab CI variable-sourced AppRole credential successfully authenticates a fresh host and retrieves a test secret.
- [ ] Naming convention (`proj-<slug>-<service>`) applied consistently across at least one full project's resources.
- [ ] VLAN segmentation applied and verified (management VLAN unreachable from project VLAN).

## 15. Open questions / assumptions

- Assumes a single physical Proxmox cluster (no multi-site DR) is acceptable for this homelab/office scale; revisit if a second physical site is added.
- Assumes GitLab (SaaS or self-hosted) is the CI host of record for the initial rollout; the Forgejo migration path is documented but not scheduled — a human decision is needed on *when* (if ever) to exercise it.
- Assumes Debian/Ubuntu LTS as the base OS family for all templates; no current requirement for other distributions (e.g. Alpine-only minimal images), but lightweight LXC services could later standardize on Alpine to reduce footprint — open for a future revision.
- ~~Static IP vs. DHCP-reservation policy per service tier is described at a conventions level (§7) but the exact IP allocation plan (which /24, which VLAN IDs) is left to the operator to finalize before the first `terraform apply`.~~ **Resolved** — see [`NET-00-vlan-ip-addressing-plan.md`](./NET-00-vlan-ip-addressing-plan.md): VLAN allocation, CIDR formula, and hostname convention are now fully specified and Terraform-computed. The one remaining manual step is confirming the physical switch trunk permits the chosen VLAN ID range (NET-00 §8) — a one-time, out-of-band network decision, not a per-project one.
