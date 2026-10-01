# SVC-17/18 — Metrics, Dashboards & Centralized Logging Requirements

## 1. Overview

This service provides the platform-wide observability stack: metrics collection and dashboards (Prometheus + Grafana, SVC-17), centralized log aggregation (Grafana Loki + Promtail, SVC-18), and alert routing (Alertmanager, SVC-20, bundled here since it ships from the same Prometheus ecosystem and is deployed alongside SVC-17/18 in practice). Every other service in the catalog depends on this stack to be operable in production: it is the first place an engineer looks when a database is slow, a broker is dropping messages, or a GPU inference endpoint is OOM-killing. It is required by every project type — embedded/drone projects need it for device fleet health and telemetry-ingestion throughput, ML-video/ML-text projects need it for GPU utilization and inference latency, and generic backend projects need it for standard service health (Postgres, Redis, Traefik, etc.).

## 2. Scope

**In scope:**
- Prometheus server for metrics scraping, storage (local TSDB), and PromQL querying.
- Grafana for dashboards, per-project folders, and datasource management (Prometheus + Loki).
- Grafana Loki for log aggregation; Promtail as the log-shipping agent on every VM/LXC.
- Alertmanager for alert deduplication, grouping, silencing, and routing (email/webhook to start).
- node_exporter (host metrics) on every VM/LXC in the fleet.
- cAdvisor for per-container resource metrics on every Docker Compose host.
- `dcgm-exporter` for NVIDIA GPU telemetry on GPU-bearing hosts (SVC-23 model serving, other CUDA workloads).
- Central scrape configuration for every catalog service that exposes a `/metrics` endpoint.

**Out of scope:**
- Distributed tracing (SVC-19, Grafana Tempo — separate T3 requirements doc).
- Long-term metrics retention beyond 90 days (would require Thanos/Mimir/Cortex — not adopted at current scale).
- Synthetic/uptime probing beyond Alertmanager's own routing (Blackbox exporter may be added later as a T2 add-on, not required for v1).
- SIEM-grade log retention/compliance tooling.

## 3. Technology selection

