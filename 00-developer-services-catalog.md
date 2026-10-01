# Standard Developer Services Catalog — Proxmox Platform

**Purpose of this document:** enumerate the standard, reusable infrastructure services that should exist on the Proxmox environment for every new project (embedded/Raspberry Pi/NXP, drone hardware, and ML for video/image/text), so that projects consume shared services instead of re-inventing a database, IDP, message broker, etc. each time.

Each row below is meant to become the **input for a dedicated requirements document** (`SVC-xx-<slug>-requirements.md`), which in turn becomes the input for automated build-out in KIRO. Service IDs (`SVC-xx`) are stable and should be reused as filenames/tags downstream. Requirements documents for the platform foundation and all Tier 1 services are now available in this project (see §5).

*Revision 2 — incorporates feedback: European-first sourcing (flagged where not possible), Proxmox VM/LXC as the sole compute unit, Docker Compose only (no Kubernetes), GitLab-managed Terraform state, and "dedicated instance per project" as an accepted multi-tenancy pattern alongside shared multi-tenant services.*

*Revision 2.1 — SVC-14 default switched from Harbor to Zot: Harbor's multi-container stack (core, portal, job service, registry, Redis, Postgres, Trivy adapter, nginx) is disproportionately heavy for a service you may want to stand up and tear down quickly for a single project's CI, whereas Zot ships as one static binary/one Compose service with no external database dependency, while still providing built-in auth, garbage collection, and Trivy-based vulnerability scanning/SBOM generation. Both are equally non-European, so this is a pure operational-simplicity improvement, not a sourcing change; Harbor remains listed as the heavier fallback for teams that want its more mature RBAC/quota UI at scale, and Forgejo's built-in registry remains the only genuinely European option if full sovereignty is prioritized over registry-native scanning.*

---

## 1. Guiding principles

