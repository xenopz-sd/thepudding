# SVC-03 — Object Storage (S3-Compatible) — Requirements

## 1. Overview

SVC-03 provides the platform's S3-compatible object storage layer, used for build artifacts, ML model weights and datasets, drone flight-log/media uploads, container image layer caching support material, and Terraform/CI artifact storage where useful. It is a foundational shared service that most project types eventually touch: embedded/drone projects store recorded flight media and firmware artifacts here, ML-video/ML-text projects store training datasets and checkpoints, and generic backend projects use it for user-uploaded files or static asset hosting behind Traefik.

## 2. Scope

**In scope:**
- Garage as the default S3-compatible object store, deployed as a shared multi-node cluster.
- MinIO as the documented fallback if Garage's feature set proves insufficient for a specific need.
- Per-project bucket and access-key provisioning within the shared cluster.
- Erasure coding / replication basics for data durability.
- Scoped S3 credential handoff to project CI pipelines via OpenBao.

**Out of scope:**
- Object storage lifecycle policies beyond basic retention (advanced tiering/archival is a future enhancement).
- Cross-cluster/geo-replicated object storage (single-site homelab/office cluster assumed).
- Using object storage as a general-purpose filesystem substitute for services that need POSIX semantics (NFS/local-zfs remains the choice there).

## 3. Technology selection

