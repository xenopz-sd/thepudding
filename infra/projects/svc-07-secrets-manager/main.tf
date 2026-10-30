# SVC-07 Secrets Manager (OpenBao) Terraform root — LXC declarations.
#
# This root declares exactly TWO `proxmox_virtual_environment_container`
# resources and no other container resources (Requirement 9.1):
#
#   * PRIMARY  — svc07-20-01,          10.0.20.10, host_index 0  (this task 2.1)
#   * UNSEALER — svc07-unsealer-20-01, 10.0.20.11, host_index 1  (task 2.2)
#
# Each container's addressing/hostname/tags are produced by an invocation of the
# shared `proxmox-compute` module, CONSUMED UNMODIFIED. That module owns only the
# computed addressing contract (vlan_id + host_index -> computed static IPv4,
# hostname, tags); it declares no guest-provisioning inputs and no IP literal, so
# the actual guest resource lives here and threads the module's computed
# `hostname`, `ip_config`, `gateway`, and `tags` outputs into the LXC (see the
# module's locals.tf "MODULE SCOPE" note). No operator IP literal is used
# anywhere — 10.0.20.10 is COMPUTED by the module, not written (Requirement 9.2).
#
# ===========================================================================
# Host_Index_Authority ledger read (fix-vlan-host-index-collision, task 5 / C7)
# ===========================================================================
#
# SVC-07 is one of SEVERAL independent Terraform roots that populate VLAN 20
# (alongside svc-09-reverse-proxy, svc-06-identity-provider, ...). Each root
# picks its own host_index from zero with no shared counter, so NET-00 §3's
# "host_index is unique by for_each construction" guarantee — true for a single
# root enumerating a whole VLAN — does NOT hold across roots. That cross-root gap
# is the live VLAN-20 .10 collision (SVC-07 primary vs SVC-09 Traefik both at
# host_index 0). The fix is the committed per-VLAN ledger
# (infra/platform-foundation/host-indices.yaml, the Host_Index_Authority): the
# shared counter that makes host_index unique within a VLAN across roots.
#
# This root READS the ledger (it never writes it — read-only input, exactly like
# projects.yaml, NEVER Terraform state; one-state-per-service is preserved). From
# it we derive, for each of SVC-07's two VLAN-20 guests, its `peer_host_indices`
# = every host_index allocated on VLAN 20 EXCEPT its own — threaded into the
# proxmox-compute module so its Guard-4 plan-time precondition fails CLOSED on a
# within-VLAN host-index collision before any Proxmox resource is created.
#
# SVC-07's two indices are STATEFUL ANCHORS (prevent_destroy, below) and do NOT
# move: primary stays host_index 0 -> 10.0.20.10, unsealer stays host_index 1 ->
# 10.0.20.11. The literals `host_index = 0` / `= 1` remain the explicit assigned
# values on the module calls; the ledger is used (a) to source the peer sets and
# (b) to assert — robustly, without breaking the apply if the file path differs —
# that those literals still match the ledger's anchor rows. The
# cidrhost("10.0.20.0/24", 10 + host_index) formula is UNCHANGED.

