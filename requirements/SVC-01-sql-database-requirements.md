# SVC-01 — Relational (SQL) Database — Requirements

## 1. Overview

SVC-01 provides the standard relational database engine for every project on the platform. It is the default persistence layer for embedded/drone device-fleet metadata, ML experiment metadata, application backends, and any service (e.g. SVC-06 ZITADEL, SVC-24 MLflow) that itself needs a relational store. Nearly every project type in the catalog (embedded/Raspberry Pi/NXP, drone hardware, ML-video, ML-text, generic backend) consumes SVC-01 directly or indirectly. Providing it as one well-defined, repeatable Terraform+Ansible module avoids each project re-inventing database provisioning, backup, and credential handoff from scratch.

## 2. Scope

**In scope:**
- PostgreSQL as the default relational engine, provisioned as a dedicated-per-project Docker Compose stack.
- MariaDB as the documented strict-EU-governance alternative (same provisioning pattern, swappable per project).
- Sizing tiers for small vs. medium projects.
- Backup via `pg_dump` plus Proxmox Backup Server (SVC-32) snapshotting of the underlying volume.
- Credential generation and handoff to consuming application containers via OpenBao (SVC-07).

**Out of scope:**
- Extension-specific tuning guidance for pgvector/PostGIS/Timescale workloads beyond confirming they are installable (each project's own requirements define index/query tuning).
- Multi-region or multi-primary replication (single-node-per-project with local backup is the accepted DR posture at this scale).
- NoSQL/document stores (separate future catalog entry, not SVC-01).

## 3. Technology selection

- **Default: PostgreSQL**, license PostgreSQL License (permissive, OSI-approved). Origin: 🌍 no single corporate owner — a global open community project with a strong European contributor base. Chosen as the catalog default specifically because a single engine covers vector search (`pgvector`), geospatial drone telemetry (`PostGIS`), and time-series data (`TimescaleDB` extension) without standing up three separate database technologies.
- **Strict-EU-governance alternative: MariaDB**. Origin: 🇪🇺 MariaDB Foundation, Helsinki, Finland (nonprofit) ([MariaDB Foundation — LinkedIn](https://www.linkedin.com/company/mariadb-foundation)). Not chosen as the default because it lacks first-party equivalents to pgvector/PostGIS/TimescaleDB with the same maturity, which would force a second database engine into the stack for vector/geospatial/time-series needs. It remains available as a per-project override where a project's governance requirements mandate an EU-foundation-governed engine over a globally-governed one, or where a team's existing MySQL/MariaDB familiarity outweighs the extension consolidation benefit.
- Official Docker image: **`postgres`** on Docker Hub, default port **5432** ([Docker Docs — PostgreSQL guide](https://docs.docker.com/guides/postgresql/)). Recommended pinned tag: `postgres:17` (latest stable major as of this writing; `postgres:18` also available) — pin to a specific minor via Ansible variable, never track `latest` in production ([Docker Hub — postgres official image](https://hub.docker.com/_/postgres)).
- MariaDB alternative image: `mariadb` official Docker Hub image, default port 3306.

## 4. Multi-tenancy model

**Dedicated instance per project** (per catalog §1 and §3 row SVC-01), not a shared multi-tenant cluster. Each project gets:
- Its own PostgreSQL Docker Compose stack, running on its own LXC (per platform foundation §7).
- Its own named Docker volume (`proj-<slug>-postgres-data`) bind-mounted to a dedicated ZFS dataset path, so the project's data is never co-located with another project's on the same logical volume.
- Its own set of database roles scoped to that instance only (no cross-project database users).

Rationale: dedicated-per-project isolates blast radius (a runaway query or a corrupted volume affects one project only), allows independent lifecycle (a project's database is torn down cleanly with the project, per catalog §1), and avoids the operational complexity of per-tenant schema/row-level-security policies that a shared cluster would require. Naming convention: LXC/VM hostname `proj-<slug>-postgres`, Compose service name `postgres`, database name defaults to `<slug>`, primary application role `<slug>_app`.

## 5. Functional requirements

- **FR-1**: The service MUST expose a PostgreSQL-wire-protocol endpoint on port 5432 (container-internal), reachable only from the project's own application containers and from the backup job — never exposed directly to the LAN/WAN via Traefik (Postgres is not an HTTP service; Traefik is not used for its wire protocol).
- **FR-2**: The Compose stack MUST support enabling `pgvector`, `PostGIS`, and `TimescaleDB` extensions on a per-project, per-database basis via `CREATE EXTENSION` statements templated by Ansible, driven by a project-level boolean flag per extension.
- **FR-3**: The service MUST create a dedicated, least-privilege application role (`<slug>_app`) with `CONNECT`/`CREATE`/`USAGE` scoped to its own database only — no application role may have `SUPERUSER`.
- **FR-4**: The service MUST support automated logical backups (`pg_dump`) on a configurable schedule (default: nightly) writing to a location backed up by Proxmox Backup Server.
- **FR-5**: The service MUST expose a Prometheus-scrapeable metrics endpoint via the `postgres_exporter` sidecar container in the same Compose stack.
- **FR-6**: Connection credentials MUST be retrievable by consuming application containers exclusively through OpenBao — no plaintext database password committed to any repository or baked into a Docker image.
- **FR-7**: The MariaDB alternative path MUST be selectable via the same Terraform module (a `db_engine = "postgres" | "mariadb"` input) so switching engines does not require a different provisioning pipeline.

## 6. Non-functional requirements

- **Availability**: single-node-per-project, best-effort availability appropriate to a homelab/office cluster; no automatic failover in scope. Target: restore-from-backup RTO under 30 minutes for a small project's database.
- **Performance/sizing** (small Proxmox homelab/office cluster):
  - **Small project tier** (typical embedded/drone metadata store, low write volume): 1 vCPU, 1–2 GB RAM, 10–20 GB disk (ZFS thin-provisioned). Sufficient for PostgreSQL's default `shared_buffers` tuning (~25% of container RAM) at this scale.
  - **Medium project tier** (ML-video/ML-text project with heavier metadata, embeddings via pgvector, or higher write throughput): 2–4 vCPU, 4–8 GB RAM, 40–100 GB disk, with `shared_buffers` and `work_mem` tuned upward accordingly.
  - LXC is the default compute unit (per platform foundation F2) since Postgres needs no GPU/kernel isolation; a VM is only used if a project specifically requires kernel-level isolation for compliance reasons.
- **Backup/DR**: nightly `pg_dump` (custom format, `-Fc`) to a local staging path, retained 7 days locally, then included in the nightly Proxmox Backup Server job for the LXC's volume (giving both logical and block-level recovery points). `pg_dump` produces a consistent snapshot without blocking readers/writers ([PostgreSQL docs — pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html)).
- **Data retention**: logical dumps retained 7 days on local disk, 30 days within Proxmox Backup Server's retention policy (aligned with SVC-32's default), per-project override possible for compliance-sensitive datasets.

## 7. Infrastructure architecture

- **Compute unit**: LXC (unprivileged), per decision F2 — no GPU/kernel isolation need.
- **Minimum resource spec**: see tiers in §6; baseline small-tier default is 1 vCPU / 2 GB RAM / 20 GB disk, matching common community guidance for small self-hosted Postgres instances alongside official image defaults ([Docker Hub — postgres](https://hub.docker.com/_/postgres)).
- **Network placement**: placed on the project's assigned VLAN (per platform foundation §7), static DHCP reservation so the application containers and OpenBao's connection secrets stay stable across reboots. Firewalled to accept inbound 5432 only from the project's own application-tier IP range.
- **Storage**: `local-zfs` dataset dedicated to the LXC's rootfs and Postgres data directory (`/var/lib/postgresql/<version>/main` inside the container, bind-mounted from a host ZFS path), enabling ZFS snapshots as a secondary safety net ahead of Proxmox Backup Server's own snapshot-based backup.

## 8. Terraform scope

- **Module inputs**: `project_slug`, `db_engine` (`postgres`|`mariadb`), `tier` (`small`|`medium`), `cpu_cores`, `memory_mb`, `disk_gb`, `vlan_tag`, `enable_pgvector`, `enable_postgis`, `enable_timescaledb`.
- **Resources created**: one `proxmox_virtual_environment_container` (LXC) via the `bpg/proxmox` provider, a dedicated ZFS-backed disk resource, and a firewall rule resource scoping inbound 5432 to the project's application subnet.
- **Outputs**: `db_host` (internal IP/hostname), `db_port` (5432 or 3306), `db_engine`, `vmid` — consumed by (a) the Ansible dynamic inventory, (b) the project's own application-stack Terraform/Ansible to template connection details into OpenBao, and (c) SVC-06 (ZITADEL) and SVC-24 (MLflow) when those services are configured to use a project-scoped Postgres instance rather than their own.

## 9. Ansible scope

- **Roles**: `postgres-server` (installs Docker/Compose if not already present via the shared `common` role, templates `compose.yaml` for the `postgres` + `postgres_exporter` services, creates the application role/database, enables requested extensions), `mariadb-server` (equivalent role for the alternative engine).
- **Idempotency**: role uses `community.postgresql` collection modules (`postgresql_db`, `postgresql_user`, `postgresql_ext`) which are natively idempotent; Compose reconciliation via `community.docker.docker_compose_v2`.
- **Config files templated**: `compose.yaml` (image tag, volume mounts, exposed internal port, resource limits via Compose `deploy.resources`), `postgresql.conf` overrides (`shared_buffers`, `max_connections`, `work_mem` per tier), `pg_hba.conf` (restrict to project subnet + localhost).
- **Secrets injected from SVC-07**: the initial superuser password and the `<slug>_app` role password are generated by Ansible using a cryptographically random generator at first run, written directly into OpenBao under `secret/proj-<slug>/postgres` (never left in Ansible facts/logs), and the Compose stack's `.env` is templated by the OpenBao Agent (per platform foundation §9) rather than by Ansible directly.

## 10. CI/CD pipeline

Stages: lint (`terraform fmt`, `ansible-lint`) → plan (`terraform plan` against the project's GitLab-managed state, per platform-foundation decision F1) → manual approve (required for any change to a production project's database resource, since disk resize/recreate can be destructive) → apply → smoke test (a post-deploy `pg_isready -h <host> -p 5432` check plus a test `SELECT 1` through the freshly-issued OpenBao-sourced credential, run from the CI runner over the project VLAN). Runs on GitLab CI, matching the platform default; state backend is the same GitLab-managed HTTP backend referenced in platform-foundation decision F1, under state name `<slug>-svc01-postgres`.

## 11. Secrets & credentials

- **Secrets that exist**: Postgres superuser password, per-project application role password, `postgres_exporter`'s read-only monitoring role credential.
- **Generation**: all passwords generated at Ansible provisioning time via a random-password lookup, immediately written to OpenBao — never echoed to CI logs (masked) or committed anywhere.
- **Rotation**: application role password rotated every 90 days by an Ansible playbook re-run (`ALTER ROLE ... WITH PASSWORD`) that also updates the corresponding OpenBao secret version; consuming application containers re-fetch on their next scheduled OpenBao Agent refresh or on next deploy.
- **Reaching the service**: consuming project containers never read the password from an environment file in the repo. Instead, each application container runs (or is sidecarred by) an OpenBao Agent that authenticates via its own AppRole (issued during that container's own provisioning, per platform-foundation §11) and renders the connection string into a local `.env`/config file read at container start.

## 12. Security & hardening baseline

- No direct network exposure via Traefik (wire-protocol service); firewalled at the VLAN/Proxmox level to the project's own subnet.
- Least-privilege DB roles: application role has no `SUPERUSER`, no `CREATEDB`, no cross-database grants.
- TLS: `pg_hba.conf` requires `hostssl` for any connection originating outside `localhost`, using a certificate issued by the internal CA (SVC-08 EJBCA) where cross-VLAN access is unavoidable.
- CVE scanning: the `postgres`/`mariadb` image tag is scanned by Harbor/Trivy (SVC-14) as part of the image promotion pipeline before being referenced by any project's Compose file.
- Auth via SVC-06: not applicable directly (Postgres has no OIDC-native login), but any admin UI placed in front of it (e.g. pgAdmin, if deployed) MUST be gated behind ZITADEL OIDC via Traefik's forward-auth middleware.

## 13. Observability hooks

- `postgres_exporter` (official Prometheus community exporter) runs as a sidecar in the same Compose stack, exposing metrics on its default port 9187 for Prometheus (SVC-17) scraping.
- Logs (Postgres server log, `log_statement` set to `ddl` by default to avoid excessive volume) shipped via Promtail to Loki (SVC-18), tagged `project_slug=<slug>`, `service=postgres`.
- Key alerts: connection count approaching `max_connections`, replication/backup job failure (nightly `pg_dump` non-zero exit), disk usage on the ZFS dataset exceeding 80%, `postgres_exporter` scrape failures (indicates the DB is down).

## 14. Acceptance criteria

- [ ] Dedicated LXC provisioned via Terraform for a test project, running `postgres:17` on port 5432, reachable only from the project subnet.
- [ ] Application role created with least-privilege grants; superuser access confirmed blocked for the application role.
- [ ] Requested extensions (pgvector/PostGIS/TimescaleDB) install cleanly when their respective flags are enabled.
- [ ] Nightly `pg_dump` job runs successfully and the dump file is included in the next Proxmox Backup Server backup cycle.
- [ ] Application connection credentials retrievable only via OpenBao — no plaintext credential found in any repository, CI log, or Compose file.
- [ ] `postgres_exporter` metrics visible in Prometheus/Grafana within 5 minutes of deployment.
- [ ] MariaDB alternative path validated at least once end-to-end (even if not used for a live project yet).

## 15. Open questions / assumptions

- Assumes `postgres:17` as the pinned major version at rollout time; revisit the pinned tag on each new major release after allowing a testing window (Postgres 18 is already available per Docker Hub's tag list).
- Assumes nightly `pg_dump` is sufficient DR granularity for all current project types; if a future project requires point-in-time recovery, WAL archiving (`pgbackrest` or similar) would need to be added as an enhancement, not currently in scope.
- Assumes a human decides per-project whether to use PostgreSQL or the MariaDB alternative at project kickoff; no automatic engine-selection heuristic is defined.
- Sizing tiers (§6) are starting estimates for a small homelab/office cluster; should be revisited once real per-project query load is observed.