- **Self-hosted, open source first, European by default.** For every service, the primary candidate is the most credible option with genuine European governance or headquarters. Where no such option exists at comparable maturity, this is called out explicitly rather than silently defaulting to a US vendor (see the **Origin** column in §3, and the flagged exceptions in §3.5).
- **IaC-native, Proxmox VM/LXC only.** Every service is provisioned as a Proxmox **VM or LXC container** via **Terraform** (the [`bpg/proxmox`](https://registry.terraform.io/providers/bpg/proxmox/latest/docs) provider — actively maintained, API-token based, SSH optional) and configured with **Ansible**. No Kubernetes/k3s layer.
- **Docker Compose as the sole runtime convention.** Every service ships as a Docker Compose stack on its VM/LXC, reverse-proxied by Traefik — consistent with your existing pattern.
- **CI/CD-driven.** Every service is deployable end-to-end from GitLab CI or GitHub Actions: no manual click-ops, secrets pulled from the vault/CI variable store, plan/apply gated by review, idempotent re-runs.
- **Multi-tenancy: two accepted patterns, chosen per service.**
  - **Shared platform service** — one instance serves all projects with logical separation (schemas/DBs/realms/buckets per project). Best for services that benefit from a single source of truth: IDP, secrets manager, VPN, DNS, registry, monitoring, CI runners.
  - **Dedicated per-project instance ("namespace")** — a project gets its own full Compose stack (its own DB container, its own cache, etc.), torn down with the project. Best for services where isolation, independent lifecycle, or blast-radius containment matters more than central reuse: SQL/NoSQL databases, MQTT broker instance, vector DB, model-serving endpoint.
  - The recommended pattern per service is noted in §3; both are valid, and the requirements doc for each service should make the choice explicit rather than leaving it implicit.
- **Embedded & ML awareness.** Beyond generic backend services, the list covers what embedded/drone and ML-video/image/text projects specifically need: telemetry ingestion, OTA/firmware distribution, device provisioning, model serving/registry, and vector search.

---

## 2. Foundational platform decisions (prerequisite for all services)

| # | Decision | Decision made | Notes |
|---|---|---|---|
| F1 | Terraform state backend | **GitLab-managed Terraform state** (native GitLab HTTP backend) | Simplest path since GitLab is the CI host. If gitlab.com (SaaS) is used rather than a self-hosted GitLab instance, state passes through GitLab Inc.'s US-hosted infrastructure — flag this explicitly in the platform-foundation requirements doc as a sovereignty trade-off, with self-hosted GitLab CE or [Forgejo](https://forgejo.org/) (🇩🇪 Germany, Codeberg e.V., fully self-hostable, MIT/GPLv3) noted as the fully-European alternative if this later becomes a hard requirement. |
| F2 | Compute unit per service | **LXC** for lightweight Linux services; **VM** only where full kernel isolation or GPU passthrough is required (ML/GPU services) | |
| F3 | Runtime inside compute unit | **Docker Compose** per service VM/LXC, reverse-proxied by Traefik | No Kubernetes/k3s. |
| F4 | Ansible inventory source | Terraform outputs (IPs/IDs) rendered to a dynamic Ansible inventory checked into the pipeline artifact | |
| F5 | Secrets in CI | GitLab CI/CD variables bootstrap the *first* connection to the self-hosted secrets manager (SVC-07); nothing else is hardcoded in repos | |
| F6 | Multi-tenancy default | Per-service, see §1 and the **Tenancy** column in §3 | |

---

## 3. Service catalog

Tier legend: **T1 = core**, **T2 = recommended**, **T3 = specialized**.
Origin legend: 🇪🇺 = genuinely European (company/foundation HQ or governance), 🌍 = neutral/global open governance (no single national corporate owner), 🚩 = not European (flagged per your request; included because no comparable European option was found, or noted alongside a European alternative).

### A. Data services

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-01 | Relational (SQL) database | Dedicated per project | **PostgreSQL** (default — pgvector/PostGIS/Timescale extensions cover vector search, geospatial drone data, and time-series in one engine) | 🌍 No single corporate owner; global community, strong EU contributor base | T1 |
| | | | Strict-EU alt: **MariaDB** | 🇪🇺 MariaDB Foundation, Helsinki, Finland (nonprofit) | |
| SVC-02 | Cache / key-value store | Shared or dedicated | **Redis** (AGPLv3 since May 2025) | 🚩 Created by Salvatore Sanfilippo (🇮🇹 Italy); steward Redis Inc. is 🇺🇸 US (Mountain View). No actively-maintained fully-European equivalent found (Dragonfly, KeyDB are also non-EU) — flagged, included pragmatically. | T1 |
| SVC-03 | Object storage (S3-compatible) | Shared | **Garage** (default) | 🇪🇺 Deuxfleurs, France (nonprofit collective), purpose-built for small/medium self-hosted clusters | T1 |
| | | | Fallback: **MinIO** | 🚩 MinIO Inc., Redwood City, California, US | |
| SVC-04 | Time-series database | Shared or dedicated | **QuestDB** (default) | 🇪🇺 QuestDB Ltd., London, UK | T2 |
| | | | Alt (fewer services): TimescaleDB extension on SVC-01 | 🚩 Timescale Inc., New York, US | |
| SVC-05 | Vector database | Shared or dedicated | **Qdrant** (default) | 🇪🇺 Qdrant Solutions GmbH, Berlin, Germany | T2 |
| | | | Alt: **Weaviate** | 🇪🇺 Weaviate B.V., Amsterdam, Netherlands | |

### B. Identity, access & secrets

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-06 | Identity provider / SSO | Shared | **ZITADEL** (default) | 🇪🇺 CAOS Ltd, St. Gallen, Switzerland | T1 |
| | | | Alt (max protocol breadth): **Keycloak** | 🚩 Originated at Red Hat, Raleigh, NC, US | |
| SVC-07 | Secrets manager | Shared | **OpenBao** | 🚩 No clearly European alternative of comparable maturity found. OpenBao is Linux Foundation-governed (neutral, not a single national vendor) — the closest available middle ground. Flagged explicitly per your request. | T1 |
| SVC-08 | Internal PKI / certificate authority | Shared | **EJBCA Community** (default) | 🇪🇺-origin: created by PrimeKey, Solna, Sweden; sponsor since 2022 is Keyfactor 🚩 (US, Ohio) — the Community Edition remains LGPL-2.1, fully open and self-hostable regardless of sponsor | T2 |
| | | | Lightweight alt: **step-ca** | 🚩 Smallstep, San Francisco, US | |

### C. Networking & edge

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-09 | Reverse proxy / TLS termination | Shared | **Traefik** (already in use) | 🇪🇺 Traefik Labs, Lyon, France | T1 |
| SVC-10 | Internal DNS | Shared | **PowerDNS** | 🇪🇺 PowerDNS.COM B.V., The Hague, Netherlands | T2 |
| SVC-11 | Private mesh VPN | Shared | **NetBird** (already in use) | 🇪🇺 NetBird GmbH, Berlin, Germany | T1 |
| SVC-12 | API gateway | Shared | **Tyk** | 🇪🇺 Tyk Technologies Ltd, London, UK | T3 |

### D. CI/CD & software supply chain

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-13 | Self-hosted CI runners | Shared | **GitLab Runner** (default, matches your GitLab/GitHub-hosted pipeline requirement) | 🚩 GitLab Inc. is US-HQ (Nasdaq), though founded by a Ukrainian and a Dutch developer; the Runner binary itself is MIT-licensed and runs fully under your control | T1 |
| | | | Fully-sovereign alt: **Forgejo + Forgejo Actions** (self-hosted forge, drop-in for GitLab if full independence from GitLab Inc./GitHub-Microsoft is later desired) | 🇪🇺 Codeberg e.V., Berlin, Germany (nonprofit) | |
| SVC-14 | Container registry | Shared | **Zot** (default — single static binary/single Compose service, no Postgres/Redis dependency, built-in auth + Trivy-based vulnerability scanning/SBOM + garbage collection; chosen over Harbor for operational simplicity, especially for spin-up/tear-down dev instances) | 🚩 Originated at Cisco, US; CNCF Sandbox-governed. No actively-maintained strong European equivalent identified. | T1 |
| | | | Heavier alt (full RBAC/quota UI, established at scale): **Harbor** | 🚩 Originated at VMware/Broadcom, US; CNCF Graduated. | |
| | | | Zero-extra-service alt (if self-hosting Forgejo): **Forgejo's built-in container registry** | 🇪🇺 Codeberg e.V., Berlin, Germany — the only genuinely European option, at the cost of no registry-native scanning/retention policies (bolt on Trivy as a separate CI step instead) | |
| SVC-15 | Package/artifact cache & proxy | Shared | **apt-cacher-ng** + **Verdaccio** | 🇪🇺 apt-cacher-ng: Eduard Bloch, Germany (individual maintainer) · Verdaccio: community-governed, no single national owner (🌍) | T2 |

### E. Observability

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-17 | Metrics & dashboards | Shared | **Prometheus + Grafana** (+ `dcgm-exporter` for NVIDIA GPU metrics) | 🇪🇺 Prometheus originated at SoundCloud, Berlin, Germany (now CNCF-governed, 🌍); Grafana created by Torkel Ödegaard, Sweden (Grafana Labs: dual Stockholm/NYC HQ). `dcgm-exporter` is 🚩 NVIDIA (US), unavoidable for NVIDIA GPU telemetry. | T1 |
| SVC-18 | Centralized logging | Shared | **Grafana Loki** + Promtail | 🇪🇺 Grafana Labs (Nordic-linked, see above) | T1 |
| SVC-19 | Distributed tracing | Shared | **Grafana Tempo** | 🇪🇺 Grafana Labs | T3 |
| SVC-20 | Alerting & uptime monitoring | Shared | **Alertmanager** (Prometheus ecosystem) | 🇪🇺 (see SVC-17) | T2 |

### F. Messaging & integration

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-21 | MQTT broker | Shared or dedicated | **Mosquitto** (default) | 🇪🇺🇪🇺 Eclipse Foundation AISBL, Brussels, Belgium; development driven by Cedalo GmbH, Freiburg, Germany | T1 |
| | | | Clustered-scale alt: **VerneMQ** | 🇪🇺 Octavo Labs AG, Switzerland (successor to Erlio GmbH, Germany) | |
| SVC-22 | Message queue / event bus | Shared or dedicated | **RabbitMQ** (default) | 🇪🇺-origin: LShift, London, UK (2007); current steward Broadcom is 🚩 US — codebase remains MPL-2.0 open source regardless of owner | T2 |

### G. ML/AI platform services

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-23 | Model serving / inference endpoint | Dedicated per project | **vLLM** (LLMs, matches your existing usage) + **Triton Inference Server** (CV models) | 🚩 vLLM: UC Berkeley, US. Triton: NVIDIA, US. No European alternative at comparable GPU-inference maturity was found — flagged as a pragmatic exception. **LocalAI** (🇮🇹 Ettore Di Giacinto, Italy) is a genuinely European alternative for lighter/CPU-bound inference if reducing non-EU footprint outweighs raw throughput. | T1 |
| SVC-24 | Experiment tracking & model registry | Shared | **MLflow** | 🚩 Databricks, US. Note: Neptune.ai (🇵🇱 Warsaw, Poland) was the closest actively-maintained European alternative, but it was [acquired by OpenAI in December 2025](https://www.gunder.com/en/news-insights/client-news/gunderson-client-neptune-to-be-acquired-by-openai) and its hosted service shut down March 5, 2026 — no longer viable. Flagged: no European alternative currently identified. | T2 |
| SVC-25 | Media/video ingestion & streaming | Shared or dedicated | **MediaMTX** (RTSP/RTMP/WebRTC relay) + your existing **Frigate** as NVR/AI layer | 🇪🇺 MediaMTX: created by Alessandro Ré (bluenviron), Italy. 🚩 Frigate: Blake Blackshear, US — kept as-is since it's already part of your stack. | T2 |
| SVC-26 | Dataset/annotation storage & labeling | Dedicated | **Label Studio** or **CVAT** | 🚩 Label Studio: HumanSignal, US. CVAT.ai: Wilmington, Delaware, US (with a Paphos, Cyprus 🇪🇺 office). No clearly EU-headquartered leading annotation platform identified — flagged. | T3 |
| SVC-27 | GPU job scheduling | Shared | **Slurm** | 🚩 SchedMD, US (originated at Lawrence Livermore National Lab). No comparable European alternative — flagged, though Slurm is the de facto standard even at EU HPC centers. | T3 |

### H. Embedded & drone-specific services

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-28 | OTA/firmware update server | Shared or dedicated | **Mender** (default — turnkey fleet management) | 🇪🇺 Northern.tech AS, Oslo, Norway | T1 |
| | | | Alt (max flexibility, custom partition layouts): **Eclipse hawkBit** + RAUC/SWUpdate clients | 🇪🇺🇪🇺 Eclipse Foundation, Belgium; originated at Bosch, Germany | |
| SVC-29 | Network boot / OS provisioning | Shared | **iPXE** + Ansible-driven TFTP/DHCP | 🇪🇺 Michael Brown, UK (creator; community-governed, no corporate HQ) | T2 |
| SVC-30 | Cross-compilation build cache | Shared | **ccache** | 🌍 Community-maintained (BSD); current maintainer Joel Rosdahl — individual open-source maintainership, no corporate registration found | T2 |
| SVC-31 | Device/fleet management & inventory | Dedicated | Custom service on SVC-01 (Postgres) + SVC-06 (ZITADEL) auth, or use Mender's built-in device inventory (SVC-28) | Inherits origin of underlying components (all 🇪🇺 above) | T3 |

### I. Backup & resilience

| ID | Service | Tenancy | Candidate tech | Origin | Tier |
|---|---|---|---|---|---|
| SVC-32 | Backup & disaster recovery | Shared | **Proxmox Backup Server** | 🇪🇺 Proxmox Server Solutions GmbH, Vienna, Austria | T1 |

### §3.5 — Where a genuinely European option was not found

Flagged explicitly, as requested, rather than silently defaulting: **Redis** (SVC-02), **secrets management at Vault's maturity level** (SVC-07), **container registry with integrated scanning at Zot/Harbor's maturity level** (SVC-14 — Forgejo's built-in registry is the genuinely European fallback, but with a materially lighter feature set), **GPU-accelerated LLM/CV inference serving** (SVC-23), **ML experiment tracking** (SVC-24, since Neptune.ai's Dec-2025 acquisition by OpenAI), **dataset annotation/labeling** (SVC-26), and **GPU job scheduling** (SVC-27). In each case the table above still names the most credible European-linked or European-origin option where one exists (even if the current corporate steward is American), and otherwise the best available pragmatic choice.

---

## 4. Rollout order (agreed)

1. **Platform foundation** (F1–F6): GitLab-managed Terraform state, LXC/VM provisioning conventions, dynamic Ansible inventory, secrets bootstrap.
2. **Tier 1 core:** SVC-01 (PostgreSQL), SVC-02 (Redis), SVC-03 (Garage), SVC-06 (ZITADEL), SVC-07 (OpenBao), SVC-09 (Traefik), SVC-11 (NetBird), SVC-13 (GitLab Runner), SVC-14 (Zot), SVC-17/18 (Prometheus+Grafana / Loki), SVC-21 (Mosquitto), SVC-23 (vLLM/Triton), SVC-28 (Mender), SVC-32 (Proxmox Backup Server).
3. **Tier 2 recommended:** SVC-04, 05, 08, 10, 15, 20, 22, 24, 25, 29, 30 — add as soon as 2+ projects need them.
4. **Tier 3 specialized:** SVC-12, 19, 26, 27, 31 — on demand.

## 5. Requirements documents produced

Following the reusable 15-section template in `requirements-template.md`, full requirements documents now exist in this project under `requirements/` for the platform foundation and every Tier 1 service:

- `requirements/PF-platform-foundation-requirements.md`
- `requirements/SVC-01-sql-database-requirements.md`
- `requirements/SVC-02-cache-kv-store-requirements.md`
- `requirements/SVC-03-object-storage-requirements.md`
- `requirements/SVC-06-identity-provider-requirements.md`
- `requirements/SVC-07-secrets-manager-requirements.md`
- `requirements/SVC-09-reverse-proxy-requirements.md`
- `requirements/SVC-11-vpn-mesh-requirements.md`
- `requirements/SVC-13-ci-runners-requirements.md`
- `requirements/SVC-14-container-registry-requirements.md`
- `requirements/SVC-17-18-observability-requirements.md`
- `requirements/SVC-21-mqtt-broker-requirements.md`
- `requirements/SVC-23-model-serving-requirements.md`
- `requirements/SVC-28-ota-update-server-requirements.md`
- `requirements/SVC-32-backup-dr-requirements.md`

Each is self-contained, carries forward this catalog's origin/tenancy decisions without re-litigating them, and is scoped to be handed directly to KIRO for automated implementation. Tier 2/3 services (SVC-04, 05, 08, 10, 12, 15, 19, 20, 22, 24–27, 29–31) do not yet have requirements docs — write them following the same template once those services are scheduled for rollout.

---

### Sources consulted

- [bpg/proxmox Terraform provider documentation](https://registry.terraform.io/providers/bpg/proxmox/latest/docs)
- [Qdrant headquarters — PitchBook](https://pitchbook.com/profiles/company/489063-43)
- [Weaviate — LinkedIn](https://www.linkedin.com/company/weaviate)
- [ZITADEL — CB Insights](https://www.cbinsights.com/company/zitadel)
- [MariaDB Foundation — LinkedIn](https://www.linkedin.com/company/mariadb-foundation)
- [Garage object storage — Deuxfleurs](https://garagehq.deuxfleurs.fr)
- [OVHcloud acquires OpenIO](https://corporate.ovhcloud.com)
- [QuestDB — company info](https://questdb.com)
- [PrimeKey/EJBCA — imprint](https://www.powerdns.com), [EJBCA licenses](https://www.ejbca.org/license/), [Keyfactor/ejbca-ce on GitHub](https://github.com/Keyfactor/ejbca-ce)
- [PowerDNS.com B.V. imprint](https://www.powerdns.com)
- [Tyk Technologies — company info](https://tyk.io)
- [Traefik Labs — Pappers company registry (FR)](https://www.pappers.fr)
- [NetBird GmbH — imprint](https://netbird.io)
- [Eclipse Mosquitto](https://mosquitto.org), [Cedalo — About](https://www.cedalo.com/about)
- [VerneMQ / Octavo Labs](https://vernemq.com)
- [RabbitMQ — Wikipedia](https://en.wikipedia.org/wiki/RabbitMQ)
- [Northern.tech / Mender — Oslo office](https://northern.tech/careers/oslo/versatile-engineer-embedded-and-backend)
- [Eclipse hawkBit](https://projects.eclipse.org/projects/iot.hawkbit)
- [iPXE on GitHub](https://github.com/ipxe/ipxe)
- [ccache — ccache.dev bugs page](https://ccache.dev)
- [Grafana Labs — Torkel Ödegaard](https://grafana.com/blog/authors/torkelo/)
- [Prometheus origin — SoundCloud](https://developers.soundcloud.com)
- [Neptune.ai acquired by OpenAI — Gunderson Dettmer](https://www.gunder.com/en/news-insights/client-news/gunderson-client-neptune-to-be-acquired-by-openai), [Neptune transition hub](https://docs.neptune.ai/transition_hub)
- [ArangoDB imprint (Cologne, Germany + US HQ)](https://arango.ai/imprint/)
- [Forgejo](https://forgejo.org/), [What is Codeberg?](https://docs.codeberg.org/getting-started/what-is-codeberg/)
- [Keycloak — Wikipedia](https://en.wikipedia.org/wiki/Keycloak)
- [InfluxData — LinkedIn](https://www.linkedin.com/company/influxdb)
- [Timescale — LinkedIn](https://www.linkedin.com/company/timescaledb)
- [Redis license history — redis.io](https://redis.io/blog/redis-is-now-available-under-the-agplv3-open-source-license/), [Redis — Wikipedia](https://en.wikipedia.org/wiki/Redis)
- [GitLab Inc. — Wikipedia](https://en.wikipedia.org/wiki/GitLab)
- [Harbor — CNCF project journey report](https://www.cncf.io)
- [MinIO — contact/HQ](https://www.min.io)
- [Proxmox Server Solutions GmbH — legal notice](https://www.proxmox.com)
- [VictoriaMetrics origin — The Register](https://www.theregister.com/2023/12/11/victoriametrics_interview/)
- [Zot registry project — GitHub](https://github.com/project-zot/zot), [Zot — zotregistry.dev](https://zotregistry.dev/), [Zot origin at Cisco — RudeTools](https://rudetools.dev/registries/zot), [Zot CNCF Sandbox acceptance — CNCF](https://www.cncf.io/projects/zot/)
- [Harbor vs Docker Registry weight comparison — Railway Blog](https://blog.railway.com/p/best-container-registries-2026)
- [Forgejo/Gitea built-in container registry — Gitea Docs](https://docs.gitea.com/usage/packages/container/)