locals {
  # VLAN this root's guests live on (shared-services tier, domain-rules §1). Both
  # module calls below pass vlan_id = 20; keep it here so the ledger lookup and
  # the module calls cannot drift apart.
  svc07_vlan_id = 20

  # The two anchor indices, kept as explicit literals (they are prevent_destroy
  # stateful anchors — see the lifecycle blocks below). These remain the values
  # passed to the module; the ledger cross-check asserts they still agree.
  svc07_primary_host_index  = 0 # -> 10.0.20.10
  svc07_unsealer_host_index = 1 # -> 10.0.20.11

  # --- Read the Host_Index_Authority ledger (read-only input, never state). ----
  # Walk back from this per-service root (infra/projects/svc-07-secrets-manager/)
  # to the platform-foundation root (infra/platform-foundation/), mirroring how
  # the per-project _TEMPLATE root reads projects.yaml. ROBUSTNESS: if the ledger
  # file is absent (e.g. a stripped sandbox), fall back to an empty structure so
  # the apply is NOT broken by a differing path — the derived peer sets then fall
  # back to the hardcoded VLAN-20 set below, and the module's Guard 4 still runs.
  svc07_host_index_ledger_path = "${path.module}/../../platform-foundation/host-indices.yaml"
  svc07_host_index_ledger      = fileexists(local.svc07_host_index_ledger_path) ? yamldecode(file(local.svc07_host_index_ledger_path)) : {}

  # yamldecode renders top-level integer keys as STRINGS, so look up VLAN 20 as
  # "20". Each VLAN maps to a list of allocation entries; pull every host_index
  # on VLAN 20. If the ledger is absent/empty, fall back to the design §6 VLAN-20
  # map so the peer-set wiring is still meaningful offline.
  svc07_vlan20_entries = try(local.svc07_host_index_ledger[tostring(local.svc07_vlan_id)], [])
  # tonumber() COERCION GUARD: the ledger read above uses the
  # `fileexists(p) ? yamldecode(file(p)) : {}` fallback, whose ternary unifies the
  # populated map against the empty `object({})`. Once host-indices.yaml carries
  # MORE THAN ONE top-level VLAN key whose entry-lists have differing object
  # shapes (e.g. VLAN 20's rows carry optional component/anchor, a project VLAN's
  # single svc01 row does not), terraform can no longer give the map a uniform
  # element type and coerces every scalar — including host_index — to STRING.
  # A bare `yamldecode(file(...))` (no `: {}` fallback) keeps numbers, but the
  # fail-open-on-missing-file fallback here needs the ternary. tonumber() makes
  # the index list numeric REGARDLESS of that coercion, so `contains(indices, 0)`
  # and the `i != host_index` peer filter stay correct no matter how many project
  # VLANs the shared ledger later gains (project-consumer-onboarding Task 6.3).
  svc07_vlan20_indices_from_ledger = [for e in local.svc07_vlan20_entries : tonumber(e.host_index)]
  svc07_vlan20_indices             = length(local.svc07_vlan20_indices_from_ledger) > 0 ? local.svc07_vlan20_indices_from_ledger : [0, 1, 2, 4, 5, 6, 7, 8, 9, 10]

  # Per-guest peer-set view (design §Data Models / §5.1): all VLAN-20 indices
  # EXCEPT this guest's own. A guest is never its own peer, so Guard 4's
  # !contains(peer, self) can only fire on a genuine cross-guest collision.
  svc07_primary_peer_host_indices  = [for i in local.svc07_vlan20_indices : i if i != local.svc07_primary_host_index]
  svc07_unsealer_peer_host_indices = [for i in local.svc07_vlan20_indices : i if i != local.svc07_unsealer_host_index]
}

# Ledger cross-check (robust, non-fatal on a missing file). Assert the two anchor
# literals still appear in the ledger's VLAN-20 index set when the ledger is
# present — catching a ledger edit that silently moved an anchor. When the ledger
# file is absent (path differs / stripped sandbox) the fallback index set is used
# and the check is a tautology, so the apply is never broken by a path mismatch.
resource "terraform_data" "svc07_host_index_ledger_check" {
  lifecycle {
    precondition {
      condition     = contains(local.svc07_vlan20_indices, local.svc07_primary_host_index)
      error_message = "SVC-07 primary host_index ${local.svc07_primary_host_index} (anchor, 10.0.20.10) is not present on VLAN ${local.svc07_vlan_id} in the Host_Index_Authority ledger (infra/platform-foundation/host-indices.yaml). The primary is a stateful prevent_destroy anchor and must keep index 0; reconcile the ledger rather than moving the guest."
    }
    precondition {
      condition     = contains(local.svc07_vlan20_indices, local.svc07_unsealer_host_index)
      error_message = "SVC-07 unsealer host_index ${local.svc07_unsealer_host_index} (anchor, 10.0.20.11) is not present on VLAN ${local.svc07_vlan_id} in the Host_Index_Authority ledger (infra/platform-foundation/host-indices.yaml). The unsealer is a stateful prevent_destroy anchor and must keep index 1; reconcile the ledger rather than moving the guest."
    }
  }
}

