# SVC-17/18 Observability (Prometheus + Grafana + Loki) Terraform root — the
# three LXC declarations.
#
# This root declares exactly THREE `proxmox_virtual_environment_container`
# resources and no other container resource type:
#
#   * PROMETHEUS (+ Alertmanager co-located) — svc17-prometheus-20-01, 10.0.20.18, host_index 8
#   * GRAFANA                                 — svc17-grafana-20-01, 10.0.20.19, host_index 9
#   * LOKI                                    — svc18-loki-20-01, 10.0.20.20, host_index 10
#
# Each container's addressing/hostname/tags are produced by an invocation of the
# shared `proxmox-compute` module, CONSUMED UNMODIFIED. That module owns only the
# computed addressing contract (vlan_id + host_index -> computed static IPv4,
# hostname, tags); it declares no guest-provisioning inputs and no IP literal, so
# the actual guest resources live here and thread each module's computed
# `hostname`, `ip_config`, `gateway`, and `tags` outputs into the LXC. No
# operator IP literal is used anywhere — every address is COMPUTED by the module
# via cidrhost("10.0.20.0/24", 10 + host_index) (NET-00 §3).
#
# This is the FIRST service to place THREE guests on VLAN 20 from a SINGLE root,
# so the three host indices must be mutually distinct AND distinct from every
# other VLAN-20 occupant. That is enforced two ways (design §3.6): each module
# block passes the OTHER two indices (plus every other VLAN-20 index) as
# `peer_host_indices`, feeding the module's Guard 4 plan-time precondition
# (`!contains(peer, host_index)`, which fails CLOSED on a collision); and each
# block's explicit host_index literal is cross-checked against the committed
# Host_Index_Authority ledger below so a root/ledger drift is caught, never
# silently papered over.
#
# ===========================================================================
# HOST-INDEX AUTHORITY (ledger) — the three svc17-18 indices + the VLAN-20 set.
# ===========================================================================
# The three component indices (prometheus=8, grafana=9, loki=10) and the set of
# every OTHER index on VLAN 20 are sourced from the committed per-VLAN
# host-index ledger (the Host_Index_Authority, ADR-0015) rather than hardcoded
# in isolation. The ledger is a READ-ONLY input file (never Terraform state),
# read the same way projects.yaml is: `yamldecode(file(...))`. One state per
# service is preserved; this root merges no other root's state.
#
# cidrhost("10.0.20.0/24", 10 + host_index) is UNCHANGED — only HOW each
# host_index is made unique across the separate roots on VLAN 20 is coordinated
# via the ledger.
locals {
  # Parse the committed ledger (infra/platform-foundation/host-indices.yaml).
  _host_index_ledger = yamldecode(file("${path.module}/../../platform-foundation/host-indices.yaml"))

  # This root's VLAN. Keep the literal here (it is also passed to the module
  # blocks below); the ledger is keyed by vlan_id, so we read the VLAN-20 entry
  # list.
  _vlan_id             = 20
  _vlan20_entries      = local._host_index_ledger[local._vlan_id]
  _vlan20_host_indices = [for e in local._vlan20_entries : e.host_index]

  # Each svc17-18 component's assigned index per the ledger, selected by the
  # (svc_code, component) pair. Asserted below to equal the explicit host_index
  # literal so a drift between this root and the ledger is caught, not silently
  # papered over.
  _prometheus_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if e.svc_code == "svc17" && lookup(e, "component", null) == "prometheus"
  ])
  _grafana_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if e.svc_code == "svc17" && lookup(e, "component", null) == "grafana"
  ])
  _loki_ledger_index = one([
    for e in local._vlan20_entries : e.host_index
    if e.svc_code == "svc18" && lookup(e, "component", null) == "loki"
  ])

  # This root's EXPLICIT assigned indices. Kept as literals for readability at
  # the call sites; each is cross-checked against the ledger via a precondition.
  prometheus_host_index = 8  # -> 10.0.20.18
  grafana_host_index    = 9  # -> 10.0.20.19
  loki_host_index       = 10 # -> 10.0.20.20

  # Peer set FOR EACH guest = every VLAN-20 index EXCEPT that guest's own.
  # Threaded into the proxmox-compute `peer_host_indices` plan-time precondition
  # (Guard 4), which FAILS CLOSED if any other guest on VLAN 20 already holds
  # that index. Because all three indices live in the same ledger, this also
  # guarantees the three are mutually distinct (each sees the other two).
  prometheus_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.prometheus_host_index
  ]
  grafana_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.grafana_host_index
  ]
  loki_peer_host_indices = [
    for i in local._vlan20_host_indices : i if i != local.loki_host_index
  ]
}

