# SVC-01 — Relational (SQL) Database — Terraform root (reusable, per-project)

This is the **reusable** Terraform root for **SVC-01**, the platform's standard
relational database (PostgreSQL by default, MariaDB as a strict-EU alternative),
provisioned **dedicated-per-project** (domain-rules §3). The provision-then-test
runbook is [`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md); the spec lives at
[`.kiro/specs/svc-01-sql-database/`](../../../.kiro/specs/svc-01-sql-database/).

## Reusable, per-project

Unlike the shared VLAN-20 service roots, this root is **parameterized** by
`project_slug` + `vlan_id`, so the SAME code provisions `dronefleet`'s DB on VLAN
100 and ZITADEL's backing DB on VLAN 20. Each project **instance** gets its own
GitLab-managed state name `<slug>-svc01-postgres` (supplied at `terraform init`
via `-backend-config`), so a `terraform destroy` for one project's DB can never
touch another's (`integration-boundaries.md` §5).

## What this root creates

Exactly **one** `proxmox_virtual_environment_container` — the database LXC —
addressed by the shared [`proxmox-compute`](../../modules/proxmox-compute/)
module:

| Attribute | Value |
|---|---|
| Hostname | `svc01-<vlan>-01` (computed) |
| IP | `10.0.<vlan>.1X` (computed, `host_index`) |
| Tags | `proj-<slug>-postgres` (or `-mariadb`) |
| Size | `tier=small` (1 vCPU/2 GB/20 GB) or `tier=medium` (4/8192/100), overridable |
| NIC | `bridge = p<vlan>` (project VNet applies the tag; no NIC-level `vlan_id`) |
| State name | `<slug>-svc01-postgres` (per-instance, GitLab-managed) |

Plus an **opt-in, default-off** `proxmox_virtual_environment_firewall_rules`
scoping inbound 5432 to the project subnet (`enable_firewall_rules`,
count-gated) — defence-in-depth, reversible per the ADR-0008 posture. The DB LXC
carries `lifecycle.prevent_destroy` (a stateful store must not be silently
replaced).

## Inputs (`variables.tf`)

`project_slug`, `vlan_id` (20 or 100–254), `host_index`, `db_engine`
(`postgres`/`mariadb`), `tier` (+ `cpu_cores`/`memory_mb`/`disk_gb` overrides),
`proxmox_node_name`, `svc01_template_file_id`/`os_type`/`datastore`, `svc01_vmid`,
`enable_pgvector`/`enable_postgis`/`enable_timescaledb`, `app_subnet_cidr`,
`enable_firewall_rules` (default false), `operator_ssh_public_keys`. No IP literal
(computed).

## Outputs

`db_host`, `db_port` (5432/3306 by engine), `db_engine`, and the generic
`inventory_hosts` list. No secret is ever an output.

## Ansible layer

The 4-phase installer [`svc-01-bootstrap.yml`](../../../ansible/playbooks/svc-01-bootstrap.yml)
(roles `svc01_preflight/_provision/_bootstrap/_handoff/_clean_slate`) drives the
provisioning; the DB itself is configured by the
[`postgres-server`](../../../ansible/roles/postgres-server/) role (or the
[`mariadb-server`](../../../ansible/roles/mariadb-server/) skeleton) via the
[`postgres.yml`](../../../ansible/playbooks/postgres.yml) configurator play, which
also enables requested extensions, schedules the nightly `pg_dump`, and stores
the generated passwords in OpenBao. No Traefik — PostgreSQL is wire-protocol;
5432 is bound only to the guest's private VLAN IP (the documented
`docker-compose-app` exception).