# ===========================================================================
# PRIMARY OpenBao LXC (svc07-20-01, 10.0.20.10, host_index 0) — task 2.1
# ===========================================================================

# Addressing/hostname/tags for the primary, computed by the shared module.
# svc_code=svc07, vlan_id=20, host_index=0 -> ipv4 10.0.20.10 (cidrhost(
# "10.0.20.0/24", 10 + 0)), hostname "svc07-20-01", tags ["proj-shared-openbao"]
# (Requirement 9.2, 9.4; NET-00 §3-§4). No `component` -> the plain
# <svc-code>-<vlan_id>-<instance> hostname form.
module "primary" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"  # shared platform service -> tag proj-shared-openbao
  service_name = "openbao" # -> tags ["proj-shared-openbao"] (PF §4)
  svc_code     = "svc07"   # -> hostname svc07-20-01 (NET-00 §4)
  vlan_id      = 20        # shared-services VLAN (domain-rules §1)
  host_index   = 0         # creation index 0 -> 10.0.20.10 (Requirement 9.2) — anchor, unchanged
  instance     = "01"      # singleton -> svc07-20-01

  # Every OTHER VLAN-20 host_index, from the Host_Index_Authority ledger (task 5
  # / C7). Activates the module's Guard-4 within-VLAN collision precondition for
  # the primary. No IP change — the primary stays at index 0 -> 10.0.20.10.
  peer_host_indices = local.svc07_primary_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo (C1); injection happens on the root's guest resource below
}

