# SVC-01 SQL Database Terraform root — inputs (reusable, per-project).
#
# Unlike the SHARED roots (SVC-07/SVC-09) which hardcode vlan_id=20, this root is
# REUSABLE across projects, so it ACCEPTS `project_slug` + `vlan_id` as inputs:
# the same code provisions dronefleet's DB on VLAN 100 and the ZITADEL backing DB
# on VLAN 20. (This is distinct from the per-project _TEMPLATE root, which DERIVES
# its vlan_id from the registry because it IS the project root; SVC-01 is a
# service module a project root or a shared service points at, so it is told the
# VLAN rather than owning the registry lookup.) There is deliberately NO IP
# literal: the host IP is COMPUTED by proxmox-compute from vlan_id + host_index.

variable "project_slug" {
  description = <<-EOT
    The consuming project's slug (lowercase, hyphenated, <=20 chars — PF §4).
    Drives the Proxmox tag `proj-<slug>-postgres`, the database name default
    `<slug>`, and the app role `<slug>_app`. For the ZITADEL backing DB this is
    `shared-zitadel`; for a throwaway test it is the test slug.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$", var.project_slug))
    error_message = "project_slug must be lowercase alphanumeric/hyphen, 1-20 chars (PF §4)."
  }
}

variable "vlan_id" {
  description = <<-EOT
    The VLAN the DB LXC is placed on. A per-project VLAN (100-254) for a
    dedicated project DB, or 20 for a shared-service backing DB (e.g. ZITADEL).
    The DB's NIC attaches to the existing SDN VNet `p<vlan_id>` — this root
    REFERENCES that VNet, it does not create it (the project root / platform
    foundation owns VNet creation). The host IP is `cidrhost("10.0.<vlan_id>.0/24",
    10 + host_index)` (NET-00 §3), computed by proxmox-compute.
  EOT
  type        = number

  validation {
    condition     = var.vlan_id == 20 || (var.vlan_id >= 100 && var.vlan_id <= 254)
    error_message = "vlan_id must be 20 (shared-service backing DB) or 100-254 (per-project DB) — never the management VLAN 10 or the reserved 30-99 range (NET-00 §2)."
  }
}

variable "host_index" {
  description = "Zero-based host position within the VLAN (host IP = .1(0+index)). Default 0 -> .10. Lets a project place its DB at a known offset distinct from other guests on the same VLAN. On a shared VLAN carrying more than one service root (VLAN 20, or a project VLAN with >1 guest), this MUST be the ledger-assigned index (e.g. 4 for the shared-zitadel backing DB), not the bare default 0 — the installer passes it via `-var host_index=` / `-e host_index=` from the Host_Index_Authority ledger."
  type        = number
  default     = 0
}

variable "peer_host_indices" {
  description = <<-EOT
    EXPLICIT override of the set of OTHER host indices already allocated on this
    DB's VLAN — every host_index on this VLAN except this guest's own. Threaded
    into the shared proxmox-compute module's Guard-4 plan-time precondition to
    make a within-VLAN host-index collision a hard plan-time failure (NOT a silent
    live clash) BEFORE any Proxmox resource is created.

    Default `[]` (empty) means "do not override": the root then DERIVES the peer
    set from the committed per-VLAN ledger
    (infra/platform-foundation/host-indices.yaml, the Host_Index_Authority) for
    this DB's vlan_id, excluding this guest's own entry (see locals below). A
    single-guest project VLAN that has NO ledger entry derives an empty peer set,
    so the precondition is a strict no-op and the apply behaves exactly as before
    (backward-compatible). Supply this explicitly only to bypass the ledger read
    (e.g. a sandbox where the ledger file is unavailable and the caller already
    knows the peer set).
  EOT
  type        = list(number)
  default     = []

  validation {
    # Pure-input RANGE guard only (mirrors proxmox-compute / host_index): each
    # supplied peer index is a zero-based position and must be >= 0. Cross-value
    # collision detection against host_index lives in the proxmox-compute Guard 4.
    condition     = alltrue([for i in var.peer_host_indices : i >= 0])
    error_message = "peer_host_indices entries are zero-based positions and must each be >= 0 (NET-00 §3)."
  }
}

