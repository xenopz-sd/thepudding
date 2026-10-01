# `sdn_gateway` role

Host-level Ansible role for the Developer Services Platform. It configures the
**Proxmox host itself** as the Layer-3 gateway + firewall for served SDN VLANs,
implementing [ADR-0004](../../../.kiro/decisions/0004-ansible-control-node-placement-and-sdn-reachability.md)
(built under
[`.kiro/specs/sdn-vlan-gateway-reachability/`](../../../.kiro/specs/sdn-vlan-gateway-reachability/)).

A Proxmox **SDN VLAN zone is Layer-2 only** — nothing on the host answers at the
subnet's advertised gateway (`10.0.<vlan_id>.1`), so an off-VLAN control host
cannot reach the guests on it (a connect timeout, not a credential failure).
This role materialises the missing L3 by giving the host the gateway address on
the **SDN VNet bridge `p<vlan_id>`** (the interface the guests attach to — a
`vmbr0.<vlan>` sub-interface would be the wrong L2 segment; see the ADR-0004
addendum), enables `net.ipv4.ip_forward`, adds a scoped egress masquerade so the
guests can reach the internet, and scopes inbound access with `pve-firewall`.
That host L3 config is deliberately **outside** the
Terraform-managed SDN — the recorded L2 (Terraform/SDN) / L3 (host-managed) seam
of ADR-0004. No `.tf` file gains a gateway resource.

It is a peer of the `common` baseline role — a host-level role, not a Compose
stack — which is the `structure.md`-sanctioned host-level exception.

## What it does

- Enables `net.ipv4.ip_forward=1` persistently and verifies the live value
  (mirrors the `common`/`openbao_init_unseal` sysctl set-then-verify precedent).
- For each **served** VLAN (the always-served shared VLAN 20, plus any opted-in
  active project VLAN), ensures the gateway address `10.0.<vlan_id>.1/24` is
  present on the SDN VNet bridge `p<vlan_id>` (persistently), a **scoped**
  `pve-firewall` ALLOW rule (admin source CIDR → that VLAN on the allowed ports
  only), and a **scoped egress masquerade** (internet-bound only — `! -d
  10.0.0.0/16` excludes inter-VLAN, so isolation and source-IP attribution are
  preserved) so the guests can reach the internet for `apt` / Docker pulls.
- For each **decommissioned** VLAN, removes that VLAN's gateway address, its
  `interfaces.d` stanza, its ALLOW rule, and its egress masquerade (the teardown
  direction) — but not the SDN-owned `p<vlan_id>` bridge itself — so a retired
  project retains no live gateway, opening, or egress.
- Keeps the standing isolation DENY rules in force: served/project VLANs →
  management VLAN (10) DENY, and cross-project VLAN→VLAN DENY.

Idempotent in both directions: a second run against a converged host reports
`changed=0`, and a re-run after a `status: active → decommissioned` flip
converges the removal and then reports no further changes.

Gateway persistence uses a templated `/etc/network/interfaces.d/` stanza
(`templates/interfaces.d-vlan-gateway.j2`) whose `post-up`/`pre-down` hooks
re-add/remove the gateway address AND the egress masquerade on the SDN-owned
`p<vlan_id>` bridge, so both survive a host reboot without redeclaring the bridge.
The `pve-firewall` ALLOW/DENY rules are written idempotently via `sudo pvesh`
(check-then-change against `/cluster/firewall/rules`, keyed on a managed comment
tag). The role does **not** use `ifup --no-act` to pre-validate the stanza — that
dry-run is broken on Proxmox's ifupdown2 (see the ADR-0004 addendum); instead the
guarded, check-then-change `ip addr add` / masquerade steps surface a genuine
failure loudly, so a half-configured/half-open host is never left behind.

## Variables

Defined in `defaults/main.yml` (all `sdn_gateway_*`-prefixed; none carry a
secret):

- `sdn_gateway_bridge` — the underlying LAN bridge the SDN VLAN zone tags onto
  (default `vmbr0`); also the default egress interface. The per-VLAN gateway
  address is placed on the SDN VNet bridge `p<vlan_id>`, not on this bridge.
- `sdn_gateway_admin_source_cidr` — **required**; source network permitted to
  reach served VLANs. Empty (the default) makes the role **fail closed** rather
  than open a wide rule.