# The primary OpenBao container. Exactly one of the two LXCs this root owns
# (Requirement 9.1). Unprivileged, 2 vCPU / 2048 MB / 20 GB local-zfs rootfs on
# the VLAN-20 SDN VNet, stock Debian 12 / Ubuntu 24.04 LTS template (Docker +
# Compose installed per host by the common role at provision time, ADR-0003;
# Requirement 9.2, 9.4).
resource "proxmox_virtual_environment_container" "primary" {
  description = "SVC-07 primary OpenBao instance (Raft storage, KV v2 / database / aws / jwt / oidc / audit). Managed by Terraform; see .kiro/specs/svc-07-secrets-manager."

  node_name = var.proxmox_node_name
  vm_id     = var.openbao_primary_vmid

  # Unprivileged container (Requirement 9.2; PF LXC-default). `nesting` is
  # required so the in-container Docker engine (which the Compose stack needs)
  # runs inside an unprivileged LXC.
  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # 2 vCPU (Requirement 9.2)
  }

  # Memory sizing + swap lockdown (Requirement 9.2, 5.8; ADR-0006).
  # `swap = 0` is the provider-native per-container swap cap (#2 in ADR-0006):
  # a real, per-guest cgroup limit (`memory.swap.max=0`) with no cross-tenant
  # side effect, retained as per-guest defence-in-depth. The requirement also
  # names the raw cgroup key `lxc.cgroup2.memory.swap.max = 0`; the bpg/proxmox
  # API cannot write raw `lxc.*` config keys (the provider documents that the
  # Proxmox API does not support writing lxc[n] parameters — the same reason
  # `idmap` is written over SSH), so that host-cgroup line is still enforced at
  # the host/Ansible layer over SSH. The host-layer "secrets never persist to
  # plaintext swap" control is ENCRYPTED HOST SWAP (an operator prerequisite per
  # ADR-0006), NOT an in-guest `vm.swappiness` write — that in-container
  # swappiness task was removed (broken/heuristic/cross-tenant on unprivileged
  # LXC). Setting `swap = 0` here keeps the container from being provisioned with
  # a swap device in the first place.
  memory {
    dedicated = 2048 # 2048 MB RAM (Requirement 9.2)
    swap      = 0    # no swap device — per-container swap cap (Requirement 5.8; ADR-0006)
  }

  # Root filesystem: 20 GB on local-zfs (Requirement 9.2).
  disk {
    datastore_id = var.openbao_datastore_id
    size         = 20
  }

  operating_system {
    template_file_id = var.openbao_template_file_id
    type             = var.openbao_template_os_type # debian | ubuntu (Req 9.4)
  }

  # Hostname + computed static IPv4 (10.0.20.10) injected from the module —
  # never an operator IP literal (Requirement 9.2, 9.4; NET-00 §3).
  initialization {
    hostname = module.primary.hostname # svc07-20-01

    ip_config {
      ipv4 {
        address = module.primary.ip_config.ipv4.address # 10.0.20.10/24 (computed)
        gateway = module.primary.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys (bpg/proxmox); empty list => block omitted => strict no-op
      }
    }
  }

  # Attach to the VLAN-20 shared-services SDN VNet. The VNet id `p20` follows the
  # platform's deterministic `p<vlan_id>` derivation (mirrors the per-project
  # root's `local.vnet_id = "p${vlan_id}"`, _TEMPLATE/sdn.tf). Per the SVC-07
  # requirements Assumptions, the VLAN-20 SDN zone/VNet objects are a
  # Platform-Foundation-owned precondition assumed present — this root only
  # references the VNet, it does not create it (Requirement 9.2). `p20` is a
  # VLAN-zone SDN VNet, so the VNet itself applies tag 20 to the traffic; the
  # guest NIC must NOT also 802.1Q-tag onto VLAN 20 (no `vlan_id` here), or
  # Proxmox rejects the container start with "vm vlans are not allowed on vnet
  # p20" (SDN Zones Plugin). The static address above comes solely from
  # cloud-init; SDN built-in IPAM/DHCP stays disabled (NET-00 §5).
  network_interface {
    name   = "veth0"
    bridge = "p20" # VLAN-20 shared-services SDN VNet (precondition, not created here). The
    #                VNet is a VLAN-zone VNet and applies tag 20 itself — do NOT also set
    #                vlan_id on the NIC, or Proxmox rejects the start with "vm vlans are not
    #                allowed on vnet p20" (SDN Zones Plugin). The VNet supplies the tag.
  }

  # Human-facing Proxmox tag proj-shared-openbao, from the module (Requirement
  # 9.4). Distinct from the machine-facing hostname above.
  tags = module.primary.tags

  # Deterministic apply/idempotency: don't fight Proxmox's tag lowercasing/sort.
  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: this LXC is a live, STATEFUL secret store (Raft/Transit data +
    # OpenBao unseal state). It must NEVER be silently destroyed/replaced by an
    # apply. `prevent_destroy` makes Terraform HARD-ERROR on any plan that would
    # destroy or replace this resource (e.g. a "force replacement" from a drifted
    # datastore_id / template / network attribute), instead of merely offering a
    # `yes` prompt an operator might approve by reflex. To make a deliberate,
    # unavoidable replacement, an operator must consciously remove this block (or
    # `-target` a taint) — a conscious act, not a default. See ADR-0007 rev 2 and
    # the SVC-07 INSTALL-RUNBOOK "destructive-plan" guardrail.
    prevent_destroy = true
  }
}

# ===========================================================================
# TRANSIT UNSEALER OpenBao LXC (svc07-unsealer-20-01, 10.0.20.11, host_index 1) — task 2.2
# ===========================================================================