variable "ledger_component" {
  description = <<-EOT
    OPTIONAL ledger self-key discriminator. On a shared VLAN a single svc_code
    (svc01) can own more than one guest (e.g. the shared-zitadel backing DB vs a
    future second SVC-01 instance), so the ledger entry for THIS guest is keyed by
    (svc_code=svc01, component, instance). This var names the `component` of this
    guest's own ledger row so the derived peer set can exclude exactly this
    guest's index and nothing else. For the ZITADEL backing DB this is
    `shared-zitadel`. Null/empty (the default) matches a ledger row that has no
    `component` (the common single-SVC-01-per-VLAN case). Ignored entirely when
    peer_host_indices is supplied explicitly, or when this DB's vlan_id is absent
    from the ledger.
  EOT
  type        = string
  default     = ""
}

variable "db_engine" {
  description = <<-EOT
    The relational engine: `postgres` (default) or `mariadb` (strict-EU
    alternative, MariaDB Foundation Helsinki — domain-rules §4). Selectable via
    the same module so switching engines does not require a different pipeline
    (Req 7). Drives db_port (5432 vs 3306) and which Ansible configurator role
    runs. The hostname is keyed to the stable SVC-01 slot either way (svc01-...).
  EOT
  type        = string
  default     = "postgres"

  validation {
    condition     = contains(["postgres", "mariadb"], var.db_engine)
    error_message = "db_engine must be 'postgres' (default) or 'mariadb' (NET-00/domain-rules §4)."
  }
}

variable "tier" {
  description = <<-EOT
    Sizing tier: `small` (1 vCPU / 2 GB / 20 GB — typical embedded/drone metadata
    store) or `medium` (4 vCPU / 8 GB / 100 GB — ML metadata/embeddings/higher
    write throughput). Maps to cpu/memory/disk via locals below; each is
    individually overridable for an off-tier size.
  EOT
  type        = string
  default     = "small"

  validation {
    condition     = contains(["small", "medium"], var.tier)
    error_message = "tier must be 'small' or 'medium' (SVC-01 §6 sizing tiers)."
  }
}

variable "cpu_cores" {
  description = "Override vCPU count. Null (default) -> the tier default (small=1, medium=4)."
  type        = number
  default     = null
}

variable "memory_mb" {
  description = "Override RAM (MB). Null (default) -> the tier default (small=2048, medium=8192)."
  type        = number
  default     = null
}

variable "disk_gb" {
  description = "Override rootfs size (GB). Null (default) -> the tier default (small=20, medium=100)."
  type        = number
  default     = null
}

variable "proxmox_node_name" {
  description = "Proxmox VE node the DB LXC is created on. Supplied per-cluster at apply time."
  type        = string
}

variable "svc01_template_file_id" {
  description = <<-EOT
    Proxmox volume ID of the STOCK LXC template (Debian 12 / Ubuntu 24.04 LTS via
    pveam). Docker + Compose installed per host by the `common` role (ADR-0003).
    Form `<datastore>:vztmpl/<file>`. The installer feeds this from the generic
    cluster template key (TF_VAR_openbao_template_file_id) via svc01_tf_env.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[^:]+:vztmpl/.+", var.svc01_template_file_id))
    error_message = "svc01_template_file_id must be '<datastore>:vztmpl/<file_name>'."
  }
}

variable "svc01_template_os_type" {
  description = "operating_system.type for the template — `debian` or `ubuntu`."
  type        = string
  default     = "debian"

  validation {
    condition     = contains(["debian", "ubuntu"], var.svc01_template_os_type)
    error_message = "svc01_template_os_type must be 'debian' or 'ubuntu'."
  }
}

