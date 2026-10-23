# SVC-09 Reverse Proxy (Traefik) Terraform root — LXC declaration.
#
# This root declares exactly ONE `proxmox_virtual_environment_container`
# resource and no other container resource type:
#
#   * TRAEFIK — svc09-20-01, 10.0.20.12, host_index 2
#
# The container's addressing/hostname/tags are produced by an invocation of the
# shared `proxmox-compute` module, CONSUMED UNMODIFIED. That module owns only the
# computed addressing contract (vlan_id + host_index -> computed static IPv4,
# hostname, tags); it declares no guest-provisioning inputs and no IP literal, so
# the actual guest resource lives here and threads the module's computed
# `hostname`, `ip_config`, `gateway`, and `tags` outputs into the LXC. No
# operator IP literal is used anywhere — the address is COMPUTED by the module.
#
# ===========================================================================
# HOST-INDEX AUTHORITY (ledger) — the Traefik index + VLAN-20 peer set.
# ===========================================================================
# Traefik's host_index and the set of OTHER indices on VLAN 20 are sourced from
# the committed per-VLAN host-index ledger (the Host_Index_Authority) rather than
# hardcoded in isolation — design §5.1 "How a root consumes the authority". The
# ledger is a READ-ONLY input file (never Terraform state), read the same way
# projects.yaml is: `yamldecode(file(...))`. One state per service is preserved;
# this root merges no other root's state.
#
# cidrhost("10.0.20.0/24", 10 + host_index) is UNCHANGED — only HOW host_index is
# made unique across the separate roots on VLAN 20 is coordinated via the ledger.
locals {
  # Parse the committed ledger (infra/platform-foundation/host-indices.yaml).
  _host_index_ledger = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))

  # This root's VLAN. Keep the literal here (it is also passed to the module
  # below); the ledger is keyed by vlan_id, so we read the VLAN-20 entry list.
  _vlan_id             = 20
  _vlan20_entries      = local._host_index_ledger[local._vlan_id]
  _vlan20_host_indices = [for e in local._vlan20_entries : e.host_index]

  # Traefik's own assigned index per the ledger (svc09, no component). Asserted
  # below to equal the explicit host_index literal so a drift between this root
  # and the ledger is caught, not silently papered over.
  _traefik_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if e.svc_code == "svc09" && lookup(e, "component", null) == null
  ])

  # This root's EXPLICIT assigned index. Kept as a literal (2) for readability
  # at the call site; cross-checked against the ledger via the precondition.
  traefik_host_index = 2 # -> 10.0.20.12 (MOVED from 0, which collided with SVC-07 primary)

  # Peer set = every VLAN-20 index EXCEPT Traefik's own. Threaded into the
  # proxmox-compute `peer_host_indices` plan-time precondition (Guard 4), which
  # FAILS CLOSED if any other guest on VLAN 20 already holds index 2.
  traefik_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.traefik_host_index
  ]
}

# Addressing/hostname/tags for the Traefik LXC, computed by the shared module.
# svc_code=svc09, vlan_id=20, host_index=2 -> ipv4 10.0.20.12 (cidrhost(
# "10.0.20.0/24", 10 + 2)), hostname "svc09-20-01", tags ["proj-shared-traefik"]
# (NET-00 §3-§4). No `component` -> the plain <svc-code>-<vlan_id>-<instance>
# hostname form.
#
# ---------------------------------------------------------------------------
# IP-MOVE CALL-OUT (NOT a silent in-place apply) — Traefik moved index 0 -> 2.
# ---------------------------------------------------------------------------
# host_index 0 (10.0.20.10) collided live with the SVC-07 OpenBao primary, which
# also sits at host_index 0 -> 10.0.20.10. Traefik is moved to host_index 2
# (10.0.20.12). Because this changes an ALREADY-DEPLOYED guest's computed
# ip_config, the next apply would be a destroy/replace of the Traefik LXC — and
# that LXC carries `prevent_destroy = true` (the acme.json cert store seatbelt
# below), so Terraform will HARD-ERROR rather than apply in place. The live move
# is therefore a CONSCIOUS, one-time re-provision via SVC-09's own clean-slate
# contract (performed in the live-revalidation task 14 of
# .kiro/specs/fix-vlan-host-index-collision), NOT a silent `terraform apply`.
# Traefik's only persistent state (acme.json) is re-issuable, which is why moving
# it — rather than SVC-07's stateful anchors — is the correct fix.
module "traefik" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"                 # shared platform service -> tag proj-shared-traefik
  service_name = "traefik"                # -> tags ["proj-shared-traefik"] (PF §4)
  svc_code     = "svc09"                  # -> hostname svc09-20-01 (NET-00 §4)
  vlan_id      = local._vlan_id           # shared-services VLAN 20 (domain-rules §1)
  host_index   = local.traefik_host_index # index 2 -> 10.0.20.12 (MOVED from 0, NET-00 §3)
  instance     = "01"                     # singleton -> svc09-20-01

  # Every OTHER host_index on VLAN 20, from the ledger. The module's Guard 4
  # precondition fails at plan time if Traefik's index collides with a peer.
  peer_host_indices = local.traefik_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the root's guest resource below
}

