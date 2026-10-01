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
  host_index   = 0         # creation index 0 -> 10.0.20.10 (Requirement 9.2)
  instance     = "01"      # singleton -> svc07-20-01

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
  host_index   = 1                  # creation index 1 -> 10.0.20.11 (Requirement 9.3)
  instance     = "01"               # singleton -> svc07-unsealer-20-01

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
