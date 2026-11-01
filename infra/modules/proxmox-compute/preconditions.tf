# Shared proxmox-compute module — INPUT-GUARD PRECONDITIONS (task 7.3).
#
# ==========================================================================
# Purpose
# ==========================================================================
# This file adds the derivation-layer input guards from design.md's
# "Error Handling" table (the three rows caught in the compute module) so a
# non-compliant provisioning request FAILS AT PLAN TIME, before any Proxmox
# resource is created:
#
#   | Failure                                  | Behavior                          | Req |
#   |------------------------------------------|-----------------------------------|-----|
#   | vlan_id == 10 requested for a guest      | reject: management-VLAN placement | 1.7 |
#   | hostname not unique within its VLAN      | reject: within-VLAN collision     | 3.6 |
#   | host index pushes IP past .254           | surface host-IP exhaustion        | 2.6 |
#   | not exactly one of svc_code / role_code  | reject: ambiguous/absent identity | ADR-0021 |
#
# Formatting note: terraform is not installed in this environment, so
# `terraform fmt`/`validate` could not be run here. This file is hand-formatted
# to the canonical fmt conventions (2-space indent, aligned `=` within a block,
# argument ordering) to match the sibling files; run `terraform fmt` when a
# toolchain is available to confirm.
#
# ==========================================================================
# Why a `terraform_data` guard anchor
# ==========================================================================
# Two of the three checks (hostname collision, host-IP exhaustion) are on
# COMPUTED values in locals.tf, and one (vlan_id == 10) is a cross-value
# rejection that is clearer as a placement precondition than as a bare input
# `validation` block. Terraform `validation` blocks live on a single variable
# and cannot reference `local.*` or other variables; preconditions can, and
# they evaluate during plan BEFORE any real resource is created. `terraform_data`
# is the provider-agnostic, no-side-effect resource whose sole job here is to
# carry these `lifecycle.precondition` blocks as a gate. Because it has no
# managed infrastructure of its own, a failing precondition aborts the plan
# without ever touching Proxmox — satisfying "reject/​surface BEFORE any
# resource is created" (Req 1.7, 2.6, 3.6).
#
# Pure-input range/shape checks that DON'T need computed values or another
# variable stay as `validation` blocks in variables.tf (e.g. host_index >= 0,
# instance in 01..99, svc_code shape when set, role_code shape when set). This
# file holds only the computed-value / cross-value guards, per the task's
# "preconditions for computed-value checks, validation for pure input ranges"
# split — including the ADR-0021 exactly-one-of-identity XOR (Guard 5), which is
# a CROSS-variable check and so cannot live in a single variable's validation.