# Ledger/root drift guard. A `module` block cannot carry a `lifecycle`
# precondition (that block type is reserved there in Terraform), so this
# no-side-effect `terraform_data` anchor hosts the cross-check: the EXPLICIT
# host_index literal (2) MUST equal the ledger's svc09 VLAN-20 entry. If someone
# edits the ledger or this root's literal without the other, the plan FAILS
# CLOSED here before any Proxmox resource is created — it never drifts silently.
# (The within-VLAN collision check itself is the module's Guard 4 over
# `peer_host_indices`.)
resource "terraform_data" "ledger_index_assert" {
  input = {
    traefik_host_index   = local.traefik_host_index
    traefik_ledger_index = local._traefik_ledger_index
  }

  lifecycle {
    precondition {
      condition     = local.traefik_host_index == local._traefik_ledger_index
      error_message = "SVC-09 Traefik host_index (${local.traefik_host_index}) does not match the host-indices.yaml ledger entry for svc09 on VLAN 20 (${local._traefik_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
  }
}

# The Traefik container. The single LXC this root owns. Unprivileged, 1 vCPU /
# 512 MB / 4 GB local-zfs rootfs on the VLAN-20 SDN VNet, stock Debian 12 /
# Ubuntu 24.04 LTS template (Docker + Compose installed per host by the common
# role at provision time, ADR-0003; SVC-09 requirements §7).
resource "proxmox_virtual_environment_container" "traefik" {
  description = "SVC-09 Traefik reverse proxy / TLS termination (shared ingress, owns the external `edge` Docker network). Managed by Terraform; see .kiro/specs/svc-09-reverse-proxy."

  node_name = var.proxmox_node_name
  vm_id     = var.traefik_vmid

  # Unprivileged container (PF LXC-default). `nesting` is required so the
  # in-container Docker engine (which the Compose stack needs) runs inside an
  # unprivileged LXC.
  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 1 # 1 vCPU (SVC-09 requirements §7)
  }

  # Memory sizing + swap lockdown. `swap = 0` is the provider-native
  # per-container swap cap (a real cgroup limit with no cross-tenant side
  # effect), consistent with the platform's secrets-adjacent-guest posture
  # (ADR-0006). Traefik holds the ACME private key material, so the same swap
  # discipline applies.
  memory {
    dedicated = 512 # 512 MB RAM (SVC-09 requirements §7)
    swap      = 0   # no swap device — per-container swap cap (ADR-0006)
  }

  # Root filesystem: 4 GB on local-zfs (SVC-09 requirements §7). The acme.json
  # cert store + static/dynamic config live on this persistent rootfs (never
  # tmpfs), so certificates survive a container restart.
  disk {
    datastore_id = var.traefik_datastore_id
    size         = 4
  }

  operating_system {
    template_file_id = var.traefik_template_file_id
    type             = var.traefik_template_os_type # debian | ubuntu
  }

  # Hostname + computed static IPv4 injected from the module — never an operator
  # IP literal (NET-00 §3).
  initialization {
    hostname = module.traefik.hostname # svc09-20-01

    ip_config {
      ipv4 {
        address = module.traefik.ip_config.ipv4.address # 10.0.20.12/24 (computed)
        gateway = module.traefik.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys; empty list => block omitted => strict no-op
      }
    }
  }

  # Attach to the VLAN-20 shared-services SDN VNet. `p20` is a VLAN-zone SDN
  # VNet, so the VNet itself applies tag 20 to the traffic; the guest NIC must
  # NOT also 802.1Q-tag onto VLAN 20 (no `vlan_id` here), or Proxmox rejects the
  # container start with "vm vlans are not allowed on vnet p20" (SDN Zones
  # Plugin). The static address above comes solely from cloud-init; SDN built-in
  # IPAM/DHCP stays disabled (NET-00 §5). The VLAN-20 SDN zone/VNet objects are a
  # Platform-Foundation-owned precondition assumed present — this root only
  # references the VNet, it does not create it.
  network_interface {
    name   = "veth0"
    bridge = "p20" # VLAN-20 shared-services SDN VNet; VNet applies tag 20, NIC must not
  }

  # Human-facing Proxmox tag proj-shared-traefik, from the module. Distinct from
  # the machine-facing hostname above.
  tags = module.traefik.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: the Traefik LXC holds the acme.json cert store (private keys for
    # every issued certificate) on its rootfs. It must never be silently
    # destroyed/replaced by an apply (which would discard acme.json and force a
    # full Let's Encrypt re-issuance, rate-limited to 50 certs/week/domain).
    # `prevent_destroy` makes Terraform HARD-ERROR on any destroy/replace plan
    # instead of offering a `yes` prompt an operator might approve by reflex. To
    # make a deliberate replacement, an operator must consciously remove this
    # block. See the SVC-09 INSTALL-RUNBOOK "destructive-plan" guardrail.
    prevent_destroy = true
  }
}
