# SVC-06 — Identity Provider / SSO — Requirements

## 1. Overview

SVC-06 provides the platform's single sign-on and identity backbone: one place where humans (developers, operators) and, where relevant, service accounts authenticate, and from which every other UI/API on the platform (Grafana, Harbor, GitLab, project web apps, admin dashboards) delegates authentication via OIDC/SAML rather than maintaining its own local user database. Every project type in the catalog benefits indirectly — an embedded/drone dashboard, an ML experiment-tracking UI, or a generic backend's admin panel all authenticate the same way, with the same credentials and MFA policy, rather than each project reinventing login.

## 2. Scope

**In scope:**
- ZITADEL as the default IdP, supporting both OIDC and SAML.
- Keycloak as the documented fallback for maximum protocol/IdP-broker breadth.
- Tenancy model: one shared ZITADEL instance with a separate ZITADEL "project"/organization per platform project for realm-like isolation.
- OIDC integration patterns for Grafana, Harbor, GitLab, and generic project web apps.
- Baseline MFA and password policy expectations.

**Out of scope:**
- Directory synchronization with an external HR/identity source (no such source exists yet in this homelab/office context).
- Fine-grained per-application RBAC policy design beyond basic role/group mapping (each consuming service's own requirements doc covers its specific authorization model).
- Public user self-registration flows (this IdP serves internal operators/developers and project-facing tools, not an external customer base).

## 3. Technology selection

- **Default: ZITADEL**. Origin: 🇪🇺 **CAOS Ltd, St. Gallen, Switzerland** ([ZITADEL — CB Insights](https://www.cbinsights.com/company/zitadel)). Chosen as the catalog default for its modern OIDC-first design, native multi-tenancy primitives (organizations/projects, directly useful for the per-project isolation model in §4), and self-hostability via a single Docker Compose stack backed by PostgreSQL.
- **Alternative (max protocol/IdP-broker breadth): Keycloak**. Origin: 🚩 originated at **Red Hat, Raleigh, North Carolina, US** ([Keycloak — Wikipedia](https://en.wikipedia.org/wiki/Keycloak)) — flagged as non-European per the catalog's sourcing principle. Kept as the documented fallback specifically for scenarios ZITADEL does not yet cover as well: broader out-of-the-box protocol/IdP-broker support (e.g. brokering against a wide range of legacy SAML/LDAP identity sources, or very specific Keycloak-only extension points some third-party software expects). Not deployed by default.
- ZITADEL Docker image: `ghcr.io/zitadel/zitadel`, pinned tag (e.g. `v4.x.y` — pin a specific release, do not track `latest`). Default ports per the official Docker Compose reference: **8080** (HTTP — web console, login UI, OIDC/SAML endpoints) and **8081** (gRPC API), with PostgreSQL as ZITADEL's own backing database ([ZITADEL Docs — Set up ZITADEL with Docker Compose](https://zitadel.com/docs/self-hosting/deploy/compose); [zitadel/zitadel docker-compose.yaml](https://github.com/zitadel/zitadel/blob/main/docs/docs/self-hosting/deploy/docker-compose.yaml)). ZITADEL requires PostgreSQL versions 14–18 ([ZITADEL Docs — Requirements](https://zitadel.com/docs/self-hosting/manage/requirements)).

## 4. Multi-tenancy model

**One shared ZITADEL instance** (per catalog §1 and §3 row SVC-06: "Tenancy: Shared") for the whole platform, with **realm-like isolation implemented via ZITADEL's own organization/project resource model**:

- Each platform project gets its own **ZITADEL organization** (or, for lighter-weight isolation needs, a **ZITADEL project** within a shared organization — organizations are used by default since they give the cleanest separation of users, login policies, and branding per project). Naming: ZITADEL organization name `proj-<slug>`.
- Each application/service belonging to that project registers as one or more **OIDC clients** (ZITADEL "applications") inside that project's organization — e.g. `proj-dronefleet-webapp`, `proj-mlvideo-grafana` (for a project-specific Grafana instance, if applicable).
- Shared platform services that are themselves cross-project (Grafana, Harbor, GitLab) register their OIDC clients under a dedicated `platform` organization rather than any individual project's organization, since they are consumed by users across all projects.
- Cross-organization login is supported (a platform operator's single ZITADEL human user account can belong to multiple organizations/projects with different roles in each), avoiding the need for separate credentials per project while still keeping each project's application registrations and role definitions logically separated.

## 5. Functional requirements

- **FR-1**: The service MUST expose OIDC discovery (`/.well-known/openid-configuration`) and standard OIDC flows (Authorization Code + PKCE at minimum) for every registered application.
- **FR-2**: The service MUST support SAML 2.0 as a fallback protocol for any legacy or third-party tool that cannot do OIDC.
- **FR-3**: The platform team MUST be able to provision a new project's ZITADEL organization, default roles (e.g. `admin`, `member`, `viewer`), and initial OIDC application registrations via an idempotent, scriptable process (ZITADEL's own management API), not manual console clicking, so it can be driven from Ansible/CI.
- **FR-4**: The service MUST support enforced MFA (TOTP at minimum) for all platform-operator accounts, configurable per-organization login policy.
- **FR-5**: Grafana, Harbor, GitLab, and generic project web apps MUST each be able to complete an OIDC login against their respective registered ZITADEL application, mapping ZITADEL roles/groups to each application's own role model (e.g. ZITADEL role `admin` → Grafana `Admin` org role).
- **FR-6**: The service MUST expose a management/admin API usable for automated user and application provisioning (used by the Ansible role in §9), separate from the interactive login UI.
- **FR-7**: Service account (machine-to-machine) authentication MUST be supported via ZITADEL's service user + JWT/client-credentials flow for any automated pipeline needing to call another service's API on a project's behalf.

## 6. Non-functional requirements

- **Availability**: single-node ZITADEL instance is acceptable at this scale (best-effort availability, matching the rest of the shared platform services); since ZITADEL is a hard dependency for logging into nearly every other UI, an outage window should be short — target restore-from-backup RTO under 30 minutes, backed by ZITADEL's own PostgreSQL database backup (§11/§14 of SVC-01's backup pattern reused here).
- **Performance/sizing**: ZITADEL itself is lightweight — official guidance states a **test/evaluation deployment needs as little as 1 CPU and 512 MB memory** ([ZITADEL Docs — Deploy overview](https://zitadel.com/docs/self-hosting/deploy/overview)), while a small **production** deployment for a homelab/office scale (a handful of projects, a handful of human operators) is comfortably served by **2–4 vCPU and 1–2 GB RAM** for the ZITADEL container itself, consistent with community guidance that containers need 512 MB–1 GB RAM minimum with 2–4 vCPUs recommended to absorb password-hashing CPU spikes ([ZITADEL self-hosting overhead & resource guide](https://help.zitadel.com/self-hosting-zitadel-overhead-resources)). Its backing PostgreSQL database (dedicated instance, per SVC-01's tenancy model, not shared with unrelated project data) should be sized at the SVC-01 "small project" tier (1 vCPU, 1–2 GB RAM, 10–20 GB disk) as a starting point.
- **Backup/DR**: nightly `pg_dump` of ZITADEL's dedicated PostgreSQL database (same mechanism as SVC-01 §6/§11), plus Proxmox Backup Server volume-level backup of the ZITADEL LXC.
- **Data retention**: user/org/application metadata retained indefinitely (it is configuration, not transient data); audit/login-event logs retained per Loki's standard retention policy (aligned with SVC-18 defaults).

## 7. Infrastructure architecture

- **Compute unit**: LXC, per decision F2 — no GPU/kernel isolation requirement.
- **Minimum resource spec**: 2 vCPU, 1–2 GB RAM, 10 GB disk for the ZITADEL application container itself; its dedicated PostgreSQL backing store (separate LXC or a Compose sidecar within the same stack — a separate LXC is preferred for consistency with the SVC-01 dedicated-instance pattern and easier independent backup/scaling) at 1 vCPU / 1–2 GB RAM / 10–20 GB disk.
- **Network placement**: placed on the shared-services VLAN with a static DHCP reservation (its IP/hostname is referenced by every other service's OIDC client configuration, so it must be stable); exposed externally only through Traefik with TLS, never a raw port opened to the LAN/WAN.
- **Storage**: `local-zfs` for both the ZITADEL application LXC and its backing PostgreSQL LXC's data volumes.

## 8. Terraform scope

- **Module inputs**: `cpu_cores`, `memory_mb`, `disk_gb`, `vlan_tag`, `zitadel_version` (image tag), `db_cpu_cores`, `db_memory_mb`, `db_disk_gb` (for the backing Postgres instance, provisioned via the SVC-01 module reused here with `db_engine = "postgres"`).
- **Resources created**: one `proxmox_virtual_environment_container` (LXC) for ZITADEL itself via `bpg/proxmox`, plus a reference to (or direct instantiation of) an SVC-01 dedicated PostgreSQL instance for `proj-shared-zitadel`; a Traefik dynamic-config resource (or Docker label, applied via Ansible) exposing ZITADEL's HTTP port through the shared reverse proxy with TLS.
- **Outputs**: `zitadel_issuer_url` (e.g. `https://auth.<domain>`), `zitadel_host`, `zitadel_grpc_port` (8081) — consumed by every other service's Terraform/Ansible when templating that service's OIDC client configuration (issuer URL, client ID/secret retrieved separately from OpenBao per §11).

## 9. Ansible scope

- **Roles**: `zitadel-server` (templates `compose.yaml` for the `zitadel` + backing considerations, `ZITADEL_DATABASE_POSTGRES_*` environment variables, `ZITADEL_EXTERNALDOMAIN`/`ZITADEL_EXTERNALPORT`/`ZITADEL_TLS_ENABLED` settings matching the public Traefik-fronted endpoint exactly, since ZITADEL requires its configured external domain/port to match the actual public endpoint precisely ([ZITADEL Docs — Set up ZITADEL with Docker Compose](https://zitadel.com/docs/self-hosting/deploy/compose))), `zitadel-org-provision` (uses the ZITADEL management API — via `ansible.builtin.uri` calls or a small Python helper — to idempotently create a new project's organization, default roles, and OIDC application registrations).
- **Idempotency**: `zitadel-org-provision` checks for an existing organization by name before creating one (ZITADEL's API returns a conflict/existing-resource response that the role treats as a no-op success); Compose reconciliation via `community.docker.docker_compose_v2`.
- **Config files templated**: `compose.yaml` (ZITADEL image tag, environment variables for DB connection and external domain/TLS settings), an initial `machine-user` service-account key file used only for the first automated org-provisioning API calls (itself sourced from OpenBao, not left on disk after use).
- **Secrets injected from SVC-07**: ZITADEL's own database password (same handoff pattern as SVC-01), the ZITADEL master encryption key (`ZITADEL_MASTERKEY`, a 32-byte key required at first init and needed for every subsequent start), and every OIDC client's `client_id`/`client_secret` pair (generated at application-registration time and written to OpenBao under `secret/proj-<slug>/zitadel/oidc/<app-name>`), are all stored in OpenBao — never in a Compose `.env` committed to a repo.

## 10. CI/CD pipeline

Stages: lint → plan (`terraform plan` against GitLab-managed state per platform-foundation decision F1) → manual approve (ZITADEL is a hard dependency for platform-wide login, so any infra change is manually gated) → apply → smoke test (an automated check that `/.well-known/openid-configuration` resolves and returns a valid discovery document, plus a scripted OIDC client-credentials token request using a test service account, executed from the CI runner). New-project organization onboarding runs as a separate, lighter-weight pipeline (auto-applied, since it only creates a new organization/application within ZITADEL's API and carries low blast radius). Runs on GitLab CI; state name `shared-svc06-zitadel`.

## 11. Secrets & credentials

- **Secrets that exist**: the ZITADEL master encryption key (`ZITADEL_MASTERKEY`), the ZITADEL backing-database password, the initial admin bootstrap credential, and per-application OIDC `client_id`/`client_secret` pairs.
- **Generation**: the master encryption key is generated once at first deployment (32 random bytes, base64-encoded) and must never change afterward without a full data-re-encryption migration — treated as a maximally sensitive, rarely-rotated secret. Per-application client secrets are generated by ZITADEL itself at application-registration time and immediately captured by the provisioning Ansible role into OpenBao.
- **Rotation**: OIDC client secrets rotated on a 180-day default cadence (or immediately on suspected compromise) by re-registering a new secret for the same application and cutting over consuming services' OpenBao-sourced config; the master encryption key is not rotated under normal operations given its non-rotatable-without-migration nature — its protection instead relies on OpenBao's own strict access control.
- **Reaching consuming services**: Grafana, Harbor, GitLab, and project web apps each retrieve their own OIDC `client_id`/`client_secret` from OpenBao via their own OpenBao Agent at container start (same pattern as SVC-01/SVC-02/SVC-03), templating it into their respective OIDC configuration blocks (e.g. Grafana's `[auth.generic_oauth]` INI section, GitLab's `omniauth_providers` config, Harbor's OIDC settings, or a project web app's standard OIDC client library config).

## 12. Security & hardening baseline

- TLS everywhere via Traefik (SVC-09): ZITADEL's `ZITADEL_EXTERNALSECURE`/TLS settings are configured to match its actual public HTTPS endpoint exactly, since mismatches break redirect URIs and token issuance.
- Least-privilege: the machine-user service account used for automated org/application provisioning is scoped to only the ZITADEL management API operations it needs (org and project management), not full instance-admin rights.
- Firewall/VLAN rules: ZITADEL's LXC and its backing PostgreSQL instance sit on the shared-services VLAN; the PostgreSQL port is firewalled to accept connections only from the ZITADEL application container's IP.
- CVE scanning: the `ghcr.io/zitadel/zitadel` image tag is scanned by Harbor/Trivy (SVC-14) before promotion to the trusted registry namespace.
- Auth via SVC-06 (self-referential): ZITADEL's own console UI is protected by its own native login + MFA policy (it is the identity root of trust, so it cannot delegate to itself) — MFA enforcement here is the single most important hardening control on the whole platform, since a compromised ZITADEL admin account compromises every downstream OIDC-integrated service.

## 13. Observability hooks

- ZITADEL exposes a Prometheus-compatible metrics endpoint (enabled via its telemetry/observability configuration) scraped by Prometheus (SVC-17).
- Application and access logs shipped via Promtail to Loki (SVC-18), tagged `service=zitadel`; failed-login and MFA-challenge events are of particular interest for security monitoring.
- Key alerts: ZITADEL instance down/unreachable (blocks login platform-wide — high-priority alert), backing PostgreSQL connection failures, unusual spike in failed authentication attempts (possible credential-stuffing attempt), OIDC discovery endpoint returning non-200.

## 14. Acceptance criteria

- [ ] ZITADEL instance provisioned via Terraform+Ansible, backed by its own dedicated PostgreSQL instance, reachable over HTTPS via Traefik.
- [ ] At least one project organization created with a working OIDC application registration, validated with a full Authorization Code + PKCE login flow.
- [ ] Grafana, Harbor, and GitLab each successfully configured to log in via ZITADEL OIDC, with role mapping verified for at least one non-admin role.
- [ ] MFA (TOTP) enforced and verified for at least one platform-operator account.
- [ ] All OIDC client secrets and the master encryption key confirmed retrievable only via OpenBao — no plaintext secret found in any repository, CI log, or Compose file.
- [ ] Prometheus metrics and Loki logs visible for the ZITADEL instance within 5 minutes of deployment.
- [ ] Keycloak fallback path documented (even if not deployed) with a clear trigger condition for when it would be adopted instead.

## 15. Open questions / assumptions

- Assumes one ZITADEL organization per project is the right isolation granularity; if the number of projects grows very large, a lighter-weight "ZITADEL project" (within a shared organization) model could be revisited to reduce per-project administrative overhead — left as a future decision.
- Assumes the ZITADEL master encryption key's backup (via OpenBao) is sufficient DR coverage; a full instance-loss recovery drill should be run at least once to validate the actual recovery procedure before relying on it in a real incident.
- Assumes Keycloak is never deployed unless a specific, currently-unforeseen protocol/broker requirement arises; no current project has such a requirement.
- The exact role-mapping convention between ZITADEL roles and each consuming application's native roles (Grafana org roles, Harbor project roles, GitLab access levels) is sketched at a high level in §5/§11 but should be finalized per-application as each integration is built.
