# SVC-02 — Cache / Key-Value Store — Requirements

## 1. Overview

SVC-02 provides the standard in-memory cache, session store, and lightweight pub/sub broker used across the platform. It backs application-level caching for web backends, session storage for anything sitting behind ZITADEL-authenticated frontends, short-lived task/queue state for embedded and ML pipelines (e.g. a drone telemetry ingestion service debouncing bursts before writing to SVC-01), and pub/sub fan-out for real-time dashboards. Every project type in the catalog can consume it, but it is most heavily used by generic backend and ML-video/ML-text projects that need low-latency ephemeral state alongside their durable Postgres store.

## 2. Scope

**In scope:**
- Redis as the chosen cache/session/pub-sub engine.
- Both tenancy options (shared, namespaced-by-key-prefix vs. dedicated-per-project) with a stated default.
- Sizing guidance for a small Proxmox homelab/office cluster.
- Persistence configuration (RDB/AOF) appropriate to a cache workload (not a durable primary store).
- Credential/connection handoff via OpenBao.

**Out of scope:**
- Using Redis as a primary system of record (that role belongs to SVC-01/SVC-04/SVC-05).
- Redis Cluster (multi-shard) topologies — out of scope at this cluster size; single-instance-per-tenant is assumed sufficient.
- Message-queue semantics beyond simple pub/sub (durable queues are SVC-21 Mosquitto / SVC-22 RabbitMQ).

## 3. Technology selection