# Addressing/hostname/tags for the seal-only Transit unsealer, computed by the
# shared module. svc_code=svc07, component=unsealer, vlan_id=20, host_index=1 ->
# ipv4 10.0.20.11 (cidrhost("10.0.20.0/24", 10 + 1)), hostname
# "svc07-unsealer-20-01", tags ["proj-shared-openbao-unsealer"] (Requirement
# 9.3, 9.5; NET-00 §3-§4). This catalog line item deploys two distinct running
# services (the primary Raft store and the Transit auto-unseal helper), so the
# unsealer sets `component = "unsealer"` -> the
# <svc-code>-<component>-<vlan_id>-<instance> hostname form (NET-00 §4). The
# service_name "openbao-unsealer" yields the distinct tag proj-shared-openbao-unsealer.
module "unsealer" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"           # shared platform service -> tag prefix proj-shared-
  service_name = "openbao-unsealer" # -> tags ["proj-shared-openbao-unsealer"] (PF §4)
  svc_code     = "svc07"            # stable catalog slot (NET-00 §4)
  component    = "unsealer"         # -> hostname svc07-unsealer-20-01 (NET-00 §4)
  vlan_id      = 20                 # shared-services VLAN (domain-rules §1)
  host_index   = 1                  # creation index 1 -> 10.0.20.11 (Requirement 9.3) — anchor, unchanged
  instance     = "01"               # singleton -> svc07-unsealer-20-01

  # Every OTHER VLAN-20 host_index, from the Host_Index_Authority ledger (task 5
  # / C7). Activates the module's Guard-4 within-VLAN collision precondition for
  # the unsealer. No IP change — the unsealer stays at index 1 -> 10.0.20.11.
  peer_host_indices = local.svc07_unsealer_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo (C1); injection happens on the root's guest resource below
}

