# SVC-07 — Secrets Manager Requirements

## 1. Overview

SVC-07 provides the platform's central secrets manager: a durable, audited store for static credentials, dynamically-issued short-lived database/object-storage credentials, TLS material handoff, and encryption-as-a-service. The chosen product is **OpenBao**, an identity-based secrets and encryption management system governed openly under the Linux Foundation ([openbao.org](https://openbao.org/docs/install/)). Every other service in this catalog that needs a credential — root passwords, API keys, TLS private keys copied from SVC-08, CI push tokens for SVC-14 — reads it from OpenBao rather than from a CI variable, a `.env` file, or a Terraform state blob. It is a Tier 1 / platform-foundation service and is a hard dependency for the rollout of every other service (per catalog §4, it is provisioned second, immediately after Terraform/Ansible bootstrap). All project types — embedded/drone, ML-video, ML-text, generic backend — depend on it indirectly through SVC-01/SVC-02/SVC-03 dynamic credentials and through GitLab CI's own secret lookups.

## 2. Scope

**In scope:**
- Deploying one shared OpenBao instance (single-node Raft/integrated-storage, expandable to 3-node HA later) as the platform secrets manager.
- Static KV secret storage per project under isolated paths.
- Dynamic secrets engine for PostgreSQL (SVC-01) and Garage/MinIO S3-compatible credentials (SVC-03).
- Auto-unseal via a dedicated seal-only OpenBao Transit instance (avoids manual Shamir unseal after every restart/reboot).
- GitLab CI → OpenBao authentication via JWT/OIDC (`jwt` auth method), eliminating long-lived root/admin tokens from CI/CD variables.
- Policy design per project/tenant.
- Backup of the Raft storage snapshot to SVC-32 (Proxmox Backup Server).

