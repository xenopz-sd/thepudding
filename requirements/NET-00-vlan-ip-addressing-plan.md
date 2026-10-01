# Network Foundation — VLAN & IP Addressing Plan

## 1. Overview

This document resolves the three networking decisions that [`PF-platform-foundation-requirements.md`](./PF-platform-foundation-requirements.md) §7 left as illustrative examples and §15 explicitly deferred to a human/operator decision. It defines a concrete, deterministic VLAN allocation scheme, an IP addressing formula that Terraform computes automatically (no manual IP planning per project), a hostname convention that correlates a service to its VLAN, and the Proxmox networking implementation (Proxmox SDN, VLAN zone type) that makes per-project VLAN creation an ordinary `terraform apply` rather than a manual multi-node host-file edit. It is a companion to PF, at the same foundation layer, and every other `SVC-xx-*-requirements.md` doc's `vlan_tag`/network-placement fields should point back here rather than restating network conventions locally.

## 2. VLAN allocation scheme

| VLAN ID range | Purpose | Assignment | Notes |
|---|---|---|---|
| **10** | Management | Fixed, single VLAN | Proxmox host/corosync/API traffic only. No guest VM/LXC ever gets an interface on this VLAN — it never carries project or shared-service workload traffic (per PF §12's isolation rule: project VLANs cannot initiate connections to the management VLAN). |
| **20** | Shared platform services | Fixed, single VLAN | Every catalog service tagged "Shared" tenancy (SVC-06 ZITADEL, SVC-07 OpenBao, SVC-09 Traefik, SVC-13 CI runners, SVC-14 Zot, SVC-17/18 observability, etc.) lives here. |
| **30–99** | Reserved | Unallocated | Buffer for a future shared-tier addition (e.g. a public-edge/DMZ VLAN, a dedicated backup/storage VLAN) — deliberately not assigned yet, so a future need doesn't collide with the per-project range below. |
| **100+** | Per-project, dedicated | Sequential, auto-incrementing, never reused | Every project gets exactly **one** dedicated VLAN (not a shared "projects" VLAN) — this is the simpler mental model the catalog now standardizes on. The first project onboarded gets VLAN 100, the second gets 101, and so on. An ID is never reused even if a project is decommissioned, mirroring the existing "project slug assigned once, never reused" rule in PF §4. |

**Why one VLAN per project, not a shared pool:** a dedicated VLAN per project means every project's blast radius, firewall ruleset, and traffic pattern is trivially identifiable by VLAN ID alone — no per-project firewall-rule bookkeeping inside a shared VLAN is needed, and the isolation guarantee PF §12 already requires for the management VLAN extends naturally to every other project too, at zero extra modeling cost.

## 3. IP addressing (CIDR) scheme — fully automated, no per-project decision

**Subnet formula:** every VLAN gets a `/24` whose **third octet equals the VLAN ID**: `10.0.<vlan_id>.0/24`. This is deterministic and reversible by inspection — anyone looking at an IP already knows its VLAN ID without a lookup table.

| VLAN | Subnet | Gateway |
|---|---|---|
| 10 (management) | `10.0.10.0/24` | `10.0.10.1` |
| 20 (shared services) | `10.0.20.0/24` | `10.0.20.1` |
| 100 (project #1) | `10.0.100.0/24` | `10.0.100.1` |
| 101 (project #2) | `10.0.101.0/24` | `10.0.101.1` |
| … | `10.0.<vlan_id>.0/24` | `10.0.<vlan_id>.1` |

**Host IP assignment within a VLAN — computed by Terraform, not chosen by a human:**
- `.1` — gateway (the Proxmox SDN VNet's own subnet gateway, per `proxmox_sdn_subnet`'s `gateway` attribute).
- `.2`–`.9` — reserved buffer (a future per-VLAN jump host, NAT/firewall VIP, etc.) — not allocated to services.
- `.10` onward — every guest VM/LXC's static IP is computed as `cidrhost("10.0.${vlan_id}.0/24", 10 + index)`, where `index` is that host's position in Terraform's `for_each`/`count` over the VLAN's service list — i.e. the first host created in a VLAN gets `.10`, the second `.11`, and so on. This removes the "operator finalizes the IP allocation plan" step PF §15 flagged: nobody chooses an IP, Terraform derives it from creation order, and — per the reasoning that triggered this document — the literal number doesn't need to be memorized because internal DNS is the actual lookup mechanism.

```hcl
# Illustrative pattern only — not a full module.
locals {
  hosts_by_index = { for i, h in var.vlan_hosts : h.hostname => i }
}

resource "proxmox_virtual_environment_vm" "guest" {
  for_each = local.hosts_by_index
  # ...
  initialization {
    ip_config {
      ipv4 {
        address = "${cidrhost("10.0.${var.vlan_id}.0/24", 10 + each.value)}/24"
        gateway = cidrhost("10.0.${var.vlan_id}.0/24", 1)
      }
    }
  }
}
```

**Scale assumption:** this formula covers VLAN IDs 0–255 cleanly within a single octet. At the homelab/office scale PF §6 assumes (a handful of concurrent projects), that means up to ~155 projects (100–254) before the scheme needs a second-octet extension — flagged in §9 as a non-blocking future revision point, not a current gap.

## 4. Hostname convention

Two separate conventions serve two separate purposes — this was previously conflated in PF §4 and is now split out explicitly:

- **Proxmox `tags` attribute** (human-friendly, for UI/Ansible group_vars filtering) — unchanged from PF §4: `proj-<slug>-<service>` (e.g. `proj-dronefleet-postgres`).
- **Hostname / DNS record** (machine-facing, VLAN-correlated) — **new convention introduced here**: `<svc-code>-<vlan_id>-<instance>`, where:
  - `<svc-code>` is the catalog service's own ID in lowercase, e.g. `svc01` (SQL database), `svc14` (container registry), `svc09` (reverse proxy). Using the catalog's stable service-slot number — rather than a product name — means the hostname survives a future default-technology swap (exactly the kind of change just made for SVC-14 Harbor→Zot) without ever needing to be renamed.
  - `<vlan_id>` is the numeric VLAN ID from §2 (e.g. `20` for a shared service, `140` for a specific project's dedicated VLAN).
  - `<instance>` is a zero-padded two-digit counter, `01` for a singleton service, incrementing only if a service is ever scaled/replicated within the same VLAN.
  - If one catalog line item deploys more than one distinct running service (e.g. SVC-17/18 Prometheus + Grafana + Loki), append the component name before the instance number: `<svc-code>-<component>-<vlan_id>-<instance>` (e.g. `svc17-prometheus-20-01`).

**Worked example:** a project onboarded third gets VLAN 102 (`10.0.102.0/24`). Its dedicated Postgres (SVC-01) is `svc01-102-01` at `10.0.102.10`; its dedicated Redis (SVC-02) is `svc02-102-01` at `10.0.102.11`. The shared container registry (SVC-14, Zot, VLAN 20) is `svc14-20-01` at `10.0.20.10`. Nobody needed to choose either the VLAN ID or the IP for any of these — both fall out of the onboarding order and the formulas in §2/§3; only the hostname's readability is a deliberate human-facing design choice.

## 5. Proxmox implementation: VLAN-aware bridge vs. SDN — and why SDN is the right fit here

Proxmox offers two materially different ways to realize a VLAN on the hypervisor, and the choice matters for how per-project VLAN creation gets automated:

**Option A — VLAN-aware Linux bridge (traditional):** a single bridge (typically `vmbr0`) on each Proxmox node is marked `bridge-vlan-aware yes` in `/etc/network/interfaces`, and every VM/LXC's virtual NIC is simply tagged with a `vlan_id` attribute at the guest level. The physical uplink carries all VLANs as 802.1Q-tagged frames; the bridge is a dumb tagged switch with no separate control plane, per the [Proxmox VLAN configuration guide](https://www.virtualizationhowto.com/2023/12/proxmox-vlan-configuration-management-ip-bridge-and-virtual-machines/) and [Proxmox community forum discussion](https://forum.proxmox.com/threads/confused-about-linux-bridge-vlan-aware-and-default-vlan.145080/). This is simple and has existed forever, but **creating a new VLAN means hand-editing `/etc/network/interfaces` on every cluster node and reloading networking** — there is no Terraform resource that creates the VLAN itself, only one that tags a guest NIC with an ID that must already be permitted on the bridge/trunk.

**Option B — Proxmox SDN, VLAN zone type (chosen for this platform):** Proxmox's Software-Defined Networking layer introduces **zones** (Simple, VLAN, QinQ, VXLAN, EVPN) and **VNets**/**subnets** within a zone as first-class, API-managed objects — per the [official Proxmox VE SDN documentation](https://pve.proxmox.com/pve-docs/chapter-pvesdn.html) and the [zone-type comparison at datazone.de](https://datazone.de/en/aktuelles/proxmox-sdn-software-defined-networking/). A **VLAN zone** specifically "uses an existing local Linux or OVS bridge to connect to the node's physical interface" and uses VLAN tagging defined at the VNet level to isolate segments — i.e., the same 802.1Q mechanism as Option A underneath, but exposed as a declarative, cluster-wide-consistent object instead of a per-node config file, per the [bpg/proxmox provider's VLAN-zone data source docs](https://registry.terraform.io/providers/bpg/proxmox/latest/docs/data-sources/virtual_environment_sdn_zone_vlan). The `bpg/proxmox` Terraform provider models this directly with `proxmox_sdn_zone_vlan`, `proxmox_sdn_vnet`, `proxmox_sdn_subnet`, and a `proxmox_sdn_applier` resource that triggers Proxmox to apply pending SDN changes cluster-wide, per the [provider's SDN subnet resource docs](https://registry.terraform.io/providers/bpg/proxmox/latest/docs/resources/sdn_subnet).

**Decision: use Proxmox SDN with a VLAN zone.** Given this platform's explicit workflow — a brand-new VLAN is created on every project onboarding, not just once at cluster setup — SDN turns that recurring operation into an ordinary, idempotent `terraform apply` (create a `proxmox_sdn_vnet` + `proxmox_sdn_subnet` pair, then a `proxmox_sdn_applier`), with zero manual per-node file editing and no risk of one cluster node's `/etc/network/interfaces` drifting from another's. Option A remains technically simpler for a network that is defined once and never changes, but that isn't this platform's actual usage pattern.

**One caveat, explicitly not relied on:** Proxmox's own SDN-integrated IPAM/DHCP feature is listed as **tech preview** (not GA) as of the current Proxmox VE 8.1-era documentation — *"IPAM, including DHCP management for virtual guests, is in tech preview"* per the [Proxmox VE SDN wiki page](https://pve.proxmox.com/wiki/Software-Defined_Network). This platform does **not** depend on it: §3's static-IP-via-cloud-init approach, computed entirely by Terraform, is used instead, and Proxmox's built-in DHCP/IPAM is left disabled. (For reference, a community Terraform module exists that automates a VLAN-backed SDN zone with `dnsmasq`-based DHCP — [`hybridops-tech/sdn/proxmox`](https://registry.terraform.io/modules/hybridops-tech/sdn/proxmox/0.1.0) — noted here only as prior art, not adopted, since this platform's static-IP approach doesn't need it.)

## 6. Terraform scope

- **New shared foundation resources** (live in the platform-foundation Terraform root, not per-project): one `proxmox_sdn_zone_vlan` (the single VLAN-type zone all VNets attach to), one `proxmox_sdn_applier` wired via `replace_triggered_by` to react to any zone/VNet/subnet change, **plus the shared-services VLAN-20 VNet + subnet**: one `proxmox_sdn_vnet` (id `p20`, VLAN tag 20) and one `proxmox_sdn_subnet` (`cidr = "10.0.20.0/24"`, `gateway = "10.0.20.1"`) for the shared-services tier. VLAN 20 is shared and cluster-wide, not per-project — every "Shared"-tenancy service (SVC-06/07/09/13/14/17-18) attaches to it — so its VNet belongs to the foundation root alongside the zone and applier. Rationale: the per-project VNet mechanism below is deliberately limited to VLAN 100–254, so the shared VLAN-20 VNet has no per-project owner and must be foundation-owned.
- **Per-project resources** (created by the project's own onboarding Terraform, per PF §4's "every project gets its own root module" rule): one `proxmox_sdn_vnet` (VLAN tag = the project's assigned `vlan_id`, range 100–254) and one `proxmox_sdn_subnet` (`cidr = "10.0.<vlan_id>.0/24"`, `gateway = "10.0.<vlan_id>.1"`) per project.
- **Module inputs added to the shared `proxmox-compute` module** (PF §8): `vlan_id` now flows through to the guest's cloud-init static IP config via the §3 formula, rather than being paired with an operator-supplied static IP.
- **Terraform service-account privilege:** the `terraform@pve` custom PVE role (PF §11 / FR-1) must additionally include the `SDN.Allocate` privilege for SDN zone/VNet/subnet management to succeed. (`SDN.Use` is a separate, later-required privilege for attaching a guest NIC to an existing VNet, not for creating the SDN objects.)
- **Outputs:** `vlan_id`, `subnet_cidr`, `gateway_ip` — consumed by every service's own Terraform (to compute its own host IP per §3) and by the Ansible dynamic inventory generator.

## 7. Project VLAN-ID registry (replaces a human decision with a reviewable file, not a new service)

- A single committed file, `projects.yaml` (slug → assigned `vlan_id`, onboarding date, and a lifecycle `status`), lives in the platform-foundation Terraform repository — **not** a separate IPAM service, to keep with the self-hosted/simplicity-first principle at this scale.
- A small onboarding script (run as a CI job, or by hand before opening the onboarding MR) reads `projects.yaml`, computes `next_free_id = max(existing_ids, default=99) + 1`, appends the new project's slug/ID with `status: active`, and opens/updates a merge request — so the VLAN-ID assignment is automatic *and* visible/reviewable in a diff, rather than either a human picking a number or a hidden side-effect.
- Each project's own Terraform reads its assigned `vlan_id` from `projects.yaml` (via a `yamldecode()` data source or a CI-injected `-var`) rather than accepting it as a freely-chosen input — this is what actually makes VLAN-ID assignment non-human-decided in practice, closing the gap PF §15 originally flagged.

### 7.1 Two views over one append-only file — allocation vs. desired state

`projects.yaml` is an **append-only allocation ledger**: rows are never removed and VLAN IDs are never reused. But two different questions are asked of it, and conflating them is a bug (see [ADR-0005](../.kiro/decisions/0005-projects-yaml-lifecycle-status-field.md)):

- **Allocation ledger view (all rows):** the input to VLAN-ID allocation. `next_vlan_id` counts **every** row regardless of status, so a decommissioned project's ID still pushes the ceiling up and is never re-issued. This preserves the never-reuse rule of §2 and `domain-rules.md` §1.
- **Desired-state view (`status: active` rows only):** the input to the Proxmox-host gateway/firewall converge role (ADR-0004). It answers "which VLANs should **currently** have a routed host gateway + `pve-firewall` opening configured?" — every active project plus the always-served shared-services VLAN (20), not every ID ever allocated.

To keep those two questions orthogonal, each row carries a **`status`** field:

- `status` is one of `active` | `decommissioned`. An optional `decommissioned_date` (ISO 8601 `YYYY-MM-DD`) MAY be recorded on the row when it flips.
- **Decommissioning is a `status` flip in a reviewable MR, never a row removal.** Flipping `active` → `decommissioned` retains the row (so its VLAN ID stays retired forever) and, on the next converge run, drives the gateway role to *remove* that VLAN's host gateway + firewall opening — closing the security exposure where a torn-down project's VLAN keeps a live routed gateway indefinitely.
- **A missing `status` degrades safely to `active`** — a pre-backfill or hand-added row without the field is treated as active by every reader, so the field's absence reproduces the pre-feature behaviour rather than erroring.
- **Allocation is unaffected by status.** Adding `status` changes only what the *converge role* reads (the desired-state view); it does not change what the *onboarding allocation* reads (the ledger view). The `next_vlan_id` math is untouched.
- **Future upgrade path (not needed now):** if the platform ever grows past a few dozen concurrent projects, `projects.yaml` can be replaced by a proper IPAM/CMDB (e.g. NetBox, which Proxmox SDN can already integrate with as an external IPAM backend per the [Proxmox SDN documentation](https://pve.proxmox.com/pve-docs/chapter-pvesdn.html)) without changing the VLAN ID or CIDR formulas themselves.

## 8. The one decision that genuinely remains manual — and why

Everything above removes a human decision from IP addressing, VLAN-ID assignment, and hostname naming. One decision cannot be automated by Terraform because Terraform has no visibility into it: **which VLAN ID range is actually safe to use on the physical switch trunk port(s) feeding the Proxmox node(s)**, and configuring that trunk to permit the chosen range (802.1Q allows VLAN IDs up to 4094; this plan uses 10, 20, and 100+, but the *physical* switch/router the cluster's uplink is plugged into must already be configured to pass those tags, and must not have a conflicting native/default VLAN or an already-in-use ID in that range on the same physical network). This is a one-time, out-of-band decision made once when the physical network is wired up — it is not repeated per project, and it is explicitly out of scope for the Terraform/Ansible automation this catalog covers, since it requires either manual switch configuration or (if the switch itself becomes Terraform-managed later, e.g. via a network-vendor provider) a separate piece of infrastructure this catalog does not currently include.

## 9. Open questions / assumptions

- **Single-octet CIDR ceiling:** the `10.0.<vlan_id>.0/24` formula (§3) works cleanly for VLAN IDs 0–255; if the platform ever exceeds ~155 concurrent-or-historical projects (VLAN 100–254), the formula needs a second-octet extension. Not a current concern at the scale PF §6 assumes, but noted for a future revision.
- **Reserved range 30–99 has no assigned purpose yet** — intentionally left open per §2; a future shared-tier VLAN (public-edge/DMZ, dedicated backup network) should draw from this range rather than colliding with 100+.
- **`projects.yaml` as a flat file vs. a proper IPAM** (§7) is accepted for now given the homelab/office scale; revisit if project count or team size grows enough that MR-based registry updates become a bottleneck.
- **Physical trunk/switch configuration** (§8) is assumed to already permit VLANs 10, 20, and 100–354 (or whatever range is provisioned for) on the Proxmox uplink port(s) — this must be verified/configured once, out-of-band, before the first project onboarding, and is not validated by any acceptance criterion in this catalog.
- Whether the reserved buffer `.2`–`.9` inside each VLAN's `/24` (§3) is ever needed for anything concrete (a per-project jump host, a NAT gateway) is left open — currently just held in reserve.