- `sdn_gateway_allowed_ports` — ports opened from the admin source (default
  `[22, 80, 443]`).
- `sdn_gateway_shared_vlan_id` — the always-served shared VLAN (default `20`).
- `sdn_gateway_served_project_vlan_ids` — opt-in list of project VLANs (100+) to
  serve (default `[]`; serving a project VLAN is deliberate, never blanket).
- `sdn_gateway_active_vlan_ids` / `sdn_gateway_decommissioned_vlan_ids` —
  caller-supplied desired-state / teardown sets, derived from `projects.yaml` by
  the JSON emitter and fed via `--extra-vars` (default `[]`).
- `sdn_gateway_management_vlan_id` — management VLAN that must stay unreachable
  (default `10`).

## Usage

The role is applied via the bind-only `ansible/playbooks/sdn-gateway.yml`
playbook against the static `ansible/inventory/proxmox-hosts.yml` inventory. All
commands invoke the project venv binaries by absolute path (`dev-workflow.md`).

Offline preflight — parse the whole task graph and prove the host group
resolves (no infrastructure needed):

<!-- doctest: offline cwd: . -->
```bash
~/venv/devinfra/bin/ansible-playbook --syntax-check \
  -i ansible/inventory/proxmox-hosts.yml \
  ansible/playbooks/sdn-gateway.yml
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/proxmox-hosts.yml \
  ansible/playbooks/sdn-gateway.yml --list-hosts
```

> Note: `--syntax-check` in ansible-core is structural only and does **not**
> resolve FQCNs — `ansible.posix.sysctl` correctness is guarded by the
> `ansible-lint` `fqcn` rule, not by `--syntax-check`.

Live converge against the Proxmox host — feed the desired-state sets from the
`projects.yaml` JSON emitter plus the admin source CIDR (requires the live
Proxmox node and credentials):

<!-- doctest: requires-infra -->
```bash
~/venv/devinfra/bin/ansible-playbook \
  -i ansible/inventory/proxmox-hosts.yml \
  ansible/playbooks/sdn-gateway.yml \
  --extra-vars "sdn_gateway_admin_source_cidr=<dev-net-cidr>"
```

Placeholders (`<proxmox-lan-ip>` in the inventory, `<dev-net-cidr>` above) are
never real IPs. The developer static-route step and the full provision-then-test
walkthrough live in `TESTING.md` and the SVC-07 `INSTALL-RUNBOOK.md`.

## Firewalled-guest egress baseline (applies to EVERY firewalled guest)

Enabling a guest's per-NIC firewall (`firewall=1` on its `network_interface`, with
`pve-firewall` enabled) makes Proxmox filter **that guest's own traffic**. With no
guest-scoped rules the guest loses **egress**: DNS, `apt update`, and container-image
pulls all fail, so it cannot provision itself. This bit the SVC-07 Transit unsealer —
`apt update` failed and Docker was never installed, while the non-firewalled primary
provisioned fine.

`tasks/guest_firewall_baseline.yml` is the reusable fix. For every guest listed in
`sdn_gateway_firewalled_guests` it ensures scoped `out` ALLOW rules at that guest's
own firewall scope:

| Proto | Ports (default) | Why |
|---|---|---|
| tcp | 53, 80, 443 | DNS/TCP, apt repositories, apt-over-TLS + container registries |
| udp | 53, 123 | DNS, NTP (TLS validity and token TTLs depend on the clock) |

It is an explicit, reviewable **port allowlist** — never a blanket
`policy_out=ACCEPT` — and opens **no guest ingress**. It runs BEFORE any
guest-scoping rules, because a guest needs egress to be provisioned at all.

### Using it for a new guest / service / VLAN

Nothing service-specific is required: add an entry (node + vmid from the
Terraform-generated inventory, never hardcoded) and the guest gets the baseline.

```yaml
sdn_gateway_firewalled_guests:
  - { name: svcNN-<vlan>-01, node: "<proxmox-node>", vmid: <vmid> }
```

Widen the allowlist only deliberately, via
`sdn_gateway_guest_egress_tcp_ports` / `sdn_gateway_guest_egress_udp_ports`. An entry
missing `node` or `vmid` fails closed rather than writing rules to an unknown guest.
