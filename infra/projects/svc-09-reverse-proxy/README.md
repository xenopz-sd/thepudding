# SVC-09 — Reverse Proxy / TLS Termination (Traefik) — Terraform root

This is the Terraform root for **SVC-09 (Traefik)**, the platform's single shared
HTTP/HTTPS ingress and TLS-termination edge. It is the documentation entry point
for the service (`structure.md`): the provision-then-test runbook is
[`INSTALL-RUNBOOK.md`](./INSTALL-RUNBOOK.md); the spec lives at
[`.kiro/specs/svc-09-reverse-proxy/`](../../../.kiro/specs/svc-09-reverse-proxy/).

## What this root creates

Exactly **one** `proxmox_virtual_environment_container` — the Traefik LXC on the
shared-services VLAN (20):

| Attribute | Value |
|---|---|
| Hostname | `svc09-20-01` (computed by `proxmox-compute`) |
| IP | `10.0.20.12` (computed — `cidrhost("10.0.20.0/24", 10 + 2)`; `host_index 2`, moved from the colliding `0`/`.10` per the host-index ledger) |
| Tags | `proj-shared-traefik` |
| Size | 1 vCPU / 512 MB / 4 GB (`local-zfs`) |
| NIC | `bridge = p20` (VLAN-zone VNet applies tag 20; no NIC-level `vlan_id`) |
| State name | `svc-09-reverse-proxy` (GitLab-managed, PF FR-2) |

The hostname/IP/tags come from the shared
[`proxmox-compute`](../../modules/proxmox-compute/) module (consumed unmodified);
the guest resource and sizing live here. There is **no `sdn.tf`** — shared VLAN-20
services reference the pre-existing `p20`, they do not create a VNet.

The `acme.json` certificate store lives on the LXC rootfs (persistent, never
tmpfs), and a `lifecycle.prevent_destroy` seatbelt stops an accidental apply from
replacing the LXC and discarding issued certificates.

## Inputs (`variables.tf`)

`proxmox_node_name`, `traefik_template_file_id` (regex-guarded stock LXC
template), `traefik_template_os_type` (`debian`/`ubuntu`), `traefik_datastore_id`
(default `local-zfs`), `traefik_vmid`, `letsencrypt_email` (non-secret, consumed
by Ansible), `operator_ssh_public_keys` (list, default `[]` — public keys only).
There is deliberately **no IP-literal input**: the address is module-computed.

## Outputs (`outputs.tf`)

`traefik_internal_ip`, `traefik_dashboard_internal_endpoint`, and the generic
`inventory_hosts` list the Ansible inventory generator keys on. No secret is ever
an output.

## Ansible layer

Provisioning (`terraform apply`) is driven by the 4-phase installer
[`ansible/playbooks/svc-09-bootstrap.yml`](../../../ansible/playbooks/svc-09-bootstrap.yml)
(roles `svc09_preflight/_provision/_bootstrap/_handoff/_clean_slate`). The
service itself is configured by the
[`traefik_install`](../../../ansible/roles/traefik_install/) role via the
[`traefik.yml`](../../../ansible/playbooks/traefik.yml) configurator play, which:

- renders the static `traefik.yml` (entrypoints `web:80`/`websecure:443` + an
  internal `metrics` entrypoint; Docker provider `exposedByDefault:false`;
  read-only Docker socket; HTTP→HTTPS redirect; the `letsencrypt` ACME resolver),
- creates and **owns** the real external `edge` Docker network every HTTP service
  attaches to, and
- deploys the Traefik Compose stack via the generic `docker-compose-app` role.

## Mock stand-ins (SVC-08 / SVC-06 not built yet)

Two permanent, default-off mocks let Traefik come up and be validated before its
dependencies exist (see
[`mock-stand-ins.md`](../../../.kiro/steering/mock-stand-ins.md) Mock 3 + Mock 4):
`traefik_tls_self_signed` (self-signed cert, stand-in for SVC-08/Let's Encrypt)
and `traefik_dashboard_auth_disabled` (internal-only dashboard route, stand-in
for SVC-06 forward-auth). Production leaves both off.

## Terraform state isolation

This root has its own GitLab-managed state name `svc-09-reverse-proxy`, distinct
from `platform-foundation`, every `<slug>-infra`, and every other service root —
so a `terraform destroy` here touches only the Traefik LXC (`integration-boundaries.md`
§5). The `terraform@pve` role needs `SDN.Allocate` + `SDN.Use` (owned by
platform-foundation) to attach the guest NIC to `p20`.