# ===========================================================================
# PROMETHEUS (+ Alertmanager) — svc17-prometheus-20-01, 10.0.20.18, host_index 8
# ===========================================================================
# Addressing/hostname/tags computed by the shared module. svc_code=svc17,
# component=prometheus, vlan_id=20, host_index=8 -> ipv4 10.0.20.18
# (cidrhost("10.0.20.0/24", 10 + 8)), hostname "svc17-prometheus-20-01", tags
# ["proj-shared-prometheus"] (NET-00 §3-§4). This catalog line item deploys more
# than one distinct running service, so each component passes a `component` to
# the module -> the <svc-code>-<component>-<vlan_id>-<instance> hostname form
# (NET-00 §4 worked example), the same pattern SVC-07's unsealer uses.
module "prometheus" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"                    # shared platform service -> tag proj-shared-prometheus
  service_name = "prometheus"                # -> tags ["proj-shared-prometheus"] (PF §4)
  svc_code     = "svc17"                     # stable catalog slot (NET-00 §4)
  component    = "prometheus"                # -> hostname svc17-prometheus-20-01 (NET-00 §4)
  vlan_id      = local._vlan_id              # shared-services VLAN 20 (domain-rules §1)
  host_index   = local.prometheus_host_index # index 8 -> 10.0.20.18 (NET-00 §3)
  instance     = "01"                        # first observability guest -> svc17-prometheus-20-01

  # Every OTHER host_index on VLAN 20, from the ledger. The module's Guard 4
  # precondition fails at plan time if Prometheus's index collides with a peer.
  peer_host_indices = local.prometheus_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the root's guest resource below
}

# ===========================================================================
# GRAFANA — svc17-grafana-20-01, 10.0.20.19, host_index 9
# ===========================================================================
module "grafana" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"                 # shared platform service -> tag proj-shared-grafana
  service_name = "grafana"                # -> tags ["proj-shared-grafana"] (PF §4)
  svc_code     = "svc17"                  # stable catalog slot (NET-00 §4)
  component    = "grafana"                # -> hostname svc17-grafana-20-01 (NET-00 §4)
  vlan_id      = local._vlan_id           # shared-services VLAN 20 (domain-rules §1)
  host_index   = local.grafana_host_index # index 9 -> 10.0.20.19 (NET-00 §3)
  instance     = "01"                     # singleton grafana guest -> svc17-grafana-20-01

  peer_host_indices = local.grafana_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the root's guest resource below
}

# ===========================================================================
# LOKI — svc18-loki-20-01, 10.0.20.20, host_index 10
# ===========================================================================
module "loki" {
  source = "../../modules/proxmox-compute"

  project_slug = "shared"              # shared platform service -> tag proj-shared-loki
  service_name = "loki"                # -> tags ["proj-shared-loki"] (PF §4)
  svc_code     = "svc18"               # stable catalog slot (NET-00 §4)
  component    = "loki"                # -> hostname svc18-loki-20-01 (NET-00 §4)
  vlan_id      = local._vlan_id        # shared-services VLAN 20 (domain-rules §1)
  host_index   = local.loki_host_index # index 10 -> 10.0.20.20 (NET-00 §3)
  instance     = "01"                  # singleton loki guest -> svc18-loki-20-01

  peer_host_indices = local.loki_peer_host_indices

  ssh_public_keys = var.operator_ssh_public_keys # forward-compat echo; injection happens on the root's guest resource below
}

