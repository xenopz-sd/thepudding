# SVC-06 — Identity Provider / SSO (ZITADEL) — Terraform root

This is the Terraform root for **SVC-06**, the platform's shared **ZITADEL**
identity provider (OIDC/SAML SSO). The provision-then-test runbook is
[`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md); the spec lives at
[`.kiro/specs/svc-06-identity-provider/`](../../../.kiro/specs/svc-06-identity-provider/).

## Shared platform service

ZITADEL is a **shared** platform service on VLAN 20 (one instance for the whole
platform); per-project isolation is **logical** via ZITADEL organizations
(`proj-<slug>`), not a per-project VLAN. State name `svc-06-identity-provider`.

## What this root creates

Exactly **one** `proxmox_virtual_environment_container` — the ZITADEL LXC —
addressed by the shared [`proxmox-compute`](../../modules/proxmox-compute/)
module. It does **NOT** create the backing database:

| Attribute | Value |
|---|---|
| Hostname | `svc06-20-01` (computed) |
| IP | `10.0.20.15` (computed, `zitadel_host_index=5`) |
| Tags | `proj-shared-zitadel` |
| Size | 2 vCPU / 2048 MB / 10 GB |
| NIC | `bridge = p20` (VLAN-zone VNet applies the tag; no NIC-level `vlan_id`) |
| State name | `svc-06-identity-provider` (GitLab-managed) |

The ZITADEL LXC carries `lifecycle.prevent_destroy` (it holds masterkey-encrypted
identity state and is a hard dependency for platform-wide login).

## The backing database is a SEPARATE SVC-01 instance

ZITADEL's PostgreSQL is **not** declared in this root. It is a dedicated
[SVC-01](../svc-01-sql-database/) instance (`project_slug=shared-zitadel`,
`db_engine=postgres`, VLAN 20, `host_index=4` → `10.0.20.14`) with its **own
isolated state** `shared-zitadel-svc01-postgres`. The installer's
`svc06_provision` role stands it up by running `svc-01-bootstrap.yml` as a child
**before** this root's apply. Folding the DB into this root would collapse
SVC-01's per-instance state isolation (`integration-boundaries.md` §5), so this
root owns only the ZITADEL guest.

## Inputs (`variables.tf`)

`proxmox_node_name`, `zitadel_template_file_id`/`os_type`/`datastore`,
`zitadel_vmid` (required), `zitadel_host_index` (default 5), `zitadel_version`
(image-tag echo), `platform_domain`, `operator_ssh_public_keys`. No IP literal
(computed). No `db_*` inputs (the DB is a separate SVC-01 instance).

## Outputs

`zitadel_host`, `zitadel_http_port` (8080), `zitadel_grpc_port` (8081),
`zitadel_issuer_url` (`https://auth.<platform_domain>`), and the generic
`inventory_hosts` list (ZITADEL guest only). No secret is ever an output.

## Ansible layer

The 4-phase installer [`svc-06-bootstrap.yml`](../../../ansible/playbooks/svc-06-bootstrap.yml)
(roles `svc06_preflight/_provision/_bootstrap/_handoff/_clean_slate`) drives the
provisioning; ZITADEL itself is configured by the
[`zitadel-server`](../../../ansible/roles/zitadel-server/) role via the
[`zitadel.yml`](../../../ansible/playbooks/zitadel.yml) configurator play
(masterkey + DB creds from OpenBao, Traefik labels, metrics). Project SSO
onboarding is idempotent via
[`zitadel-org-provision`](../../../ansible/roles/zitadel-org-provision/) +
[`scripts/installer/zitadel_provision.py`](../../../scripts/installer/zitadel_provision.py).

HTTP ingress is via **Traefik labels only** — ZITADEL publishes no host port. In
dev/offline (no Traefik) the two default-off mocks `zitadel_tls_self_signed` +
`zitadel_mock_edge_network` let the stack serve HTTPS + attach an `edge` network
([`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md) Mock 5 + Mock 6).