- **Prometheus** (Apache-2.0) — metrics collection and TSDB. Originated at SoundCloud, Berlin, Germany; now governed by the CNCF, so it carries no single national corporate owner (🌍 neutral governance) ([Prometheus origin — SoundCloud](https://developers.soundcloud.com)).
- **Grafana** (AGPLv3) — dashboards and visualization. Created by Torkel Ödegaard, Sweden; Grafana Labs, the primary steward, has a dual Stockholm/NYC headquarters (🇪🇺-linked but not purely EU) ([Grafana Labs — Torkel Ödegaard](https://grafana.com/blog/authors/torkelo/)).
- **Grafana Loki** + **Promtail** (AGPLv3) — log aggregation and shipping, same Grafana Labs governance as above.
- **Alertmanager** (Apache-2.0) — same Prometheus-ecosystem governance as Prometheus itself.
- **`dcgm-exporter`** (Apache-2.0) — NVIDIA's own GPU telemetry exporter. 🚩 This is US-origin (NVIDIA Corporation) and there is no viable European or vendor-neutral substitute for NVIDIA-specific GPU metrics (SM utilization, memory bandwidth, ECC errors, NVLink throughput); it is included pragmatically and flagged per the catalog's sourcing policy. It is only deployed on the GPU host(s) backing SVC-23.
- **Alternatives considered and rejected:** VictoriaMetrics was considered as a Prometheus-compatible, more resource-efficient TSDB, but Prometheus + Grafana is kept as default because it is the de facto standard, has first-class support from every other catalog service (`/metrics` conventions, Helm/Compose examples), and the team already has operational familiarity with it. No re-litigation of this choice is intended here — see catalog §3.E for the full rationale.

## 4. Multi-tenancy model

**Shared platform instance.** One Prometheus, one Grafana, one Loki, one Alertmanager serve every project — consistent with catalog §1's guidance that monitoring is a "single source of truth" shared service. Isolation is logical, not physical:
- **Grafana:** one **folder per project** (`Project: <slug>`), with folder-level RBAC via Grafana teams mapped to ZITADEL groups (SVC-06 OIDC integration). Dashboards are provisioned as code (JSON + `dashboard-provisioning.yaml`) per project under `dashboards/<slug>/`.
- **Prometheus/Loki labeling:** every scrape target and log stream carries a `project=<slug>` label, injected via Promtail's `relabel_configs` and Prometheus's `relabel_configs`/`external_labels` at the job level. This label is mandatory in every alert rule and dashboard query (`{project="<slug>"}`) so that Grafana dashboard variables can filter per project without needing separate Prometheus instances.
- **Alertmanager routing:** routes fan out by `project` label to project-specific receivers (Slack channel/webhook/email per project) while falling back to a platform-wide default receiver for infra-level alerts (host down, disk full).
- A project needing hard tenant isolation (e.g., a client-facing compliance requirement) can be given a **dedicated Prometheus + Grafana** pair as an exception, but this is not the default and must be justified in that project's own requirements doc.

## 5. Functional requirements

- **FR-1:** Prometheus scrapes every catalog service exposing `/metrics` on a documented interval (default 15s, 30s for low-churn exporters) and retains raw samples for 30 days on local disk.
- **FR-2:** node_exporter runs on every Proxmox VM/LXC in the fleet, exposing host CPU/memory/disk/network metrics on port 9100.
- **FR-3:** cAdvisor runs on every Docker Compose host, exposing per-container CPU/memory/network/block-IO metrics on port 8080.
- **FR-4:** `dcgm-exporter` runs on GPU-bearing hosts, exposing NVIDIA GPU metrics (utilization, memory, temperature, power, ECC errors) on port 9400.
- **FR-5:** Promtail tails Docker container logs (via the `docker` service discovery mechanism) and systemd journal logs on every host, shipping to Loki with `project`, `service`, and `host` labels attached.
- **FR-6:** Grafana provides pre-built dashboards for: host health (node_exporter), container health (cAdvisor), GPU health (dcgm-exporter), and per-service dashboards for every T1/T2 catalog service.
- **FR-7:** Alertmanager defines baseline alert rules: instance down (`up == 0` for 5m), disk >85% full, container OOM-killed, certificate expiry <14 days (from SVC-08), GPU ECC error rate nonzero.
- **FR-8:** All scrape targets are registered via Prometheus file-based service discovery (`file_sd_configs`), with target files generated by Ansible from the Terraform-produced dynamic inventory (F4) — no manual `prometheus.yml` edits.
- **FR-9:** Grafana authenticates via OIDC against ZITADEL (SVC-06); no local Grafana admin accounts except a break-glass superadmin.
- **FR-10:** Loki enforces per-tenant (per-project label) query and ingestion rate limits to prevent one noisy project from starving others' log queries.

## 6. Non-functional requirements

- **Availability target:** 99% for the stack itself (best-effort monitoring, not five-nines critical-path infrastructure); Prometheus/Grafana/Loki outages must not take down any monitored service.
- **Performance/sizing** (small homelab/office cluster, ~15–25 scrape targets, ~5–10 GB/day log volume):
  - Prometheus: 2 vCPU, 4 GB RAM, 100 GB disk (30-day retention at this scale comfortably fits within tens of GB with default 15s scrape interval).
  - Grafana: 1 vCPU, 1 GB RAM, 10 GB disk.
  - Loki (monolithic mode — appropriate below ~100k log lines/sec, which this cluster is far under): 2 vCPU, 2–4 GB RAM, 200 GB disk for 30-day log retention ([Loki sizing guidance](https://invgate.com/itdb/loki)).
  - Alertmanager: 1 vCPU, 512 MB RAM, 5 GB disk.
- **Backup/DR:** Prometheus TSDB and Grafana SQLite/Postgres config DB are covered by SVC-32 (Proxmox Backup Server VM-level snapshots); Grafana dashboards are additionally version-controlled as JSON in the platform Git repo (belt-and-suspenders — dashboards are code, not just DB rows).
- **Data retention:** metrics 30 days local; logs 30 days in Loki, with optional cheap long-term archival to SVC-03 (Garage/S3) via Loki's object-storage backend if longer retention is later required.

## 7. Infrastructure architecture

- **Compute unit:** one Proxmox **VM** (not LXC) hosting Prometheus + Grafana + Loki + Promtail + Alertmanager as a single Docker Compose stack, per F2/F3. VM chosen over LXC because Loki and Prometheus both benefit from predictable cgroup/kernel behavior under sustained write load, and because it simplifies future migration to a dedicated Proxmox node if the stack outgrows shared hosting.
- **Minimum resource spec:** 4 vCPU, 8 GB RAM, 400 GB disk (local-zfs, thin-provisioned) — sums the per-component sizing above with headroom.
- **Network placement:** dedicated "infra-mgmt" VLAN; static DHCP reservation. Reachable from all other service VLANs for scraping (Prometheus pulls) and log shipping (Promtail pushes), but the Grafana/Prometheus/Alertmanager web UIs are only exposed externally through Traefik (SVC-09) with TLS.
- **Storage:** local-zfs for Prometheus/Loki data directories (fast local NVMe preferred for Loki's index/chunk write path); Grafana's SQLite state DB also on local-zfs, backed up nightly.
- **node_exporter/cAdvisor/dcgm-exporter placement:** these run as sidecar containers (or host-level systemd units for node_exporter where LXC networking prevents container-level host metrics) on *every* VM/LXC in the fleet, not just the observability VM.

## 8. Terraform scope

- **Module inputs:** `vm_name`, `vlan_id`, `cpu_cores` (4), `memory_mb` (8192), `disk_gb` (400), `ip_address` (static reservation), `ssh_public_key`.
- **Resources created:** one `proxmox_virtual_environment_vm` resource (via `bpg/proxmox`) for the observability VM; firewall rules opening 9090 (Prometheus, internal only), 3000 (Grafana, via Traefik), 3100 (Loki push API, internal only), 9093 (Alertmanager, internal only) to the infra VLAN.
- **Outputs:** `observability_vm_ip`, `prometheus_url` (internal), `loki_push_url` (internal, consumed by every other service's Promtail config), `grafana_url` (external, via Traefik).

## 9. Ansible scope

- **Roles:** `observability_stack` (renders `docker-compose.yml`, `prometheus.yml`, `loki-config.yaml`, `alertmanager.yml`), `promtail_agent` (applied to every host in the fleet), `node_exporter` (applied to every host), `cadvisor` (applied to every Docker host), `dcgm_exporter` (applied only to GPU-tagged hosts).
- **Idempotency:** all config files are Jinja2 templates re-rendered on every run; Compose stack is brought up with `docker compose up -d` guarded by a content hash check to avoid unnecessary restarts.
- **Templated config files:** `prometheus.yml` (scrape jobs generated from inventory group `all` with `project` labels from host vars), `loki-config.yaml`, `alertmanager.yml` (routing tree generated from a `project_receivers` inventory variable), Grafana provisioning YAML for datasources and dashboard folders.
- **Secrets injected from SVC-07:** Grafana OIDC client secret (ZITADEL), Alertmanager webhook/SMTP credentials, Loki S3 archival credentials (if enabled) — all pulled from OpenBao at Ansible run time via the `community.hashi_vault` lookup plugin (OpenBao is Vault-API-compatible).

## 10. CI/CD pipeline

- **Stages:** `lint` (yamllint + `promtool check config` + `promtool check rules`) → `plan` (Terraform plan against the observability VM module) → `manual approve` → `apply` (Terraform apply) → `configure` (Ansible playbook run) → `smoke test` (curl `/-/healthy` on Prometheus, `/api/health` on Grafana, `/ready` on Loki, `/-/healthy` on Alertmanager).
- **Where it runs:** GitLab CI, using GitLab Runner (SVC-13).
- **State backend:** GitLab-managed Terraform state per F1, one state file scoped to the `observability` module.

## 11. Secrets & credentials

- Grafana OIDC client ID/secret (registered in ZITADEL) — generated once during SVC-06 setup, stored in OpenBao at `secret/observability/grafana-oidc`, rotated annually or on suspected compromise.
- Alertmanager notification channel credentials (SMTP password, Slack webhook URL) — stored in OpenBao at `secret/observability/alertmanager`.
- No database credentials required (Prometheus/Loki use local disk, not an external DB, at this scale).
- Grafana admin break-glass password — stored in OpenBao, rotated after each use.

## 12. Security & hardening baseline

- All external access (Grafana UI) via Traefik (SVC-09) with TLS termination and HTTP→HTTPS redirect; Prometheus, Loki, and Alertmanager UIs/APIs are **not** exposed externally, only reachable within the infra VLAN.
- Grafana authentication exclusively via ZITADEL OIDC (SVC-06); anonymous access disabled.
- Least-privilege service accounts: Promtail/node_exporter/cAdvisor run as unprivileged containers where the exporter allows it; cAdvisor requires read access to `/var/lib/docker` and `/sys/fs/cgroup` (documented, unavoidable exception).
- Firewall/VLAN rules: only the infra VLAN may reach Prometheus (9090), Loki push API (3100), and Alertmanager (9093); Grafana (3000) is reachable only from the Traefik VM.
- CVE scanning: container images pinned to specific tags (not `:latest`) and rebuilt/rescanned monthly via Harbor's (SVC-14) Trivy integration.

## 13. Observability hooks

- This service *is* the observability hook target for every other service — see FR-1 through FR-10 above.
- Self-monitoring: Prometheus scrapes its own `/metrics`, Grafana exposes `/metrics`, Loki exposes `/metrics`, Alertmanager exposes `/metrics` — all fed back into the same Prometheus instance ("meta-monitoring") so the stack can alert on its own failure.
- Key alerts to define in Alertmanager: `PrometheusTargetMissing`, `LokiRequestErrors`, `GrafanaDown`, `DiskSpaceLow` on the observability VM itself, `TSDBCompactionsFailing`.

## 14. Acceptance criteria

- [ ] Prometheus, Grafana, Loki, Promtail, and Alertmanager are running as a Compose stack on the observability VM and pass their respective health-check endpoints.
- [ ] node_exporter, cAdvisor are deployed fleet-wide via Ansible and appear as `up=1` targets in Prometheus.
- [ ] `dcgm-exporter` is deployed on the GPU host(s) and GPU metrics are visible in a Grafana dashboard.
- [ ] At least one project-scoped Grafana folder exists with `project=<slug>` filtering working end-to-end (dashboard variable → PromQL/LogQL query → correct data).
- [ ] Grafana login works via ZITADEL OIDC; no default admin/admin login is reachable externally.
- [ ] Alertmanager fires a test alert to the platform default receiver and to at least one project-specific receiver.
- [ ] Terraform plan/apply and Ansible playbook run cleanly (idempotent second run produces no changes).

## 15. Open questions / assumptions

- Assumed 30-day metrics/log retention is sufficient; if compliance or incident-forensics needs push this to 90+ days, Loki object-storage archival to SVC-03 should be revisited.
- Assumed single-node Prometheus/Loki (no HA/clustering) is acceptable at this scale — a human should confirm this trade-off is acceptable given the "99% best-effort" availability target.
- Assumed Slack or email is an acceptable Alertmanager receiver; specific webhook URLs/SMTP relay to be confirmed per project before go-live.
- The exact `dcgm-exporter` version/tag should be pinned to match the NVIDIA driver version installed on the GPU host at SVC-23 build time — a human should confirm the driver version before finalizing the tag.