variable "svc01_datastore_id" {
  description = "Proxmox storage pool for the LXC rootfs + Postgres data (default local-zfs per PF §6/§7)."
  type        = string
  default     = "local-zfs"
}

variable "svc01_vmid" {
  description = "Proxmox VMID for the DB LXC. Supplied explicitly so instances get stable, non-colliding IDs."
  type        = number
}

variable "enable_pgvector" {
  description = "Enable the pgvector extension for this project's database (vector search). PostgreSQL only."
  type        = bool
  default     = false
}

variable "enable_postgis" {
  description = "Enable the PostGIS extension (geospatial, e.g. drone telemetry). PostgreSQL only."
  type        = bool
  default     = false
}

variable "enable_timescaledb" {
  description = "Enable the TimescaleDB extension (time-series). PostgreSQL only."
  type        = bool
  default     = false
}

variable "app_subnet_cidr" {
  description = <<-EOT
    The project's application-tier subnet CIDR that inbound 5432 is scoped to.
    Default `10.0.<vlan_id>.0/24` (the project's own subnet). Consumed by the
    OPT-IN, default-OFF firewall-rules resource (enable_firewall_rules). The
    primary isolation is the VLAN-per-project L2 model + the private VLAN-bound
    port binding; the firewall rule is defence-in-depth, inert until the operator
    enables the pve-firewall master switch (mirrors ADR-0008's gated posture).
  EOT
  type        = string
  default     = ""
}

variable "enable_firewall_rules" {
  description = <<-EOT
    OPT-IN (default FALSE): declare a Proxmox guest firewall-rules resource
    scoping inbound 5432 to app_subnet_cidr. OFF by default and REVERSIBLE,
    mirroring ADR-0008: enabling the per-NIC/pve-firewall path can disturb guest
    egress on unprivileged LXC, so it is never flipped on by a merge — it is a
    deliberate, operator-gated step. With it off, isolation rests on the
    VLAN-per-project L2 model + the private VLAN-bound 5432 binding, which do not
    require the host firewall.
  EOT
  type        = bool
  default     = false
}

variable "operator_ssh_public_keys" {
  description = <<-EOT
    Operator SSH PUBLIC key(s) injected into the DB LXC root account so Ansible
    can reach it. PUBLIC keys only; sourced from TF_VAR_operator_ssh_public_keys /
    gitignored .env, never committed. Default [] = strict no-op (SSH-unreachable
    until supplied).
  EOT
  type        = list(string)
  default     = []
}

locals {
  # Tier -> default sizing (SVC-01 §6). Individual overrides win when non-null.
  _tier_defaults = {
    small  = { cpu = 1, mem = 2048, disk = 20 }
    medium = { cpu = 4, mem = 8192, disk = 100 }
  }
  svc01_cpu_cores = var.cpu_cores != null ? var.cpu_cores : local._tier_defaults[var.tier].cpu
  svc01_memory_mb = var.memory_mb != null ? var.memory_mb : local._tier_defaults[var.tier].mem
  svc01_disk_gb   = var.disk_gb != null ? var.disk_gb : local._tier_defaults[var.tier].disk

  # Wire-protocol port by engine (NOT proxied by Traefik — SVC-01 §12 exception).
  svc01_db_port = var.db_engine == "mariadb" ? 3306 : 5432

  # The project's SDN VNet this DB attaches to — referenced, not created.
  svc01_vnet_id = "p${var.vlan_id}"

  # Firewall scope defaults to the project's own /24 when not supplied.
  svc01_app_subnet_cidr = var.app_subnet_cidr != "" ? var.app_subnet_cidr : "10.0.${var.vlan_id}.0/24"

  # ==========================================================================
  # Host_Index_Authority ledger read (fix-vlan-host-index-collision, task 8 / C9)
  # ==========================================================================
  #
  # SVC-01 is the REUSABLE per-project/service DB root. Unlike the single-purpose
  # shared roots (SVC-07/SVC-09 are hardwired to VLAN 20), the SAME code provisions
  # a dedicated project DB on VLAN 100+ AND the shared-zitadel backing DB on VLAN
  # 20. On any VLAN that carries MORE THAN ONE independent Terraform root (VLAN 20,
  # or a project VLAN with >1 guest), host_index is NOT unique-by-for_each
  # construction — each root picks from zero with no shared counter (this is the
  # live VLAN-20 .10 collision this spec fixes). The coordination mechanism is the
  # committed per-VLAN ledger (infra/platform-foundation/host-indices.yaml), read
  # the same read-only way projects.yaml is (yamldecode(file(...)), NEVER state;
  # one-state-per-service preserved). The cidrhost("10.0.<vlan>.0/24", 10 +
  # host_index) formula is UNCHANGED — only HOW host_index is made unique across
  # roots is coordinated here.
  #
  # This root threads `peer_host_indices` into the proxmox-compute module so its
  # Guard-4 plan-time precondition fails CLOSED on a within-VLAN host-index
  # collision. The peer set is EITHER the explicit var.peer_host_indices override
  # OR — the normal path — DERIVED from the ledger for THIS DB's vlan_id, excluding
  # this guest's own (svc01 + ledger_component) entry.
  #
  # FAIL-OPEN POLICY (precise): the derived peer set is empty ONLY when this DB's
  # vlan_id is GENUINELY ABSENT from the ledger — i.e. a single-guest project VLAN
  # with no ledger entry at all, which must keep behaving exactly as today (empty
  # peers => Guard 4 is a strict no-op). When the VLAN IS present in the ledger,
  # peers are computed from the present entries even if no row matches this exact
  # (svc01, component) self-key — a present VLAN never silently collapses to empty.
  #
  # ROBUSTNESS: a missing ledger FILE (stripped sandbox / differing path) also
  # yields an empty structure (fileexists guard), so a path mismatch never breaks
  # the apply; supply var.peer_host_indices explicitly to enforce a set in that
  # case.

  # Parse the ledger if the file exists; else an empty map (missing-file fail-open).
  svc01_host_index_ledger_path = "${path.module}/../../platform-foundation/host-indices.yaml"
  svc01_host_index_ledger      = fileexists(local.svc01_host_index_ledger_path) ? yamldecode(file(local.svc01_host_index_ledger_path)) : {}

  # yamldecode renders top-level integer keys as STRINGS, so look up this DB's VLAN
  # as its string form. `svc01_vlan_present` is TRUE iff this vlan_id is a key in
  # the ledger — the discriminator for the fail-open policy above (absent VLAN =>
  # empty; present VLAN => compute from present entries).
  svc01_vlan_key     = tostring(var.vlan_id)
  svc01_vlan_present = contains(keys(local.svc01_host_index_ledger), local.svc01_vlan_key)
  svc01_vlan_entries = local.svc01_vlan_present ? local.svc01_host_index_ledger[local.svc01_vlan_key] : []

  # This guest's own ledger self-key: svc_code=svc01 AND the component the caller
  # named (ledger_component; empty matches a row with no `component`). Used ONLY to
  # EXCLUDE self from the derived peer set — never to require a match (a present
  # VLAN with no matching self-row still computes peers from the present entries).
  svc01_peer_from_ledger = [
    for e in local.svc01_vlan_entries : e.host_index
    if !(
      try(e.svc_code, "") == "svc01"
      && try(e.component, "") == var.ledger_component
    )
  ]

  # Final peer set threaded into proxmox-compute:
  #   * explicit var.peer_host_indices override wins when non-empty;
  #   * else the ledger-derived set for a PRESENT vlan_id;
  #   * else [] (vlan_id absent from the ledger OR ledger file missing) — the
  #     single-guest no-op that preserves today's behavior.
  svc01_peer_host_indices = length(var.peer_host_indices) > 0 ? var.peer_host_indices : local.svc01_peer_from_ledger
}