resource "terraform_data" "input_guards" {
  # Nothing to manage — this anchor exists solely to host the preconditions
  # below. Keep an explicit trigger-free body so a re-plan with unchanged
  # inputs is a no-op (idempotency, Req 4.6/8.1). The `input` records the
  # guarded values purely for readability in `terraform plan` output.
  input = {
    vlan_id           = var.vlan_id
    host_index        = var.host_index
    hostname          = local.hostname
    last_octet        = 10 + var.host_index
    peer_host_indices = var.peer_host_indices
    svc_code          = var.svc_code
    role_code         = var.role_code
  }

  lifecycle {
    # ---------------------------------------------------------------------
    # Guard 1 — Management-VLAN placement rejection (Requirement 1.7).
    # VLAN 10 is management-only (Proxmox host/corosync/API); no guest VM or
    # LXC interface may ever land on it (NET-00 §2, domain-rules §1). Reject
    # before any resource is created and name the violation in the message.
    # ---------------------------------------------------------------------
    precondition {
      condition     = var.vlan_id != 10
      error_message = "Requirement 1.7 (management-VLAN placement): vlan_id == 10 is the management VLAN (Proxmox host/corosync/API only) and no guest VM/LXC interface may be placed on it. Provisioning is rejected before any resource is created. Use a shared-services (20) or per-project (100-254) VLAN."
    }

    # ---------------------------------------------------------------------
    # Guard 2 — Host-IP exhaustion (Requirement 2.6).
    # The host address is cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)
    # (locals.tf). Once 10 + host_index exceeds 254 the /24 is exhausted;
    # surface it as an explicit error rather than letting cidrhost wrap,
    # duplicate, or emit an out-of-range (.255 / cross-subnet) address.
    # (host_index >= 0 is already guaranteed by variables.tf's validation.)
    # ---------------------------------------------------------------------
    precondition {
      condition     = (10 + var.host_index) <= 254
      error_message = "Requirement 2.6 (host-IP exhaustion): host_index ${var.host_index} yields last octet ${10 + var.host_index} > 254, exhausting subnet 10.0.${var.vlan_id}.0/24. The address is NOT wrapped, duplicated, or emitted out of range — the failure is surfaced. A /24 seats host indices 0..244 (addresses .10-.254)."
    }

    # ---------------------------------------------------------------------
    # Guard 3 — Within-VLAN hostname uniqueness (Requirement 3.6).
    #
    # BOUNDARY NOTE — read carefully. A single module instance computes exactly
    # ONE hostname (locals.hostname = <svc-code>[-<component>]-<vlan_id>-
    # <instance>). "Uniqueness within a VLAN" is inherently a property of a SET
    # of hosts, which a single instance cannot see — the colliding peer lives in
    # a sibling module instance the caller fans out, not here. So the authoritative
    # within-VLAN collision check is enforced at the CALLING LAYER (the project
    # root / fan-out `for_each`), and by the pure-derivation guard
    # `assert_unique_hostnames` in the Python layer
    # (scripts/netfoundation/derivation.py, task 5.2) which receives the full set.
    #
    # What IS expressible and enforced here at the single-instance level:
    #   * The hostname is well-formed and non-empty (a malformed/empty hostname
    #     could otherwise silently collide with another malformed one).
    #   * Within one VLAN, a hostname is unique iff its (svc_code, component,
    #     instance) triple is unique for that vlan_id. Since vlan_id is fixed for
    #     this instance, this instance's contribution to VLAN-uniqueness is that
    #     its (svc_code[-component], instance) discriminator is present and
    #     well-formed. instance ("01".."99") and svc_code ("svcNN") shapes are
    #     already validated in variables.tf; here we assert the assembled
    #     hostname actually carries the -<vlan_id>-<instance> discriminator so
    #     two hosts in the same VLAN cannot collapse to the same name.
    #
    # The condition below is a structural well-formedness gate (the best a
    # single instance can express); cross-instance set-uniqueness is the caller's
    # / the Python guard's responsibility, as documented above. When the caller
    # is refactored to pass the peer hostname set into this module (e.g. via a
    # future `peer_hostnames` list input), this block is the ready-made seat for
    # a `!contains(var.peer_hostnames, local.hostname)` condition — the guard
    # structure is intentionally shaped to receive that list.
    # ---------------------------------------------------------------------
    #
    # ADR-0021 — the identity segment is EITHER a catalog `svc_code` (`svcNN`)
    # OR a non-catalog `role_code` (a lowercase role name, e.g. `workload`), so
    # the well-formedness regex accepts BOTH forms as the leading segment. The
    # intent is unchanged (structural well-formedness; cross-instance
    # set-uniqueness still lives at the calling layer + the Python
    # assert_unique_hostnames guard). An exactly-one-of-identity guard (Guard 5)
    # separately guarantees the leading segment came from exactly one of the two
    # inputs.
    # ---------------------------------------------------------------------
    precondition {
      condition     = can(regex("^(svc[0-9]{2}|[a-z][a-z0-9]*)(-[a-z0-9]([a-z0-9-]*[a-z0-9])?)?-${var.vlan_id}-[0-9]{2}$", local.hostname))
      error_message = "Requirement 3.6 (within-VLAN hostname collision): computed hostname '${local.hostname}' is not a well-formed '<identity>[-<component>]-${var.vlan_id}-<instance>' name — where <identity> is a catalog svc_code ('svcNN') or a non-catalog role_code (a lowercase role name, e.g. 'workload'; ADR-0021) — carrying the -<vlan_id>-<instance> discriminator that keeps hosts distinct within VLAN ${var.vlan_id}. NOTE: full cross-instance set-uniqueness within the VLAN is enforced at the calling layer (project-root fan-out) and by the Python assert_unique_hostnames guard, since a single module instance sees only its own hostname."
    }

    # ---------------------------------------------------------------------
    # Guard 5 — Exactly-one-of guest identity (ADR-0021).
    #
    # A guest is identified by EXACTLY ONE of a catalog `svc_code` (`svcNN`) OR
    # a non-catalog `role_code` (a role name such as `workload`) — never both,
    # never neither. The shapes of each (when supplied) are validated in their
    # own variable `validation` blocks; this cross-variable XOR check cannot
    # live in a variable `validation` block (those cannot reference another
    # variable), so it is a plan-time precondition here, failing closed before
    # any Proxmox resource is created and naming the offending combination.
    # `locals.identity_code = coalesce(svc_code, role_code)` relies on this: it
    # would error on (null, null) and silently ignore role_code on (set, set),
    # so this guard is what makes the coalesce well-defined.
    # ---------------------------------------------------------------------
    precondition {
      condition     = (var.svc_code == null) != (var.role_code == null)
      error_message = "ADR-0021 (exactly-one-of guest identity): a guest must supply EXACTLY ONE of svc_code (a catalog 'svcNN' slot) or role_code (a non-catalog role name, e.g. 'workload') — not both, not neither. Got svc_code=${var.svc_code == null ? "<null>" : var.svc_code}, role_code=${var.role_code == null ? "<null>" : var.role_code}. A catalog service sets svc_code; a non-catalog role-named guest (e.g. a consumer workload) sets role_code."
    }

    # ---------------------------------------------------------------------
    # Guard 4 — Within-VLAN host-index (host-IP) collision (Requirement 1.2).
    #
    # This is the IP-side analogue of the `peer_*` seat Guard 3's note above
    # describes as "intentionally shaped to receive". Where Guard 3 can only
    # assert single-instance hostname well-formedness (a single module instance
    # cannot see its peers), this guard CLOSES the cross-root gap for the host
    # index: the calling root threads in `peer_host_indices` — every OTHER
    # host_index already allocated on this guest's VLAN, read from the committed
    # per-VLAN ledger (host-indices.yaml, the Host_Index_Authority) — so the set
    # the single instance was missing is supplied externally.
    #
    # On a shared VLAN (20) or a project VLAN populated by more than one
    # independent Terraform root, no single `terraform plan` sees the other
    # roots' indices, so host_index uniqueness is NOT guaranteed by for_each
    # ordering (the origin of the live VLAN-20 .10 collision between SVC-07's
    # primary and SVC-09's Traefik). This precondition makes that collision a
    # hard PLAN-TIME failure, before any Proxmox resource is created, in the one
    # module that already computes the IP via
    # cidrhost("10.0.<vlan_id>.0/24", 10 + host_index).
    #
    # An empty peer set (the default) makes this a strict no-op — a single-guest
    # root behaves exactly as before (backward-compatible).
    # ---------------------------------------------------------------------
    precondition {
      condition     = !contains(var.peer_host_indices, var.host_index)
      error_message = "Requirement 1.2 (within-VLAN host-index collision): host_index ${var.host_index} on VLAN ${var.vlan_id} (IP 10.0.${var.vlan_id}.${10 + var.host_index}) collides with another guest already allocated that index on this VLAN — a within-VLAN host-IP collision across independent Terraform roots. Rejected at plan time before any Proxmox resource is created. Pick a distinct host_index for this guest from the per-VLAN host-index ledger (infra/platform-foundation/host-indices.yaml)."
    }
  }
}