- **Default: Garage**. Origin: 🇪🇺 **Deuxfleurs, France** (nonprofit collective), purpose-built for small/medium self-hosted clusters ([Garage object storage — Deuxfleurs](https://garagehq.deuxfleurs.fr)). Chosen as the catalog default specifically because it targets this platform's scale (a handful of nodes, heterogeneous/modest hardware) rather than being a scaled-down version of a hyperscale design, and it ships a native, lightweight S3-compatible API without the operational weight of a full MinIO deployment.
- **Fallback: MinIO**. Origin: 🚩 **MinIO Inc., Redwood City, California, US** ([MinIO — contact/HQ](https://www.min.io)) — flagged as non-European per the catalog's sourcing principle. MinIO remains documented as the fallback for the specific case where a project needs a MinIO-specific feature Garage does not yet support (e.g. certain advanced IAM policy constructs, or client tooling that assumes MinIO-specific extensions); it is not deployed by default.
- Garage Docker image: `dxflrs/garage`, pinned tag e.g. `v2.0.0`. Default ports observed in community Compose deployments: **3900** (S3 API), **3901** (RPC — internal inter-node communication), **3902** (S3-compatible web/static-site hosting), **3903** (administration API) ([portalzine.de — Day 38: Garage Object Storage](https://portalzine.de/day-38-garage-object-storage-the-self-hosted-s3-alternative-7-days-of-docker/)). These four ports should be treated as the standard port set for every Garage node in this deployment.
- MinIO fallback image: `minio/minio`, default ports 9000 (S3 API) and 9001 (web console).

## 4. Multi-tenancy model

**Shared multi-node cluster** (per catalog §1 and §3 row SVC-03: "Tenancy: Shared"), with **per-project buckets and per-project access keys** providing isolation:

- One Garage cluster (recommended minimum 3 nodes for the erasure-coding/replication scheme described in §6, though a 1-node bootstrap is acceptable for the very first rollout before workload justifies the third node) serves all projects.
- Each project gets one or more buckets named `proj-<slug>-<purpose>` (e.g. `proj-dronefleet-flightlogs`, `proj-mlvideo-datasets`, `proj-mlvideo-checkpoints`).
- Each project gets its own Garage access key (`GK...` key ID / secret pair), granted read/write only on its own bucket(s) via Garage's per-key bucket permission model — never a cluster-wide admin key handed to a project pipeline.
- Naming convention: bucket names always begin with `proj-<slug>-`; access key names mirror this (`proj-<slug>-cikey` for CI-scoped keys, `proj-<slug>-appkey` for application-runtime keys if separated for least privilege).

## 5. Functional requirements

- **FR-1**: The cluster MUST expose an S3-compatible API (port 3900) sufficient for standard S3 SDK operations (`PutObject`, `GetObject`, `ListObjectsV2`, `DeleteObject`, multipart upload) used by common client libraries (boto3, aws-cli, MLflow's artifact store, GitLab CI's own artifact-to-S3 pattern).
- **FR-2**: The platform team MUST be able to create a new project bucket and scoped access key via a single idempotent Ansible/Terraform-driven operation, without manual `garage` CLI intervention for routine onboarding.
- **FR-3**: Access keys MUST be restricted to specific buckets (Garage's key-to-bucket permission grants), never cluster-admin-level by default.
- **FR-4**: The cluster MUST tolerate the loss of one node (given a minimum 3-node deployment) without data loss, per the configured replication/erasure-coding factor (§6).
- **FR-5**: The service MUST optionally support static website hosting (port 3902) for a project's public build artifacts or documentation site, gated behind Traefik for TLS termination.
- **FR-6**: Bucket-level object counts and storage usage MUST be queryable (via the Garage admin API, port 3903) to support per-project usage reporting and capacity planning.
- **FR-7**: Scoped S3 credentials (key ID + secret) MUST be retrievable by project CI pipelines exclusively through OpenBao, never stored as a plaintext GitLab CI variable.

## 6. Non-functional requirements

- **Availability**: target tolerance of one node failure without data loss or read/write outage, achieved via Garage's built-in replication (recommended replication factor 3 for a 3-node cluster, giving each object 3 copies across nodes) — Garage's design favors straightforward full replication over complex erasure-coding schemes at small node counts, since erasure coding's storage-efficiency benefit only pays off with a larger number of nodes/zones than this homelab/office cluster is expected to have.
- **Performance/sizing** (small Proxmox homelab/office cluster): a 3-node Garage cluster, each node 2 vCPU, 2–4 GB RAM, and object-data disk sized to the workload (recommend starting at 200–500 GB per node on spinning disk or SSD, scaled up as ML dataset/checkpoint storage grows — this is typically the most storage-hungry service in the whole catalog for ML-video projects). RAM/CPU needs for Garage itself are modest; disk capacity, not compute, is the primary sizing driver.
- **Backup/DR**: in addition to Garage's own intra-cluster replication (which protects against node failure, not against accidental deletion or corruption), buckets containing irreplaceable data (e.g. raw drone flight logs) are included in the Proxmox Backup Server nightly cycle at the underlying LXC/volume level; buckets containing easily-regenerable data (e.g. re-derivable model checkpoints from a training pipeline) may be excluded from backup to save space, per-project decision.
- **Data retention**: no platform-wide TTL by default; per-project lifecycle/retention policy is documented in that project's own storage plan (e.g. an ML project may prune intermediate checkpoints after N days).

## 7. Infrastructure architecture

- **Compute unit**: LXC per node, per decision F2 — Garage has no GPU/kernel isolation requirement.
- **Minimum resource spec**: 2 vCPU, 2–4 GB RAM, and a disk sized per §6 guidance, per node; minimum 3 nodes recommended for the replication factor to provide real fault tolerance (a 1-node bootstrap cluster is acceptable only as a temporary starting point, explicitly not fault-tolerant).
- **Network placement**: all Garage nodes sit on the shared-services VLAN with static DHCP reservations (RPC port 3901 must be stable and mutually reachable between nodes); the S3 API (3900) is reachable from all project VLANs via firewall allow-list, while the admin API (3903) is restricted to the management VLAN/CI runner only.
- **Storage**: each Garage node's data directory is backed by its own dedicated disk/ZFS dataset (not shared with the LXC's rootfs) so that node-level storage can be scaled independently of compute; `local-zfs` per node is the default, with the option of a larger dedicated spinning-disk pool for capacity-heavy ML dataset storage.

## 8. Terraform scope

- **Module inputs**: `node_count` (default 3), `cpu_cores`, `memory_mb`, `disk_gb_per_node`, `vlan_tag`, `replication_factor` (default 3).
- **Resources created**: `node_count` × `proxmox_virtual_environment_container` (LXC) via `bpg/proxmox`, each with a dedicated data-disk resource; a firewall rule resource exposing 3900 to project VLANs and restricting 3901/3903 appropriately.
- **Outputs**: `garage_s3_endpoint` (e.g. `http://proj-shared-garage.internal:3900`, fronted by Traefik with TLS for external-facing use), `garage_admin_endpoint`, node IP list — consumed by (a) the Ansible dynamic inventory for cluster-layout configuration, and (b) a separate, project-triggered Ansible playbook (not Terraform, since bucket/key creation is a Garage-API-level operation, not a Proxmox resource) that provisions new project buckets and keys on demand.

## 9. Ansible scope

- **Roles**: `garage-node` (installs Docker/Compose, templates `garage.toml` and `compose.yaml`, joins the node to the cluster layout via `garage layout assign`/`garage layout apply`), `garage-bucket-provision` (idempotently creates a project bucket and scoped access key via the Garage admin API/CLI, checking for existing bucket/key before creating).
- **Idempotency**: cluster layout changes (`garage layout apply`) are only executed when the target layout differs from the current one (checked via `garage layout show` output parsing); bucket/key creation checks existence via `garage bucket list`/`garage key list` before creating.
- **Config files templated**: `garage.toml` (per-node RPC address, replication factor, admin API bind address, S3 API bind address), `compose.yaml` (image tag, all four port mappings, data volume mount).
- **Secrets injected from SVC-07**: each newly-created project access key's secret is written directly to OpenBao under `secret/proj-<slug>/garage` at creation time by the provisioning playbook — the secret is never displayed in CI logs or Ansible output (registered as `no_log: true`).

## 10. CI/CD pipeline

Stages: lint → plan (cluster-level Terraform changes only — node count/sizing, not day-to-day bucket provisioning) → manual approve for cluster topology changes (adding/removing a node affects the replication layout and warrants a human check) → apply → smoke test (an S3 `PutObject`/`GetObject` round-trip test against a scratch test bucket, executed from the CI runner using a temporary test key). Separately, **project bucket onboarding** runs as its own lightweight pipeline (triggered by a new project's setup MR), auto-applying since it only touches that project's own bucket/key namespace. Runs on GitLab CI; state name `shared-svc03-garage` for the cluster itself.

## 11. Secrets & credentials

- **Secrets that exist**: the Garage cluster's shared RPC secret (used for inter-node trust), the admin API token, and each project's S3 access key ID/secret pair.
- **Generation**: the RPC secret and admin token are generated once at cluster bootstrap and stored in OpenBao under a cluster-level path (`secret/shared/garage/admin`); per-project keys are generated by the `garage key create` command at project onboarding time.
- **Rotation**: project-level S3 keys are rotated on a 180-day default cadence (object storage credentials are typically longer-lived than database passwords since they're often embedded in longer-running CI/build pipelines) or immediately on suspected compromise, via `garage key create` for a new key followed by `garage bucket allow`/`garage bucket deny` to cut over and revoke the old key.
- **Reaching the service**: project CI pipelines retrieve their scoped S3 credentials from OpenBao at pipeline-runtime (an OpenBao-authenticated CI job step exports `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` as masked pipeline variables sourced from OpenBao, never stored as static GitLab CI/CD variables) — this keeps the credential's lifetime tied to OpenBao's lease/rotation policy rather than GitLab's separate variable store.

## 12. Security & hardening baseline

- TLS termination for the S3 API and any static-website-hosting endpoint is handled by Traefik (SVC-09) when exposed beyond the internal VLAN; inter-node RPC traffic (3901) stays on the internal shared-services VLAN only, never exposed externally.
- Least-privilege: every project access key is bucket-scoped; the cluster admin token is restricted to the CI runner and platform operators only, never handed to a project pipeline.
- Firewall/VLAN rules: admin API (3903) reachable only from the management VLAN; S3 API (3900) reachable from authorized project VLANs via explicit allow-list, not open to the whole network.
- CVE scanning: the `dxflrs/garage` image tag is scanned by Harbor/Trivy (SVC-14) before being promoted to the trusted registry namespace referenced by the Compose stack.
- Auth via SVC-06: the Garage admin API itself has no native OIDC support; any web-based admin UI added on top (e.g. a community Garage web UI) MUST sit behind ZITADEL OIDC via Traefik forward-auth if deployed.

## 13. Observability hooks

- Garage exposes a native Prometheus metrics endpoint (via its admin API) scraped by Prometheus (SVC-17) for per-node and per-bucket usage/latency metrics.
- Node logs shipped via Promtail to Loki (SVC-18), tagged `service=garage`, `node=<hostname>`.
- Key alerts: any node reporting degraded/unreachable status in `garage status`, replication factor under-satisfied (fewer live copies of data than configured), disk usage per node exceeding 80%, S3 API error-rate spike (indicates client misconfiguration or an outage).

## 14. Acceptance criteria

- [ ] Garage cluster (minimum 3 nodes for production use) provisioned via Terraform+Ansible, cluster layout applied and confirmed healthy via `garage status`.
- [ ] At least one project bucket and scoped access key created, confirmed to grant access to only that bucket (a cross-bucket access attempt with the same key fails).
- [ ] Replication factor confirmed by simulating one node's unavailability and verifying reads/writes continue to succeed.
- [ ] Scoped S3 credentials retrievable by a CI pipeline only via OpenBao — no plaintext key found in any repository or static CI variable.
- [ ] Prometheus metrics visible for cluster health and per-bucket usage within 5 minutes of deployment.
- [ ] MinIO fallback path documented and validated at least once in a non-production test, confirming it can be substituted without changing the S3 API contract consumed by client applications.

## 15. Open questions / assumptions

- Assumes a 3-node minimum cluster is achievable within the available Proxmox hardware; if only 1–2 physical nodes exist initially, the cluster starts in a reduced-fault-tolerance mode with an explicit note that full replication guarantees do not apply until the third node is added.
- Assumes full replication (factor 3) rather than erasure coding is the right trade-off at this node count; revisit if the cluster grows beyond ~6 nodes, where erasure coding's storage-efficiency advantage becomes more compelling.
- Data-heavy ML dataset/checkpoint storage sizing is a rough estimate (§6); actual disk provisioning should be revisited once real project storage consumption is observed over the first few months.
- The specific trigger condition for falling back to MinIO ("Garage's feature set proves insufficient") is intentionally left as a case-by-case human judgment call rather than a predefined technical threshold.