- **Chosen: Redis**, license **AGPLv3** since May 2025 ([Redis license history — redis.io](https://redis.io/blog/redis-is-now-available-under-the-agplv3-open-source-license/)). Origin: 🚩 created by **Salvatore Sanfilippo**, who is **Italian** 🇮🇹, but the current steward, **Redis Inc., is a US company headquartered in Mountain View, California** ([Redis — Wikipedia](https://en.wikipedia.org/wiki/Redis)). Per the catalog's guiding principles, this is flagged explicitly rather than silently defaulted: **no actively-maintained, fully-European equivalent was found** at comparable maturity — Dragonfly and KeyDB, the closest technical alternatives, are also non-EU-governed, so they do not resolve the sourcing concern and were not adopted as an alternate default. Redis is included pragmatically because of its ubiquity, protocol compatibility with existing client libraries, and operational simplicity at this scale.
- **No European alternative currently recommended.** This is the one entry in the Data Services group where the catalog accepts a US-stewarded technology without an EU-governed fallback (see catalog §3.5).
- Official Docker image: **`redis`** on Docker Hub, default port **6379** ([Docker Hub — redis official image](https://hub.docker.com/_/redis)). Recommended pinned tag: `redis:7.4` (or the current stable 7.x line) — pin a specific version, do not track `latest`. Note that the official image ships with Redis's "protected mode" effectively bypassed once a port is published outside the Docker network, so a password (`requirepass`) is mandatory the moment the port leaves the container's internal network ([Docker Hub — redis official image](https://hub.docker.com/_/redis)).

## 4. Multi-tenancy model

Both patterns from catalog §1 are valid for this service and are documented here explicitly, per the catalog's instruction that each service's requirements doc make the tenancy choice explicit:

- **Option A — Dedicated per project (recommended default)**: each project gets its own Redis Compose stack, its own LXC, its own volume (if AOF/RDB persistence is enabled at all). Naming: `proj-<slug>-redis`. This is the **default** recommendation because it gives full isolation (no risk of one project's cache eviction policy or memory pressure starving another's), independent lifecycle (torn down with the project), and avoids the operational overhead of enforcing key-prefix discipline across unrelated codebases.
- **Option B — Shared instance, namespaced by key prefix (option for lightweight projects)**: a single shared `proj-shared-redis` instance serves multiple lightweight projects, each required to prefix every key with its `project_slug` (e.g. `dronefleet:session:<id>`, `mlvideo:cache:<key>`) and to use a distinct numbered Redis logical database (`SELECT 0..15`) or ACL-scoped user as a secondary isolation layer. This option exists for genuinely lightweight projects (e.g. a small internal tool with negligible cache traffic) where standing up a full dedicated LXC is not worth the resource cost.
- **Recommendation**: default new projects to **Option A (dedicated per project)**; only use Option B when a project's expected cache footprint is small enough that dedicating a full LXC would be wasteful, and document the choice in that project's own infra README.

## 5. Functional requirements

- **FR-1**: The service MUST expose the Redis wire protocol (RESP) on port 6379 (container-internal), reachable only from the project's own application containers (or, for shared mode, from all authorized projects' subnets).
- **FR-2**: The service MUST require authentication (`requirepass`, or Redis ACLs for shared-mode per-project users) — an unauthenticated Redis instance is never acceptable, including on an internal VLAN.
- **FR-3**: For shared-mode deployments, the service MUST support Redis ACL users scoped per project (`ACL SETUSER <slug> ... ~<slug>:* +@all -@dangerous`), restricting each project's user to its own key prefix and denying destructive commands like `FLUSHALL`/`FLUSHDB` to non-admin users.
- **FR-4**: The service MUST support pub/sub channels for real-time fan-out use cases (e.g. a live telemetry dashboard) without requiring a separate broker for that purpose.
- **FR-5**: The service MUST be configurable with an eviction policy appropriate to a cache workload (default `allkeys-lru`) and a `maxmemory` cap sized per tier (see §6), rather than allowing unbounded memory growth.
- **FR-6**: Persistence (RDB snapshotting, optionally AOF) MUST be configurable per deployment — enabled for session-store use cases where losing all sessions on restart is disruptive, disabled/minimal for pure ephemeral-cache use cases to reduce disk I/O.
- **FR-7**: Connection credentials (host, port, password or ACL username/password) MUST be retrievable by consuming application containers exclusively through OpenBao.

## 6. Non-functional requirements

- **Availability**: single-node, best-effort — Redis here is a cache/session layer, not a durable system of record, so a restart with a cold cache is an accepted failure mode; session loss on restart is mitigated by RDB persistence where session continuity matters.
- **Performance/sizing** (small Proxmox homelab/office cluster):
  - **Dedicated small-project tier**: 1 vCPU, 256–512 MB RAM (`maxmemory` capped at ~70% of container RAM, e.g. 350 MB on a 512 MB container), 2 GB disk (mostly for RDB snapshot headroom).
  - **Dedicated medium-project tier** (heavier session/cache load, e.g. an ML-video project caching inference results): 1–2 vCPU, 1–2 GB RAM, 5 GB disk.
  - **Shared-mode instance** (Option B, serving several lightweight projects): 2 vCPU, 2–4 GB RAM total, sized to the sum of its tenants' light workloads with headroom, disk 10 GB for RDB snapshots.
- **Backup/DR**: RDB snapshots (if enabled) are captured on the standard Proxmox Backup Server nightly cycle at the LXC/volume level; Redis is not considered durable storage, so backup exists to shorten recovery time, not to guarantee zero data loss.
- **Data retention**: cache data has no long-term retention requirement by definition; session data persisted via RDB is retained only as long as the snapshot rotation window (aligned with SVC-32 defaults, typically 7–30 days), acceptable since sessions naturally expire via TTL well before that.

## 7. Infrastructure architecture

- **Compute unit**: LXC, per decision F2 — no GPU/kernel isolation requirement.
- **Minimum resource spec**: see tiers in §6; baseline default for a new dedicated-per-project instance is 1 vCPU / 512 MB RAM / 2 GB disk.
- **Network placement**: dedicated-mode instances sit on the owning project's VLAN with a static DHCP reservation; shared-mode instances sit on the shared-services VLAN, reachable from any authorized project subnet via an explicit firewall allow-list per tenant.
- **Storage**: `local-zfs`, minimal disk footprint since Redis is primarily memory-resident; RDB snapshot files are the only persistent artifact of note.

## 8. Terraform scope

- **Module inputs**: `project_slug`, `tenancy_mode` (`dedicated`|`shared`), `tier` (`small`|`medium`), `cpu_cores`, `memory_mb`, `enable_persistence` (bool), `vlan_tag`.
- **Resources created**: one `proxmox_virtual_environment_container` (LXC) via `bpg/proxmox` per dedicated instance, or a reference to the existing shared instance's Terraform-managed resource plus a new ACL-user resource (managed via Ansible, not Terraform, since Redis ACL users are not a Proxmox/Terraform-level concept) for shared-mode onboarding.
- **Outputs**: `redis_host`, `redis_port` (6379), `tenancy_mode`, `key_prefix` (for shared mode) — consumed by the project's application-stack Terraform/Ansible to template connection details into OpenBao.

## 9. Ansible scope

- **Roles**: `redis-server` (templates `compose.yaml`, `redis.conf` with `requirepass`/ACL config, `maxmemory`/`maxmemory-policy`, persistence settings), `redis-acl-user` (idempotently applies `ACL SETUSER` for a new shared-mode tenant via `redis-cli` invoked through an Ansible `command` module with an idempotency check against `ACL LIST`).
- **Idempotency**: `redis.conf` templating is fully idempotent (Jinja2 template + `notify: restart redis` only on actual content change); ACL user creation checks existing ACL state before applying to avoid unnecessary `ACL SETUSER` calls that would otherwise be harmless but noisy.
- **Config files templated**: `redis.conf` (bind address, `requirepass`/ACL file, `maxmemory`, `maxmemory-policy`, `appendonly`/`save` directives), `compose.yaml` (image tag, resource limits, volume mount for RDB/AOF file if persistence enabled).
- **Secrets injected from SVC-07**: the `requirepass` value (dedicated mode) or each tenant's ACL password (shared mode) is generated by Ansible at provisioning time and written to OpenBao under `secret/proj-<slug>/redis`; the Compose stack's `.env` is rendered by the OpenBao Agent, consistent with the platform-wide secrets bootstrap flow.

## 10. CI/CD pipeline

Stages: lint → plan (`terraform plan` against GitLab-managed state per platform-foundation decision F1) → auto-approve for dedicated small-tier instances (low blast radius), manual approve for any change to the shared-mode instance (affects multiple tenants) → apply → smoke test (`redis-cli -h <host> -a <password> PING` expecting `PONG`, executed from the CI runner). Runs on GitLab CI; state name `<slug>-svc02-redis` (dedicated) or `shared-svc02-redis` (shared instance).

## 11. Secrets & credentials

- **Secrets that exist**: the dedicated instance's `requirepass`, or per-tenant ACL username/password pairs in shared mode.
- **Generation**: random password generated by Ansible at provisioning/onboarding time, immediately written to OpenBao, never logged or committed.
- **Rotation**: rotated every 90 days (or immediately on suspected compromise); rotation for shared-mode tenants is scoped to that tenant's ACL user only, with zero impact on other tenants' credentials — a direct benefit of the ACL-per-tenant design even in shared mode.
- **Reaching the service**: consuming application containers retrieve their Redis connection string via their own OpenBao Agent, identical pattern to SVC-01 — never a hardcoded password in a repo or image.

## 12. Security & hardening baseline

- No public exposure via Traefik (raw TCP/RESP protocol, not HTTP); firewalled to the owning project's subnet (dedicated) or an explicit tenant allow-list (shared).
- `requirepass`/ACLs mandatory in all modes — the official image's default "protected mode is off once a port is published" behavior is explicitly compensated for by always setting a password before any port mapping is applied ([Docker Hub — redis official image](https://hub.docker.com/_/redis)).
- Dangerous commands (`FLUSHALL`, `FLUSHDB`, `CONFIG`, `SHUTDOWN`) are disabled or renamed for non-admin ACL users in shared mode.
- CVE scanning: `redis` image tag scanned by Harbor/Trivy (SVC-14) before promotion to the trusted project registry namespace.
- Auth via SVC-06: not directly applicable (Redis has no OIDC-native auth); any Redis-browsing admin UI (e.g. RedisInsight, if deployed) MUST be placed behind ZITADEL OIDC via Traefik forward-auth.

## 13. Observability hooks

- `redis_exporter` (Prometheus community exporter, official `oliver006/redis_exporter` image) runs as a sidecar in the Compose stack, default port 9121, scraped by Prometheus (SVC-17).
- Redis logs shipped via Promtail to Loki (SVC-18), tagged `project_slug`/`tenant` (shared mode) and `service=redis`.
- Key alerts: memory usage approaching `maxmemory` (eviction rate spike), instance restart/OOM-kill events, `redis_exporter` scrape failure (instance down), unexpectedly high `FLUSHALL`/`FLUSHDB` command counter (potential misuse in shared mode).

## 14. Acceptance criteria

- [ ] Redis instance (dedicated or shared, as chosen) provisioned via Terraform+Ansible, requiring authentication before any command succeeds.
- [ ] `maxmemory` and eviction policy correctly applied per the assigned tier.
- [ ] For shared mode: at least two tenant ACL users created, each confirmed unable to read or write the other's key prefix.
- [ ] Connection credentials retrievable only via OpenBao — no plaintext password in any repo, CI log, or Compose file.
- [ ] `redis_exporter` metrics visible in Prometheus/Grafana within 5 minutes of deployment.
- [ ] Pub/sub functionality verified with a simple publish/subscribe smoke test.

## 15. Open questions / assumptions

- Assumes the AGPLv3 licensing change (May 2025) does not create a legal concern for this internal, non-redistributed homelab/office use — worth a one-time legal sanity check if Redis-based functionality is ever exposed as a redistributed product rather than internal infrastructure.
- Assumes single-instance Redis (no Redis Sentinel/Cluster) is acceptable at this scale; revisit if a project's availability requirement for session continuity becomes stricter than "best-effort, cold-cache-on-restart acceptable."
- Per-project choice of dedicated vs. shared mode is left to a human decision at project kickoff, based on expected cache footprint — no automated sizing heuristic is defined here.
- No fully-European alternative is currently tracked as a fallback for this service; this should be periodically re-checked (e.g. annually) in case a credible EU-governed Redis-compatible engine matures.
