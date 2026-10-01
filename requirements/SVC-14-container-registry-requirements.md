# SVC-14 — Container Registry Requirements

## 1. Overview

SVC-14 is the shared container registry, **Zot**, that stores every Docker/OCI image built and consumed on this platform: application images built by CI pipelines, base images (including a pull-through cache of upstream images like `traefik`, `openbao/openbao`, `gitlab/gitlab-runner`) used to reduce Docker Hub rate-limit exposure, and ML model-serving container images for the ML-video/ML-text project types. Zot embeds **Trivy** as a Go library (not a separate scanner container) via its `search` extension, so every image can be scanned for CVEs without any additional moving parts — see [zot CVE scanning docs](https://zotregistry.dev/v2.1.14/articles/cve-scanning/). It is Tier 1 because SVC-13's CI runners, SVC-09's Traefik, SVC-07's OpenBao, and every dedicated per-project service image all flow through this registry at some point — either as the origin of a project's own built image, or as the proxy-cached source of an upstream base image. All project types depend on it: embedded/drone projects for their build-tool and cross-compilation container images, ML-video/ML-text projects for model-serving containers, and generic backend projects for their application images.

Zot replaces **Harbor** as the platform default (catalog revision 2.1) specifically because Zot runs as a **single static binary in a single Docker Compose service** with no external Postgres/Redis dependency, which materially simplifies both the permanent shared instance and any temporary docker-compose instance spun up for local testing — a pain point Harbor's ~8-container stack (core, portal, jobservice, registry, Redis, Postgres, Trivy adapter, nginx) does not solve well. Harbor remains documented as a heavier fallback (see §3) for teams that later need its full multi-project quota/RBAC UI at larger scale.

## 2. Scope

**In scope:**
- One shared Zot instance (full image with all extensions enabled — `ghcr.io/project-zot/zot-linux-amd64:latest`, **not** the `-minimal` build, which strips the UI, search, CVE scanning, and sync extensions — per the [RamNode Zot deployment guide](https://www.ramnode.com/guides/zot)) serving all projects, with per-project repository-path namespacing for push/pull isolation.
- Per-project CI identities (htpasswd or LDAP-bound accounts, see §4/§11) scoped to their own repository path prefix — Zot's functional equivalent of Harbor's robot accounts, though provisioned differently (see §4).
- Embedded Trivy vulnerability scanning and SBOM data via the `search`/CVE extension, scan-on-demand triggered from CI.
- Repository count cap (`storage.maxRepos`) and per-repository tag retention policies to bound unbounded growth — see the important gap noted below.
- The `sync` extension configured in **on-demand (pull-through cache)** mode for common upstream registries (Docker Hub, GHCR, Quay) to mitigate anonymous-pull rate limits, per the [zot mirroring docs](https://zotregistry.dev/v2.1.11/articles/mirroring/).

**Out of scope:**
- Helm chart repository features (OCI artifact storage for Helm charts) — not needed since F3 excludes Kubernetes/Helm entirely from this platform.
- Cosign-based image signing — deferred, noted as a future hardening step (see §15), same as previously scoped for Harbor.
- Periodic (scheduled) mirroring/replication — Zot's `sync` extension supports this mode too, but it is not configured here; only on-demand pull-through caching is in scope.
- **Byte-level per-project storage quotas** — unlike Harbor, Zot does not have a native "N GB per project" quota. Its only built-in cap is `storage.maxRepos` (a global count of repositories, not bytes), added per the [zot 2.1.16 release notes](https://zotregistry.dev/v2.1.16/general/whats-new/). Storage growth is instead bounded indirectly via tag retention policies (§5) and Prometheus disk-usage alerting (§13) — flagged as a known functional gap versus Harbor in §15.

## 3. Technology selection

**Chosen:** [Zot](https://github.com/project-zot/zot) (`project-zot/zot`), deployed via Docker Compose as a single container running the official `ghcr.io/project-zot/zot-linux-amd64` image with a mounted `config.json` and a persistent volume for `storage.rootDirectory`. License: Apache 2.0. Governance: originated at **Cisco** (US) and accepted into the **CNCF Sandbox** on December 13, 2022 — confirmed via the [CNCF Zot Sandbox page](https://www.cncf.io/projects/zot/) and [RudeTools' registry comparison](https://rudetools.dev/registries/zot) — so, like Harbor, day-to-day technical direction runs through CNCF's open governance rather than a single vendor roadmap, even though origin and dominant contributor base remain US-linked.

**European-sourcing flag (per catalog §3.5 and §1 guiding principles):** unchanged from the Harbor-era assessment — no actively-maintained strong European equivalent with comparable integrated CVE scanning was found. **Zot and Harbor are equally non-European** (Cisco vs. VMware/Broadcom, both US); switching from one to the other is a pure operational-simplicity change, not a sourcing improvement. The one genuinely European option remains **Forgejo's built-in container registry** (🇪🇺 Codeberg e.V., Berlin, Germany) — zero extra service if a project is already self-hosting Forgejo per SVC-13's sovereign alt — but it has no registry-native scanning or retention policy engine; Trivy would need to run as a separate CI step instead, per the [Gitea container registry docs](https://docs.gitea.com/usage/packages/container/).

**Alternative considered — Harbor (demoted to heavier fallback):** [`goharbor/harbor`](https://goharbor.io), still a fully valid choice for a project that needs Harbor's mature multi-project quota/RBAC administration UI at scale. Not chosen as the *default* because its Compose stack requires ~8 interdependent containers (core, portal, jobservice, registry, Redis, PostgreSQL, Trivy adapter, nginx) versus Zot's one binary/one Compose service — a meaningful difference when a developer wants a temporary registry running locally via `docker compose up` and torn down again, which was the concrete pain point that triggered this re-evaluation (per the [Railway Blog registry weight comparison](https://blog.railway.com/p/best-container-registries-2026)).

**Alternative considered — plain Docker Registry (`registry:2`):** rejected for the same reason it was rejected against Harbor previously — no scanning, no UI, no per-repository RBAC out of the box; all of that would need to be bolted on, which defeats the purpose of choosing a registry with integrated security tooling.

## 4. Multi-tenancy model

**Pattern:** Shared platform instance (per catalog §1) — one Zot deployment serves all projects.

**Isolation implementation (materially different from Harbor's "projects" object):**
- Zot has no Harbor-style "project" API resource. Namespace isolation is implemented purely through **repository path prefix convention**: images are pushed as `zot.internal.example.com/<project-slug>/<image-name>:<tag>`, mirroring the same slug convention documented for Harbor.
- Access to each path prefix is granted via an **`accessControl`** policy block in `config.json`, which is an identity-based (user/group) authorization mechanism scoping read/create/update/delete actions to one or more repository paths — see the [zot authn/authz docs](https://zotregistry.dev/v2.1.18/articles/authn-authz/).
- Because `accessControl` is declarative configuration rather than a live REST-managed object (unlike Harbor's `POST /projects`), onboarding a new project means the Ansible role (§9) appends a new `accessControl` group entry scoped to `<project-slug>/*` into the templated `config.json` and reloads the Zot service — a materially different operational model than Harbor's live API call, and one that needs a brief service restart/reload rather than being instantaneous.
- **CI identities** (Zot's functional equivalent of Harbor's ephemeral robot accounts) are provisioned as **htpasswd entries** (default) or LDAP-bound service accounts, one per project, named `ci-<project-slug>`, each granted only read+create+update on their own `<project-slug>/*` path prefix — never global/admin scope. Zot's `htpasswd` and `LDAP` auth methods can be combined, with `htpasswd` serving as the local fail-safe if LDAP is briefly unavailable, per the [zot authn/authz docs](https://zotregistry.dev/v2.1.11/articles/authn-authz/).
- The Docker Hub/GHCR/Quay pull-through cache (`extensions.sync`, on-demand mode) is a single global config block, not per-project, since caching upstream images is inherently a shared-benefit function — same rationale Harbor's proxy-cache projects had.

## 5. Functional requirements

- **FR-1:** The onboarding Ansible role MUST append a new `accessControl` group entry scoped to `<project-slug>/*` to Zot's templated `config.json`, and reload the Zot service, when a new platform project is onboarded.
- **FR-2:** Zot MUST support on-demand CVE scanning of pushed images via its embedded Trivy library (the `search` extension is a mandatory prerequisite for CVE scanning and must be enabled), with the Trivy vulnerability DB fetched from `ghcr.io/aquasecurity/trivy-db` and refreshed on a configured `updateInterval`, per the [zot CVE scanning docs](https://zotregistry.dev/v2.1.14/articles/cve-scanning/).
- **FR-3:** Local authentication MUST be enforced via `htpasswd` (LDAP optional, once available); each project's CI identity MUST be scoped to its own repository path prefix only via `accessControl` — no shared/global CI credential may be used across projects.
- **FR-4:** Severity-threshold enforcement ("block deploy on unresolved Critical/High CVEs") is **not** a native Zot project-level toggle the way it is in Harbor — this MUST instead be implemented as a CI pipeline gate that queries Zot's CVE API for the pushed digest and fails the pipeline if the configured severity threshold is exceeded, before the image is promoted to deployment.
- **FR-5:** Zot's `sync` extension MUST be configured in **on-demand** mode for Docker Hub, to reduce exposure to Docker Hub's anonymous pull-rate limit, per the [zot mirroring docs](https://zotregistry.dev/v2.1.11/articles/mirroring/); GHCR and Quay on-demand sync entries SHOULD be added as secondary upstreams if usage warrants it.
- **FR-6:** Zot MUST apply per-repository tag retention rules (e.g., retain the top-N most-recently-pushed or most-recently-pulled tags) to bound storage growth automatically, per the [zot tag retention docs](https://zotregistry.dev/v2.1.15/articles/retention/).
- **FR-7:** Zot's HTTP(S) API/UI MUST be exposed over HTTPS only (port 443), proxied through Traefik (SVC-09) exactly as every other shared service on this platform.
- **FR-8:** Human user login to the Zot UI MUST be delegated to ZITADEL (SVC-06) via Zot's `openid` provider configuration (a custom OIDC provider entry, the same mechanism documented for Dex/Keycloak), rather than maintaining a separate local user database, per the [zot OpenID/OAuth2 provider examples](https://github.com/project-zot/zot/blob/main/examples/README.md).
- **FR-9:** Zot MUST enforce a global `storage.maxRepos` cap to prevent unbounded repository sprawl, per the [zot 2.1.16 release notes](https://zotregistry.dev/v2.1.16/general/whats-new/), acknowledging this is a repository-count cap, not a per-project byte quota (see §2 and §15).

## 6. Non-functional requirements

- **Availability target:** 99% during business hours; Zot being briefly unavailable blocks new image pushes/pulls but does not affect already-running containers on any host (Docker caches pulled layers locally) — same characteristic as Harbor.
- **Performance/sizing:** Zot's idle memory footprint is well under 256 MB (per the [RamNode deployment guide](https://www.ramnode.com/guides/zot)), a fraction of Harbor's recommended 8 GB, but memory grows with active concurrent scans and sync jobs, so a modest but non-trivial allocation is still budgeted (see §7).
- **Backup/DR:** Zot's `storage.rootDirectory` (the OCI image layout on disk — there is no separate metadata database to back up, unlike Harbor's PostgreSQL) is backed up nightly to SVC-32; a documented restore procedure (restore the storage directory, then `docker compose up`) is validated at least once after initial deployment.
- **Data retention:** tag retention policies (FR-6) bound image count/age per repository; the embedded Trivy vulnerability DB is refreshed on its own `updateInterval` schedule, not subject to platform retention rules.

## 7. Infrastructure architecture

- **Compute unit:** LXC container is viable here (unlike Harbor, which needed a VM for its multi-container stack) — Zot is a single static binary with modest resource needs and no other interdependent containers to isolate; an LXC keeps provisioning lightweight. A VM remains an acceptable alternative if the platform standardizes on VMs for all shared services regardless of individual footprint.
- **Minimum resource spec:** 2 vCPU, 4 GB RAM, 80 GB disk (local-zfs), based on the [RamNode Zot VPS sizing guidance](https://www.ramnode.com/guides/zot) (2 vCPU/4 GB/80+ GB recommended plan, sub-256 MB idle footprint) — a substantial reduction from Harbor's 4 vCPU/8 GB/160 GB recommended tier; scale the disk allocation up if ML-serving container images (which can run several GB each) proliferate faster than anticipated.
- **Network placement:** platform-core VLAN, static IP reservation; reachable internally by every CI runner (SVC-13) and by Traefik (SVC-09) for hostname routing; port 443 only externally, per FR-7.
- **Storage:** local-zfs volume dedicated to `storage.rootDirectory`, sized independently of the OS disk so it can be expanded without touching the container/VM's root filesystem; local-zfs (not NFS) is preferred for latency-sensitive registry blob reads during CI image pulls, consistent with the platform's general storage convention. Zot's inline `dedupe` and `gc`/`gcDelay`/`gcInterval` storage options (per the [zot storage planning docs](https://zotregistry.dev/v2.1.9/articles/storage/)) are enabled to reduce disk consumption further without taking the registry offline.

## 8. Terraform scope

- **Module inputs:** `vmid`/`ctid`, `hostname` (`svc14-zot`), `vlan_tag`, `static_ip`, `cpu_cores`, `memory_mb`, `disk_gb` (OS), `storage_root_disk_gb` (separate volume for `storage.rootDirectory`), `proxmox_node`, `zot_hostname` (public/internal FQDN).
- **Resources created:** one `proxmox_virtual_environment_container` (LXC, per §7) or `_vm` with an attached secondary disk for `storage.rootDirectory`; a GitLab-managed Terraform state entry keyed `svc-14-container-registry`.
- **Outputs:** `zot_internal_ip`, `zot_hostname`, `zot_api_url` — consumed by the Ansible dynamic inventory and by every other service's Ansible role/CI pipeline that needs to know where to push/pull images from.

## 9. Ansible scope

- **Roles:** `zot_install` (downloads/pins the `ghcr.io/project-zot/zot-linux-amd64` image at a specific tag, templates `config.json`, runs `docker compose up -d`), `zot_project_onboard` (appends a new `accessControl` group and a new `ci-<project-slug>` htpasswd entry to the templated `config.json` given a `project_slug` variable, then reloads/restarts the Zot service — see §4 for why this differs from Harbor's live API call), `zot_sync_setup` (templates the shared `extensions.sync` on-demand pull-through-cache block for Docker Hub/GHCR/Quay, once, platform-wide).
- **Idempotency:** the onboarding role checks the templated `config.json` for an existing `accessControl` entry matching the project slug before appending a new one, so re-running Ansible against an already-onboarded project is a no-op; config regeneration on re-run does not disrupt existing image storage since `storage.rootDirectory` is a stable path across template re-application.
- **Config files templated:** `config.json` (storage root, HTTP/TLS settings, `accessControl` groups, `htpasswd` file path, `extensions.search`/CVE settings, `extensions.sync` upstreams, `extensions.metrics`, tag retention rules, `storage.maxRepos`), `htpasswd` (generated/appended by the onboarding role, not hand-edited).
- **Secrets injected from SVC-07:** the ZITADEL OIDC client secret (for FR-8) and per-project CI identity passwords (hashed into the `htpasswd` file) are fetched from/written to OpenBao at deploy time rather than being hardcoded into `config.json`; per-project CI credentials generated by the onboarding role are written back into OpenBao under `secret/data/<project-slug>/zot-ci-credential` so CI pipelines can retrieve them via the JWT/OIDC flow described in SVC-07 §11.

## 10. CI/CD pipeline

- **Stages:** `lint` (terraform fmt/validate, ansible-lint, `config.json` template syntax/schema check) → `plan` → manual approval → `apply` → `smoke test` (push a trivial test image using a freshly-onboarded project's CI identity, confirm the on-demand CVE scan can be queried via the API and returns a result, then remove the test image and the CI identity's `accessControl` entry).
- **Runs on:** GitLab CI (SVC-13 self-hosted runners); the smoke-test stage itself demonstrates the intended CI-push pattern every *other* project's pipeline will use — pull the project's own CI credential from OpenBao, `docker login`, build, push, then query `/v2/<repo>/_cve` (or the equivalent CVE API endpoint) for the result before promoting the image.
- **State backend:** GitLab-managed Terraform state, per F1.

## 11. Secrets & credentials

- **What secrets exist:** per-project CI identity credentials (htpasswd username/password pairs, one per project), the ZITADEL OIDC client secret (FR-8), and — if LDAP is later enabled — the LDAP bind credential.
- **Where generated:** CI identity passwords are generated once at project-onboarding time (random, high-entropy, generated by the Ansible role, hashed into the `htpasswd` file, and the plaintext written directly to OpenBao — never displayed in plaintext in a terminal); the ZITADEL OIDC client secret is generated during SVC-06 setup and referenced here, not regenerated.
- **Rotation:** CI identity credentials are rotated by re-running the `zot_project_onboard` role with a `rotate=true` flag, which generates a new password, updates the `htpasswd` entry, and overwrites the OpenBao secret; a documented rotation cadence of 90 days (matching the previous Harbor robot-account default) is recommended even though Zot has no native token-expiry mechanism of its own to enforce this automatically — unlike Harbor's robot-account duration setting, rotation here is a process, not a platform-enforced control (flagged in §15).
- **How they reach the service:** CI pipelines fetch their project's CI identity credential from OpenBao using the JWT/OIDC trust described in SVC-07 §11 — **no Zot credential is ever stored as a static GitLab CI/CD variable.**

## 12. Security & hardening baseline

- TLS everywhere: Zot's `config.json` `http.tls` block is configured with a certificate issued by SVC-08 (internal CA) or Let's Encrypt (if publicly reachable), and is additionally proxied through Traefik (SVC-09) for hostname-based routing consistent with every other shared service; anonymous/unauthenticated pull access is disabled platform-wide via `accessControl`.
- Least-privilege: CI identities are scoped to exactly one repository path prefix each and to the minimum permission set (create+update+read for CI push/pull, read-only for deployment-time consumers) — never given the global `adminPolicy` scope described in the [zot authn/authz docs](https://zotregistry.dev/v2.1.11/articles/authn-authz/).
- Firewall/VLAN rules: only port 443 (and Zot's configured HTTP port, redirect-only, if enabled) is reachable from CI runners and Traefik; there is no separate database or Redis container to additionally firewall, unlike Harbor.
- CVE scanning: this is Zot's `search`/CVE extension's core function (FR-2) — additionally, the pinned Zot image tag itself is periodically reviewed for upstream CVE advisories as part of a scheduled maintenance task, same discipline as was applied to Harbor's own component images.
- Auth via SVC-06: human user login to the Zot UI is delegated to ZITADEL via the `openid` provider mechanism (FR-8); CI identities (machine credentials) are Zot-native `htpasswd`/LDAP entries and not part of the SSO flow, consistent with their non-interactive purpose — though Zot's OIDC *bearer-token workload identity* feature (validating short-lived OIDC ID tokens from e.g. GitLab CI's own OIDC provider, with no static credential at all) is noted in §15 as a stronger future alternative to static htpasswd CI credentials.

## 13. Observability hooks

- Zot exposes a Prometheus metrics endpoint via the `extensions.metrics` config block, per the [zot monitoring docs](https://zotregistry.dev/v2.1.11/articles/monitoring/), scraped by SVC-17.
- Zot's request/audit logging is shipped to SVC-18/Loki via Promtail, same pattern as every other shared service.
- Key alerts to define in Alertmanager: `zot_down` (API/UI unreachable), `zot_scan_failure_rate_high` (embedded Trivy scans failing to complete, potentially masking unscanned images from being caught by the CI severity gate in FR-4), `zot_storage_disk_near_full` (a direct disk-usage alert compensating for the lack of a native byte quota — see §2/§15), `zot_repo_count_near_maxrepos` (approaching the configured `storage.maxRepos` cap), `zot_critical_vulnerability_found` (a push introduces a new Critical-severity finding, routed as a higher-priority notification than routine scan completion).

## 14. Acceptance criteria

- [ ] Zot LXC/VM provisioned via Terraform with a separate storage-root volume; UI/API reachable via Traefik over HTTPS.
- [ ] A test project's `accessControl` entry and CI identity are created end-to-end by the `zot_project_onboard` Ansible role.
- [ ] A test image pushed via the CI identity can be queried via the CVE API and returns a scan result.
- [ ] A Docker Hub on-demand sync (pull-through cache) is functional: pulling a common base image (e.g. `alpine`) through Zot succeeds and is visibly cached on second pull.
- [ ] Tag retention rule applied to the test repository successfully removes an old tag according to policy on a scheduled run.
- [ ] CI identity credentials are never observed in plaintext in Git history, CI job logs, or Terraform state.
- [ ] Prometheus scrape target live; Alertmanager rule for `zot_down` fires correctly in a manual failure-injection test.
- [ ] Nightly backup of Zot's storage-root directory present in Proxmox Backup Server, with at least one successful restore test performed.

## 15. Open questions / assumptions

- **Byte-level storage quota gap:** Zot has no native per-project GB quota (only the global `storage.maxRepos` repository-count cap, FR-9) — this is a real regression versus Harbor's per-project quota UI. Mitigated for now by tag retention (FR-6) plus disk-usage alerting (§13), but flagged for a human decision on whether a custom quota-enforcement script (checking `du` on each repository path prefix on a schedule) is worth building if any single project's image sprawl becomes a recurring problem.
- **CI-credential rotation is process, not platform-enforced:** unlike Harbor's robot-account expiry setting, Zot's htpasswd/LDAP CI identities do not expire on their own — the 90-day rotation cadence (§11) depends entirely on the Ansible role being re-run on schedule. Worth revisiting once/if Zot's **OIDC bearer-token workload identity** feature (secret-less authentication using short-lived OIDC ID tokens, e.g. from GitLab CI's own built-in OIDC provider) is evaluated as a stronger replacement — noted here as a promising future hardening step, not yet scoped for the initial deployment.
- **Config-reload operational model:** onboarding a new project requires the Ansible role to reload/restart the Zot service after templating a new `accessControl` entry (§4/§9) rather than calling a live API the way Harbor's project-creation endpoint works. Whether this causes any observable disruption to in-flight pulls/pushes from other projects during the reload needs a validation pass during the platform-foundation rollout, since SVC-13's CI runners will be actively using the registry while other projects are still being onboarded.
- Whether Zot's `openid` OIDC integration (FR-8) can co-exist cleanly with the small number of platform-admin bootstrap accounts needed before ZITADEL itself is fully online needs the same validation pass previously scoped for Harbor, since SVC-06 and SVC-14 are both Tier 1 services provisioned close together.
- Cosign-based image signing remains a desirable future hardening step (per general registry best-practice guidance, unchanged from the Harbor-era assessment) but is explicitly deferred from this initial deployment's scope.