# ===========================================================================
# Ledger/root drift guards.
# ===========================================================================
# A `module` block cannot carry a `lifecycle` precondition (that block type is
# reserved there in Terraform), so these no-side-effect `terraform_data` anchors
# host the cross-checks: each EXPLICIT host_index literal MUST equal the ledger's
# corresponding svc17-18 VLAN-20 entry. If someone edits the ledger or this
# root's literals without the other, the plan FAILS CLOSED here before any
# Proxmox resource is created — it never drifts silently. (The within-VLAN
# collision check itself is the module's Guard 4 over `peer_host_indices`.)
resource "terraform_data" "ledger_index_assert" {
  input = {
    prometheus_host_index   = local.prometheus_host_index
    prometheus_ledger_index = local._prometheus_ledger_index
    grafana_host_index      = local.grafana_host_index
    grafana_ledger_index    = local._grafana_ledger_index
    loki_host_index         = local.loki_host_index
    loki_ledger_index       = local._loki_ledger_index
  }

  lifecycle {
    precondition {
      condition     = local.prometheus_host_index == local._prometheus_ledger_index
      error_message = "SVC-17/18 Prometheus host_index (${local.prometheus_host_index}) does not match the host-indices.yaml ledger entry for svc17/prometheus on VLAN 20 (${local._prometheus_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
    precondition {
      condition     = local.grafana_host_index == local._grafana_ledger_index
      error_message = "SVC-17/18 Grafana host_index (${local.grafana_host_index}) does not match the host-indices.yaml ledger entry for svc17/grafana on VLAN 20 (${local._grafana_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
    precondition {
      condition     = local.loki_host_index == local._loki_ledger_index
      error_message = "SVC-17/18 Loki host_index (${local.loki_host_index}) does not match the host-indices.yaml ledger entry for svc18/loki on VLAN 20 (${local._loki_ledger_index}). Keep infra/platform-foundation/host-indices.yaml and this root in sync."
    }
  }
}

# ===========================================================================
# PROMETHEUS container — svc17-prometheus-20-01, 10.0.20.18
# ===========================================================================
# The Prometheus + Alertmanager LXC. Unprivileged, nesting on (the in-container
# Docker engine the Compose stack needs runs inside an unprivileged LXC). Sized
# per SVC-17/18 requirements §6: Prometheus 2 vCPU / 4 GB / 100 GB, bumped to
# 3 vCPU / 4.5 GB to seat the co-located Alertmanager (+1 vCPU / +512 MB). Stock
# Debian 12 / Ubuntu 24.04 LTS template (Docker + Compose installed per host by
# the common role at provision time, ADR-0003). `prevent_destroy` seatbelt: the
# local TSDB (30-day metrics) + Alertmanager silence state live on this rootfs
# and must never be silently destroyed/replaced by an apply.
resource "proxmox_virtual_environment_container" "prometheus" {
  description = "SVC-17/18 Prometheus (+ co-located Alertmanager) — metrics scraping, local TSDB, PromQL, baseline alert routing. Managed by Terraform; see .kiro/specs/svc-17-18-observability."

  node_name = var.proxmox_node_name
  vm_id     = var.prometheus_vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 3 # 2 vCPU Prometheus + 1 vCPU co-located Alertmanager (requirements §6)
  }

  memory {
    dedicated = 4608 # 4 GB Prometheus + 512 MB co-located Alertmanager = 4.5 GB (requirements §6)
    swap      = 0    # no swap device — per-container swap cap (ADR-0006)
  }

  # Root filesystem: 100 GB on local-zfs for the local TSDB (30-day retention,
  # requirements §6). Persistent rootfs, never tmpfs, so samples survive a
  # restart.
  disk {
    datastore_id = var.prometheus_datastore_id
    size         = 100
  }

  operating_system {
    template_file_id = var.prometheus_template_file_id
    type             = var.prometheus_template_os_type # debian | ubuntu
  }

  # Hostname + computed static IPv4 injected from the module — never an operator
  # IP literal (NET-00 §3).
  initialization {
    hostname = module.prometheus.hostname # svc17-prometheus-20-01

    ip_config {
      ipv4 {
        address = module.prometheus.ip_config.ipv4.address # 10.0.20.18/24 (computed)
        gateway = module.prometheus.ip_config.ipv4.gateway # 10.0.20.1
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

  # Human-facing Proxmox tag proj-shared-prometheus, from the module. Distinct
  # from the machine-facing hostname above.
  tags = module.prometheus.tags

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: the Prometheus LXC holds the local TSDB (30 days of metrics) and
    # Alertmanager silence/notification state on its rootfs. It must never be
    # silently destroyed/replaced by an apply (which would discard all stored
    # samples). `prevent_destroy` makes Terraform HARD-ERROR on any
    # destroy/replace plan instead of offering a `yes` prompt an operator might
    # approve by reflex. A deliberate replacement goes through SVC-17/18's own
    # clean-slate contract. See the SVC-17/18 INSTALL-RUNBOOK destructive-plan
    # guardrail.
    prevent_destroy = true
  }
}

# ===========================================================================
# GRAFANA container — svc17-grafana-20-01, 10.0.20.19
# ===========================================================================
# The Grafana LXC. Unprivileged, nesting on. Sized per SVC-17/18 requirements
# §6: 1 vCPU / 1 GB / 10 GB. Stock Debian 12 / Ubuntu 24.04 LTS template (Docker
# + Compose per host via the common role, ADR-0003). NO `prevent_destroy`:
# Grafana is config-as-code — dashboards are version-controlled JSON and
# datasources/auth are re-provisioned from Ansible, so the guest is reproducible
# and need not carry the destroy seatbelt (design §3.1).
resource "proxmox_virtual_environment_container" "grafana" {
  description = "SVC-17/18 Grafana — dashboards + Prometheus/Loki datasources, reached externally only via Traefik (SVC-09). Config-as-code (dashboards are version-controlled JSON). Managed by Terraform; see .kiro/specs/svc-17-18-observability."

  node_name = var.proxmox_node_name
  vm_id     = var.grafana_vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 1 # 1 vCPU (requirements §6)
  }

  memory {
    dedicated = 1024 # 1 GB RAM (requirements §6)
    swap      = 0    # no swap device — per-container swap cap (ADR-0006)
  }

  # Root filesystem: 10 GB on local-zfs (requirements §6). The Grafana config DB
  # lives here but is config-as-code (re-provisioned), hence no destroy seatbelt.
  disk {
    datastore_id = var.grafana_datastore_id
    size         = 10
  }

  operating_system {
    template_file_id = var.grafana_template_file_id
    type             = var.grafana_template_os_type # debian | ubuntu
  }

  initialization {
    hostname = module.grafana.hostname # svc17-grafana-20-01

    ip_config {
      ipv4 {
        address = module.grafana.ip_config.ipv4.address # 10.0.20.19/24 (computed)
        gateway = module.grafana.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys; empty list => block omitted => strict no-op
      }
    }
  }

  network_interface {
    name   = "veth0"
    bridge = "p20" # VLAN-20 shared-services SDN VNet; VNet applies tag 20, NIC must not
  }

  tags = module.grafana.tags # proj-shared-grafana

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # No prevent_destroy: Grafana is config-as-code (dashboards are
    # version-controlled JSON, datasources/auth re-provisioned by Ansible), so
    # the guest is fully reproducible and does not carry the stateful-destroy
    # seatbelt the Prometheus/Loki guests do (design §3.1).
  }
}

# ===========================================================================
# LOKI container — svc18-loki-20-01, 10.0.20.20
# ===========================================================================
# The Loki LXC. Unprivileged, nesting on. Sized per SVC-17/18 requirements §6:
# 2 vCPU / 4 GB / 200 GB. Stock Debian 12 / Ubuntu 24.04 LTS template (Docker +
# Compose per host via the common role, ADR-0003). `prevent_destroy` seatbelt:
# the Loki index + chunk store (30 days of logs) lives on this rootfs and must
# never be silently destroyed/replaced by an apply.
resource "proxmox_virtual_environment_container" "loki" {
  description = "SVC-17/18 Loki — centralized log aggregation (monolithic mode, local filesystem store, 30-day retention), internal-only push API on VLAN 20. Managed by Terraform; see .kiro/specs/svc-17-18-observability."

  node_name = var.proxmox_node_name
  vm_id     = var.loki_vmid

  unprivileged = true
  features {
    nesting = true
  }

  cpu {
    cores = 2 # 2 vCPU (requirements §6)
  }

  memory {
    dedicated = 4096 # 4 GB RAM (requirements §6)
    swap      = 0    # no swap device — per-container swap cap (ADR-0006)
  }

  # Root filesystem: 200 GB on local-zfs for the Loki index/chunk store (30-day
  # retention, requirements §6). Persistent rootfs, never tmpfs.
  disk {
    datastore_id = var.loki_datastore_id
    size         = 200
  }

  operating_system {
    template_file_id = var.loki_template_file_id
    type             = var.loki_template_os_type # debian | ubuntu
  }

  initialization {
    hostname = module.loki.hostname # svc18-loki-20-01

    ip_config {
      ipv4 {
        address = module.loki.ip_config.ipv4.address # 10.0.20.20/24 (computed)
        gateway = module.loki.ip_config.ipv4.gateway # 10.0.20.1
      }
    }

    dynamic "user_account" {
      for_each = length(var.operator_ssh_public_keys) > 0 ? [1] : []
      content {
        keys = var.operator_ssh_public_keys # root-account SSH public keys; empty list => block omitted => strict no-op
      }
    }
  }

  network_interface {
    name   = "veth0"
    bridge = "p20" # VLAN-20 shared-services SDN VNet; VNet applies tag 20, NIC must not
  }

  tags = module.loki.tags # proj-shared-loki

  lifecycle {
    ignore_changes = [
      operating_system[0].template_file_id, # template rebuilds shouldn't churn the LXC
    ]
    # SEATBELT: the Loki LXC holds the index + chunk store (30 days of logs) on
    # its rootfs. It must never be silently destroyed/replaced by an apply
    # (which would discard all stored logs). `prevent_destroy` makes Terraform
    # HARD-ERROR on any destroy/replace plan instead of offering a `yes` prompt
    # an operator might approve by reflex. A deliberate replacement goes through
    # SVC-17/18's own clean-slate contract. See the SVC-17/18 INSTALL-RUNBOOK
    # destructive-plan guardrail.
    prevent_destroy = true
  }
}
