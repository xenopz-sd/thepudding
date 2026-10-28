# SVC-17/18 — Observability Stack (Prometheus + Grafana + Loki) — Terraform root

This is the Terraform root for **SVC-17/18 (Observability)**, the platform's
shared metrics (Prometheus + Alertmanager), dashboards (Grafana), and log
aggregation (Loki) stack. It is the documentation entry point for the service
(`structure.md`): the provision-then-test runbook is
[`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md); the spec lives at
[`.kiro/specs/svc-17-18-observability/`](../../../.kiro/specs/svc-17-18-observability/).

## What this root creates

Exactly **three** `proxmox_virtual_environment_container` guests — one per
component — all on the shared-services VLAN (20):

| Attribute | Prometheus (+ Alertmanager) | Grafana | Loki |
|---|---|---|---|
| Hostname | `svc17-prometheus-20-01` | `svc17-grafana-20-01` | `svc18-loki-20-01` |
| IP | `10.0.20.18` (`cidrhost("10.0.20.0/24", 10 + 8)`; `host_index 8`) | `10.0.20.19` (`host_index 9`) | `10.0.20.20` (`host_index 10`) |
| Tags | `proj-shared-prometheus` | `proj-shared-grafana` | `proj-shared-loki` |
| Size | 3 vCPU / 4.5 GB / 100 GB (`local-zfs`; +1 vCPU/512 MB for co-located Alertmanager) | 1 vCPU / 1 GB / 10 GB | 2 vCPU / 4 GB / 200 GB |
| NIC | `bridge = p20` (VLAN-zone VNet applies tag 20; no NIC-level `vlan_id`) | `bridge = p20` | `bridge = p20` |

All three guests share one **state name** `svc-17-18-observability`
(GitLab-managed, PF FR-2).

The hostname/IP/tags of every guest come from the shared
[`proxmox-compute`](../../modules/proxmox-compute/) module (consumed unmodified)
via three `module` blocks (`host_index` 8/9/10); the guest resources and sizing
live here. The three `host_index` values and each guest's `peer_host_indices`
set are read from the committed host-index ledger
([`host-indices.yaml`](../../platform-foundation/host-indices.yaml), the
Host_Index_Authority, ADR-0015) — never hand-chosen — and the module's
plan-time `peer_host_indices` precondition fails closed on any within-VLAN
collision. There is **no `sdn.tf`** — shared VLAN-20 services reference the
pre-existing `p20`, they do not create a VNet.

A `lifecycle.prevent_destroy` seatbelt guards the **Prometheus** and **Loki**
guests (their rootfs holds the local TSDB / log index + chunks — stateful data
an accidental apply must never replace). **Grafana carries no seatbelt**: it is
config-as-code (dashboards are version-controlled JSON, datasources/auth
re-provisioned by Ansible), so the guest is fully reproducible.

## Inputs (`variables.tf`)

`proxmox_node_name`; per-guest `{prometheus,grafana,loki}_template_file_id`
(regex-guarded stock LXC template), `_template_os_type` (`debian`/`ubuntu`),
`_datastore_id` (default `local-zfs`), and `_vmid`; `operator_ssh_public_keys`
(list, default `[]` — public keys only); and `platform_domain` (non-secret,
used by Ansible to build the Grafana Traefik Host rule). There is deliberately
**no IP-literal input**: every address is module-computed.

## Outputs (`outputs.tf`)

`prometheus_internal_ip`, `grafana_internal_ip`, `grafana_url` (external via
Traefik — `https://grafana.<platform_domain>`), `loki_internal_ip`,
`loki_push_url` (internal Loki push endpoint every other service's Promtail
ships to), and the generic `inventory_hosts` list (one object per guest, same
schema as SVC-07/09 — `{hostname, ipv4_address, service_role, project_slug,
vmid, node_name}`) the Ansible inventory generator keys on. No secret is ever an
output.

## Ansible layer

Provisioning (`terraform apply`) is driven by the 4-phase installer
[`ansible/playbooks/svc-17-18-bootstrap.yml`](../../../ansible/playbooks/svc-17-18-bootstrap.yml)
(roles `obs_{preflight,provision,bootstrap,handoff,clean_slate}`). The stacks are
configured by the `observability_stack` role (per-guest Compose + `prometheus.yml`
with `file_sd_configs` only, `loki-config.yaml`, `alertmanager.yml`, Grafana
provisioning) plus the fleet exporter roles (`promtail_agent`, `node_exporter`,
`cadvisor`, and `dcgm_exporter` on GPU-tagged hosts only), all via the generic
`docker-compose-app` role. Scrape targets are never hand-edited — the target
files are generated from the Terraform-produced dynamic inventory, and the v1
scrape set is the four built services (SVC-01/06/07/09).

## Mock stand-ins (SVC-08 / SVC-06 OIDC client / GPU host not present yet)

Three permanent, default-off/deferred mocks let the stack come up and be
validated before its dependencies exist (see
[`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md)):
`grafana_oidc_enabled` (default off — Break-glass admin until a ZITADEL Grafana
OIDC client is registered in SVC-06), `obs_tls_self_signed` (default off —
self-signed cert stand-in for SVC-08/Let's Encrypt), and `dcgm_metrics_deferred`
(default on — the `dcgm_exporter` role applies to zero hosts until a GPU host
lands with SVC-23). Each fails closed when its stand-in is off and the real
dependency is absent, never silently degrading.

## Terraform state isolation

This root has its own GitLab-managed state name `svc-17-18-observability`,
distinct from `platform-foundation`, every `<slug>-infra`, and every other
service root — so a `terraform destroy` here touches only the three
observability guests (`integration-boundaries.md` §5). The `terraform@pve` role
needs `SDN.Allocate` + `SDN.Use` (owned by platform-foundation) to attach the
guest NICs to `p20`.