# The Transit unsealer OpenBao container. The second of the two LXCs this root
# owns (Requirement 9.1). Unprivileged, 1 vCPU / 512 MB / 8 GB local-zfs rootfs on
# the VLAN-20 SDN VNet, same Debian 12 / Ubuntu 24.04 LTS template family as the
# primary; Docker + Compose are installed per host by the common role at
# provision time (ADR-0003), not pre-baked (Requirement 9.3, 9.5). Internal-only:
# no Traefik labels and no published host port are declared here (the design's
# explicit, justified ingress exception; those belong to the Compose layer).
resource "proxmox_virtual_environment_container" "unsealer" {
  description = "SVC-07 seal-only Transit unsealer OpenBao instance (Transit engine + minimal Raft, no KV/database/aws/PKI mount). Managed by Terraform; see .kiro/specs/svc-07-secrets-manager."

  node_name = var.proxmox_node_name
  vm_id     = var.openbao_unsealer_vmid

  # Unprivileged container (Requirement 9.3; PF LXC-default). `nesting` is
  # required so the in-container Docker engine (which the Compose stack needs)
  # runs inside an unprivileged LXC.
  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 1 # 1 vCPU (Requirement 9.3)
  }

  # Memory sizing + swap lockdown (Requirement 9.3, 5.8; ADR-0006).
  # `swap = 0` is the provider-native per-container swap cap (#2 in ADR-0006):
  # a real, per-guest cgroup limit (`memory.swap.max=0`) with no cross-tenant
  # side effect, retained as per-guest defence-in-depth. The requirement also
  # names the raw cgroup key `lxc.cgroup2.memory.swap.max = 0`; the bpg/proxmox
  # API cannot write raw `lxc.*` config keys (the provider documents that the
  # Proxmox API does not support writing lxc[n] parameters — the same reason
  # `idmap` is written over SSH), so that host-cgroup line is still enforced at
  # the host/Ansible layer over SSH. The host-layer "secrets never persist to
  # plaintext swap" control is ENCRYPTED HOST SWAP (an operator prerequisite per
  # ADR-0006), NOT an in-guest `vm.swappiness` write — that in-container
  # swappiness task was removed (broken/heuristic/cross-tenant on unprivileged
  # LXC). Setting `swap = 0` here keeps the container from being provisioned with
  # a swap device in the first place — matching the primary's approach exactly.
  memory {
    dedicated = 512 # 512 MB RAM (Requirement 9.3)
    swap      = 0   # no swap device — per-container swap cap (Requirement 5.8; ADR-0006)
  }

  # Root filesystem: 8 GB on local-zfs (Requirement 9.3).
  disk {
    datastore_id = var.openbao_datastore_id
    size         = 8
  }

  operating_system {
    template_file_id = var.openbao_template_file_id # same template family as the primary (Req 9.5)
    type             = var.openbao_template_os_type # debian | ubuntu (Req 9.5)
  }

  # Hostname + computed static IPv4 (10.0.20.11) injected from the module —
  # never an operator IP literal (Requirement 9.3, 9.5; NET-00 §3).
  initialization {
    hostname = module.unsealer.hostname # svc07-unsealer-20-01

    ip_config {
      ipv4 {
        address = module.unsealer.ip_config.ipv4.address # 10.0.20.11/24 (computed)
        gateway = module.unsealer.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys (bpg/proxmox); empty list => block omitted => strict no-op
      }
    }
  }

  # Attach to the VLAN-20 SDN VNet. `p20` is a VLAN-zone SDN VNet, so the VNet
  # itself applies tag 20 to the traffic; the guest NIC must NOT also 802.1Q-tag
  # onto VLAN 20 (no `vlan_id` here), or Proxmox rejects the container start with
  # "vm vlans are not allowed on vnet p20" (SDN Zones Plugin). The static address
  # above comes solely from cloud-init (SDN IPAM/DHCP stays disabled, NET-00 §5).
  network_interface {
    name   = "veth0"
    bridge = "p20" # VLAN-20 shared-services SDN VNet; VNet applies tag 20, NIC must not

    # NO `firewall = true` HERE — DELIBERATE, see ADR-0008.
    #
    # Enabling the per-NIC guest firewall was tried (ADR-0007 rev 2) as the
    # enforcement point for primary-only access to this guest's seal API, because a
    # cluster-level rule cannot filter same-bridge intra-VLAN traffic (that was
    # ADR-0007 rev 1's failure). It BROKE THE GUEST: the unsealer lost all egress,
    # `apt update` failed, Docker was never installed, and `getent hosts
    # deb.debian.org` returned nothing — and it STILL failed after five verified
    # guest-scoped `out` ACCEPT rules (DNS/HTTP/HTTPS/NTP) were applied and
    # pve-firewall reloaded. Enabling the flag also re-plugs the guest behind an
    # `fwbr` bridge, disturbing the host-side egress MASQUERADE path this platform
    # relies on (ADR-0004).
    #
    # CONSEQUENCE: network-layer primary-only scoping of :8200 is a documented KNOWN
    # LIMITATION — currently UNMET; the seal port is reachable within VLAN 20 (the
    # shared-services tier). Management-VLAN-10 and cross-project isolation are
    # unaffected (those are routed paths the host firewall does see).
    #
    # Do NOT re-add `firewall = true` without a solved egress story — an offline test
    # (test_unsealer_nic_firewall_not_enabled) guards this so re-enabling is a
    # deliberate, reviewed act. The mechanism for intra-VLAN scoping is intentionally
    # left OPEN for a later decision (ADR-0008 lists the candidates).
  }

  # Human-facing Proxmox tag proj-shared-openbao-unsealer, from the module
  # (Requirement 9.5). Distinct from the machine-facing hostname above.
  tags = module.unsealer.tags

  # Deterministic apply/idempotency: don't fight Proxmox's tag lowercasing/sort.
  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: this LXC is a live, STATEFUL secret store (Raft/Transit data +
    # OpenBao unseal state). It must NEVER be silently destroyed/replaced by an
    # apply. `prevent_destroy` makes Terraform HARD-ERROR on any plan that would
    # destroy or replace this resource (e.g. a "force replacement" from a drifted
    # datastore_id / template / network attribute), instead of merely offering a
    # `yes` prompt an operator might approve by reflex. To make a deliberate,
    # unavoidable replacement, an operator must consciously remove this block (or
    # `-target` a taint) — a conscious act, not a default. See ADR-0007 rev 2 and
    # the SVC-07 INSTALL-RUNBOOK "destructive-plan" guardrail.
    prevent_destroy = true
  }
}