**Out of scope:**
- HSM-backed seals (no HSM present in this homelab/office cluster; PKCS#11 not applicable).
- Multi-region/multi-datacenter replication (Enterprise-only feature in upstream Vault; OpenBao Community has no equivalent yet).
- Acting as the CA itself — PKI issuance for internal TLS is SVC-08 (EJBCA); OpenBao's own PKI secrets engine is not used to avoid duplicating that function.
- End-user password vaulting / personal password manager use cases.

## 3. Technology selection

**Chosen:** OpenBao ([openbao/openbao](https://hub.docker.com/r/openbao/openbao) Docker image, Alpine-based, ~77 MB, also published to `ghcr.io/openbao/openbao` and `quay.io/openbao/openbao`) — [OpenBao installation docs](https://openbao.org/docs/install/). License: MPL-2.0. Origin/governance: a fork of HashiCorp Vault created after Vault's 2023 relicensing to BSL, now governed openly as part of **Linux Foundation Edge** — a neutral, multi-vendor nonprofit foundation, not a single national corporate owner.

**European-sourcing flag (per catalog §3.5 and §1 guiding principles):** no clearly European alternative of comparable maturity was found for a Vault-class secrets manager with dynamic secrets, leasing/revocation, and a mature plugin ecosystem. This is one of the catalog's explicitly flagged non-European exceptions. OpenBao's Linux Foundation governance was selected as the closest available middle ground: it is not headquartered in, or controlled by, any single country's corporate entity, and its steering committee includes IBM/Red Hat, Cisco, and other international contributors rather than one dominant US vendor. This mirrors the treatment given to Prometheus and Mosquitto elsewhere in the catalog (community/foundation-governed rather than corporate-HQ-European).

**Alternative considered:** HashiCorp Vault (the pre-fork upstream) was rejected as the default because HashiCorp Inc. is US-based (San Francisco) and, more importantly, Vault's licensing moved from MPL-2.0 to the Business Source License (BSL) in 2023, which restricts building competing hosted offerings and is not OSI-approved open source — inconsistent with the catalog's "self-hosted, open source first" principle. OpenBao preserves the pre-BSL MPL-2.0 codebase and continues independent development. No other secrets-manager product (e.g., Infisical, Doppler) was found at comparable dynamic-secrets/leasing maturity from a European vendor.

## 4. Multi-tenancy model

**Pattern:** Shared platform instance (per catalog §1) — one OpenBao cluster serves all projects.

**Isolation implementation:**
- KV v2 secrets engine mounted per project at `secret/data/<project-slug>/*` (e.g. `secret/data/acme-drone/*`, `secret/data/ml-vision-01/*`).
- A dedicated OpenBao **policy** per project (`policy-<project-slug>`) grants `create`, `read`, `update`, `delete`, `list` only on `secret/data/<project-slug>/*` and `secret/metadata/<project-slug>/*` — no cross-project read access.
- Dynamic database/object-storage roles are namespaced identically: `database/roles/<project-slug>-<db-name>-rw`, `aws/roles/<project-slug>-<bucket>-rw` (the AWS secrets engine's generic S3-compatible mode is reused for Garage/MinIO since OpenBao has no Garage-native plugin).
- A separate `platform-admin` policy (break-glass, MFA-gated, used only by human operators) has broader access for onboarding new projects and rotating root database credentials.
- Namespacing (a Vault Enterprise feature) is not available in OpenBao Community; the path-prefix + policy convention above is the substitute and must be enforced consistently by the Ansible role that provisions each new project (§9).

## 5. Functional requirements

- **FR-1:** OpenBao MUST expose a KV v2 secrets engine per project path convention above, provisioned automatically when a new project is onboarded via Terraform/Ansible.
- **FR-2:** OpenBao MUST issue dynamic, time-limited PostgreSQL credentials for SVC-01 database instances via the `database` secrets engine, using the `postgresql-database-plugin` ([OpenBao database secrets docs](https://openbao.org/docs/secrets/databases/)).
- **FR-3:** OpenBao MUST issue dynamic, time-limited access-key/secret-key pairs for SVC-03 (Garage, S3-compatible API) via the `aws` secrets engine configured against Garage's S3 endpoint.
- **FR-4:** OpenBao MUST support the `jwt`/OIDC auth method so that GitLab CI jobs authenticate using the GitLab-issued `CI_JOB_JWT`/ID token, with role bindings scoped by project path and, optionally, branch/environment claims.
- **FR-5:** OpenBao MUST auto-unseal on every restart with no human unseal-key entry required, using the Transit auto-unseal pattern against a dedicated seal-only OpenBao instance.
- **FR-6:** OpenBao MUST expose its HTTP API and UI on port 8200 (TLS) behind Traefik (SVC-09), never directly on a routable interface without TLS.
- **FR-7:** OpenBao MUST log every secret read/write/auth event to its audit device (file audit backend, shipped to SVC-18/Loki).
- **FR-8:** OpenBao MUST support root-credential rotation for the PostgreSQL/Garage "root" accounts it uses to mint dynamic secrets (`bao write -force database/rotate-root/<name>`).
- **FR-9:** Recovery/root tokens generated at initialization MUST be captured once, split, and stored outside OpenBao itself (e.g., printed once to an offline operator note, not left in shell history or CI logs).

## 6. Non-functional requirements

- **Availability target:** 99% during business hours is sufficient for a homelab/office cluster; brief downtime for OpenBao blocks new dynamic-secret issuance but does NOT invalidate already-issued leases until they expire, so downstream services keep working short-term.
- **Performance/sizing:** OpenBao itself is lightweight (the official image is ~77 MB and idles at well under 200 MB RSS); sizing is dominated by Raft storage I/O and TLS handshake volume, not CPU.
- **Backup/DR:** nightly Raft storage snapshot (`bao operator raft snapshot save`) to a file consumed by Proxmox Backup Server (SVC-32); snapshot restore is the documented DR path if the LXC is lost outright.
- **Data retention:** audit logs retained 90 days in Loki; KV secret version history capped at 10 versions per key (`max_versions` on the KV v2 mount) to bound storage growth.

## 7. Infrastructure architecture

- **Compute unit:** LXC container (per F2 — lightweight Linux service, no GPU/kernel isolation need). A second, minimal LXC hosts the seal-only Transit unsealer instance, kept intentionally separate from the primary so its failure domain does not double as an attacker's single point of compromise for both storage and unseal key.
- **Minimum resource spec (primary OpenBao LXC):** 2 vCPU, 2 GB RAM, 20 GB disk (local-zfs) — comfortably above the idle footprint, with headroom for Raft log churn and audit log buffering in a small cluster with a few dozen projects.
- **Minimum resource spec (seal-only Transit LXC):** 1 vCPU, 512 MB RAM, 8 GB disk — it holds no application secrets, only the Transit unseal key.
- **Network placement:** dedicated internal VLAN (e.g. VLAN 20 "platform-core") shared with SVC-06/SVC-08/SVC-14; static DHCP reservation for a stable internal IP (e.g. `10.20.0.7`); no public exposure — reachable only via Traefik on the internal network or via NetBird (SVC-11) for remote administrators.
- **Storage:** local-zfs for the Raft integrated-storage data directory (`/openbao/data`), snapshotted nightly.

## 8. Terraform scope

- **Module inputs:** `vmid`, `hostname` (`svc07-openbao`, `svc07-openbao-unsealer`), `vlan_tag`, `static_ip`, `cpu_cores`, `memory_mb`, `disk_gb`, `proxmox_node`.
- **Resources created:** two `proxmox_virtual_environment_container` (bpg/proxmox provider) LXC resources; associated `proxmox_virtual_environment_network` bindings; a GitLab-managed Terraform state entry keyed `svc-07-secrets-manager` (per F1).
- **Outputs:** `openbao_internal_ip`, `openbao_api_port` (8200), `openbao_unsealer_internal_ip` — consumed by the Ansible dynamic inventory (F4) and referenced by every other service's Ansible role that needs to bootstrap its OpenBao client config (agent address, CA bundle path).

## 9. Ansible scope

- **Roles:** `openbao_install` (installs the `openbao/openbao` binary/Docker Compose stack), `openbao_init_unseal` (one-time init + Transit seal wiring, idempotent — checks `bao status` before attempting init), `openbao_project_onboard` (creates the KV mount, policy, and JWT role for a new project given a `project_slug` variable).
- **Idempotency:** all `bao write`/`bao secrets enable` calls are wrapped in check-then-write Ansible tasks (`bao secrets list -format=json` grep before enabling a mount) so re-running the playbook against an already-configured instance is a no-op.
- **Config files templated:** `config.hcl` (listener, storage "raft", seal "transit" stanza), Docker Compose file pinning `openbao/openbao:2.4` (pin to a specific minor to control upgrades rather than floating `latest`).
- **Secrets injected from SVC-07 itself:** N/A for this service (it is the origin); however, the *unsealer* token used in the `seal "transit"` stanza is generated once during bootstrap and stored in the Ansible Vault-encrypted bootstrap secrets file referenced by F5, then rotated into OpenBao-managed storage once the primary instance is live.

## 10. CI/CD pipeline

- **Stages:** `lint` (terraform fmt/validate, ansible-lint) → `plan` (terraform plan against the GitLab-managed state) → manual approval gate → `apply` → `smoke test` (`bao status` returns `sealed: false`, then a scripted round-trip: write a throwaway KV secret, read it back, delete it).
- **Runs on:** GitLab CI (self-hosted runners, SVC-13), using the `hashicorp/terraform:1.x` image for plan/apply stages and a thin `curlimages/curl` image for the smoke test against the OpenBao API.
- **State backend:** GitLab-managed Terraform state (native HTTP backend), per platform foundation decision F1.

## 11. Secrets & credentials

- **What exists:** per-project KV secrets (arbitrary key/value), dynamic PostgreSQL leases, dynamic Garage/S3 leases, the OpenBao root token (used only once at init, then revoked/rotated to policy-scoped tokens), recovery keys (from Transit auto-unseal init), the Transit unsealer's own token.
- **Where generated:** the root token and recovery keys are generated by `bao operator init` at first boot; dynamic leases are generated on-demand per `bao read database/creds/<role>` or `bao read aws/creds/<role>` call.
- **Rotation:** dynamic leases default to a 1-hour TTL / 24-hour max TTL (`default_ttl=1h`, `max_ttl=24h` per [OpenBao database secrets engine docs](https://openbao.org/docs/secrets/databases/)); root DB/S3 credentials used by OpenBao itself are rotated via `bao write -force database/rotate-root/<name>` on a scheduled Ansible run (monthly).
- **How they reach the service:** N/A — this document describes the origin, not a consumer; every *other* service's requirements doc describes the OpenBao Agent/API-call pattern it uses to fetch its own secrets from here.
- **GitLab CI authentication (JWT/OIDC):** GitLab CI jobs present their `ID_TOKEN` (JWT signed by GitLab, configured with `aud: https://openbao.internal`) to OpenBao's `jwt` auth method. OpenBao is configured with GitLab's OIDC discovery URL as the JWKS source, plus a `bao write auth/jwt/role/<project-slug>-ci` role that binds the `project_path` and `ref_type`/`ref` claims to the project's own policy. This means **no long-lived OpenBao token is ever stored as a GitLab CI/CD variable** — each pipeline run gets a short-lived OpenBao token scoped to exactly that project's policy, valid only for the job's lifetime.

## 12. Security & hardening baseline

- TLS everywhere: OpenBao's listener is configured with `tls_disable = false`, certificate issued by SVC-08 (EJBCA internal CA); Traefik (SVC-09) additionally terminates public-facing TLS for the UI/API route and proxies to OpenBao's internal HTTPS listener (TLS is not double-terminated to plaintext internally).
- Least-privilege: no service or human uses the root token for day-to-day operations; root token is generated once, used to bootstrap the `platform-admin` policy and auth methods, then revoked (`bao token revoke -self`).
- Firewall/VLAN rules: only Traefik, the Ansible control node, and the seal-only Transit instance may reach port 8200/8201; all other traffic blocked at the VLAN firewall.
- Secret material is protected from plaintext swap by two complementary controls (ADR-0006): the per-container swap cap `--memory-swappiness=0` (`memory.swap.max=0` / Terraform `swap = 0`) recommended by the [official OpenBao install docs](https://openbao.org/docs/install/) is retained as per-guest defence-in-depth, AND the host-layer at-rest control is **encrypted host swap** (ideally encrypted disks) — an operator prerequisite, not an in-container tunable. The former in-container host-global `vm.swappiness=0` write is retired (unrunnable on unprivileged LXC, cross-tenant, only a heuristic); `mlock`/`IPC_LOCK` is not used (Raft-only, per OpenBao upstream).
- CVE scanning: the `openbao/openbao` image is pulled through Harbor (SVC-14) as a pull-through cache, which applies Trivy scanning on ingestion; image updates are gated on no new critical/high CVEs.
- Auth via SVC-06: the OpenBao **UI** login for human operators is bound to ZITADEL via the `oidc` auth method (separate from the `jwt` method used by CI), so human access is SSO-gated and MFA-enforced by ZITADEL policy.

## 13. Observability hooks

- OpenBao exposes a Prometheus-compatible telemetry endpoint (`telemetry` stanza with `prometheus_retention_time`) scraped by SVC-17 (Prometheus) at `/v1/sys/metrics?format=prometheus`.
- Audit logs (file audit device) are shipped via Promtail to SVC-18 (Loki); log format is structured JSON, one event per secret operation.
- Key alerts to define in Alertmanager: `openbao_sealed == 1` (instance unexpectedly sealed), `openbao_unsealer_unreachable` (Transit seal dependency down), `openbao_lease_count_high` (possible leak of unrevoked dynamic leases), `openbao_audit_log_write_failure` (audit device blocking writes — OpenBao fails closed on audit failure by design, so this is a hard outage alert).

## 14. Acceptance criteria

- [ ] Primary OpenBao LXC and seal-only Transit LXC provisioned via Terraform, visible in Ansible dynamic inventory.
- [ ] OpenBao auto-unseals on container restart with zero manual intervention.
- [ ] KV v2 mount + policy + JWT role created end-to-end by the `openbao_project_onboard` Ansible role for a test project slug.
- [ ] A test GitLab CI pipeline authenticates via JWT/OIDC (no static token variable) and successfully reads a KV secret scoped to its own project only, and fails to read another project's path.
- [ ] Dynamic PostgreSQL credential issuance verified against a test SVC-01 instance; credential auto-expires and is revoked after TTL.
- [ ] Prometheus scrape target live; Alertmanager rule for `sealed == 1` fires correctly in a manual test (stop the unsealer, restart OpenBao).
- [ ] Nightly Raft snapshot present in Proxmox Backup Server and restore tested at least once.

## 15. Open questions / assumptions

- Assumes GitLab is reachable from OpenBao (or vice versa) to fetch the OIDC JWKS document — if GitLab is fully air-gapped, the JWKS must be mirrored/cached, which is not yet designed.
- Assumes a single OpenBao node is acceptable for now; multi-node Raft HA (3-node quorum) is deferred until more than one Proxmox host is available for anti-affinity — open question for when the cluster grows beyond one physical node.
- The AWS secrets engine's fitness for Garage's S3-compatible API is assumed based on protocol compatibility, not yet validated against Garage's actual IAM-equivalent surface — this needs a spike before SVC-03's own requirements doc is finalized.
- Whether to also route SVC-08's own CA private key material through OpenBao's `pki` secrets engine (instead of file-based storage on the EJBCA host) is left open for a human decision.
