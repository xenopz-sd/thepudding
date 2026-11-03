# Shared proxmox-compute module — computed derivation layer (task 7.2).
#
# ==========================================================================
# Scope decision (task 7.2)
# ==========================================================================
# Task 7.1 scaffolded the input/output CONTRACT (variables.tf, outputs.tf) and
# left every local a TODO(7.2) placeholder. This file now implements the REAL
# derivation the design's "Shared proxmox-compute module — input/output
# contract" table prescribes:
#
#   * ipv4_address  = cidrhost("10.0.<vlan_id>.0/24", 10 + host_index)   (Req 2.1, 2.3)
#   * gateway       = cidrhost("10.0.<vlan_id>.0/24", 1) = 10.0.<vlan_id>.1 (Req 2.1)
#   * hostname      = <identity>[-<component>]-<vlan_id>-<instance>       (Req 3.1, 3.2)
#                     where <identity> is the catalog svc_code OR the non-catalog
#                     role_code (ADR-0021)
#   * tags          = ["proj-<slug>-<service>"]  (distinct from hostname)  (Req 3.4)
#   * ip_config     = a cloud-init initialization.ip_config.ipv4 structure that a
#                     compute resource injects; SDN IPAM/DHCP stays disabled    (Req 2.5)
#
# MODULE SCOPE — locals + documented cloud-init structure, NOT a full guest
# resource. Task 7.1's input contract is deliberately minimal (project_slug,
# service_name, svc_code, vlan_id, host_index, instance, component). It carries
# none of the inputs a real proxmox_virtual_environment_{vm,container} needs
# (node_name, template/clone source, disk datastore+size, cpu, memory, ssh
# keys, datastore for the cloud-init disk). Declaring a full guest here would
# require inventing that entire input surface — scope beyond both 7.1's
# contract and the design's stated change ("thread vlan_id -> computed
# ip_config + hostname; reject operator IP literals"). Per the task's
# documented fallback, this module therefore produces the computed locals and a
# ready-to-consume cloud-init `ip_config` structure; the actual guest resource
# still lives in each calling root. The `vmid` local is now COMPUTED
# deterministically (vlan_id * 1000 + host_index — proxmox-compute-deterministic-vmid,
# ADR-0022), the VMID-side analogue of the computed IP, so a root's container can
# read `module.<x>.vmid` instead of an operator-chosen ID. The address / hostname
# / tags / vmid derivation IS final and matches the design contract exactly.
#
# Input-guard preconditions (reject vlan_id == 10, within-VLAN hostname
# collision, host-IP exhaustion) are task 7.3 and are intentionally NOT here.

locals {
  # ------------------------------------------------------------------------
  # Subnet / gateway (Requirement 2.1, NET-00 §3).
  # The /24 CIDR whose third octet IS the VLAN ID (reversible by inspection);
  # the gateway is always the .1 host of that subnet.
  # ------------------------------------------------------------------------
  subnet_cidr = "10.0.${var.vlan_id}.0/24"
  gateway     = cidrhost(local.subnet_cidr, 1) # = 10.0.<vlan_id>.1

  # ------------------------------------------------------------------------
  # Host IPv4 (Requirement 2.3, NET-00 §3).
  # cidrhost("10.0.<vlan_id>.0/24", 10 + host_index): index 0 -> .10, index 1
  # -> .11, ... The .2-.9 band is intentionally left unallocated (Requirement
  # 2.2). `ipv4_address` is the bare address for the Ansible-inventory output;
  # `ipv4_cidr` is the CIDR-suffixed form the cloud-init ip_config block wants.
  # ------------------------------------------------------------------------
  ipv4_address = cidrhost(local.subnet_cidr, 10 + var.host_index) # e.g. 10.0.102.10
  ipv4_cidr    = "${local.ipv4_address}/24"                       # e.g. 10.0.102.10/24

  # ------------------------------------------------------------------------
  # Guest identity code (ADR-0021). A guest is identified by EXACTLY ONE of a
  # catalog `svc_code` (`svcNN`) OR a non-catalog `role_code` (a role name such
  # as `workload`). `coalesce` picks whichever is non-null — the exactly-one-of
  # invariant (never both, never neither) is enforced at plan time by Guard 5
  # in preconditions.tf, so here exactly one is guaranteed to be set and
  # `identity_code` is that value. For a catalog caller (svc_code set, role_code
  # null) this is identical to the pre-ADR-0021 behavior: identity_code ==
  # svc_code, so the hostname below is byte-for-byte unchanged.
  # ------------------------------------------------------------------------
  identity_code = coalesce(var.svc_code, var.role_code)

  # ------------------------------------------------------------------------
  # Hostname (Requirement 3.1, 3.2, NET-00 §4) — machine-facing, VLAN-correlated.
  # <identity_code>-<vlan_id>-<instance>, or
  # <identity_code>-<component>-<vlan_id>-<instance> WHERE one catalog line item
  # deploys more than one distinct running service. The identity code is the
  # catalog `svc_code` ("svcNN", stable catalog slot — Requirement 3.5) or the
  # non-catalog `role_code` (a role name, e.g. "workload" — ADR-0021); both are
  # swap-stable. The instance is already validated as a zero-padded 2-digit
  # "01".."99" in variables.tf.
  # ------------------------------------------------------------------------
  hostname = var.component == null ? "${local.identity_code}-${var.vlan_id}-${var.instance}" : "${local.identity_code}-${var.component}-${var.vlan_id}-${var.instance}"

  # ------------------------------------------------------------------------
  # Proxmox tags (Requirement 3.4, PF §4) — human-facing, project-correlated.
  # proj-<slug>-<service>. This is a DISTINCT convention from the hostname
  # above: the hostname is keyed on the stable catalog slot + VLAN ID (survives
  # a technology swap), the tag is keyed on project slug + service name (for
  # Proxmox-UI / Ansible filtering). They are neither equal nor derived from
  # one another. Proxmox `tags` is a list attribute, hence the single-element
  # list wrapping the value.
  # ------------------------------------------------------------------------
  proxmox_tag = "proj-${var.project_slug}-${var.service_name}"
  tags        = [local.proxmox_tag]

  # ------------------------------------------------------------------------
  # Cloud-init ip_config structure (Requirement 2.5, NET-00 §3).
  # Shaped to match bpg/proxmox's initialization.ip_config.ipv4 block exactly
  # (see the provider's cloud-init guide): a CIDR `address` and a `gateway`.
  # A consuming guest resource injects this as:
  #
  #   initialization {
  #     ip_config {
  #       ipv4 {
  #         address = module.compute.ip_config.ipv4.address   # 10.0.<vlan>.<host>/24
  #         gateway = module.compute.ip_config.ipv4.gateway   # 10.0.<vlan>.1
  #       }
  #     }
  #   }
  #
  # The address is Terraform-COMPUTED, never operator-supplied (Requirement
  # 2.4 — variables.tf declares no IP-literal input at all). The Proxmox SDN
  # built-in IPAM/DHCP is deliberately NOT used: the zone/subnet set no `ipam`
  # attribute and no dhcp_* range, so the guest gets its static address solely
  # from this cloud-init block (Requirement 2.5, NET-00 §5).
  # ------------------------------------------------------------------------
  ip_config = {
    ipv4 = {
      address = local.ipv4_cidr
      gateway = local.gateway
    }
  }

  # ------------------------------------------------------------------------
  # vmid — deterministic, collision-free, reversible Proxmox VMID
  # (proxmox-compute-deterministic-vmid, ADR-0022). This is the VMID-side
  # analogue of the host-IP formula above: it derives the VMID from the SAME two
  # stable inputs (vlan_id + host_index), so no operator chooses a VMID and
  # collisions are impossible platform-wide.
  #
  #   vmid = vlan_id * 1000 + host_index
  #     VLAN 20  idx 0 -> 20000 ; VLAN 100 idx 1 -> 100001 ; VLAN 254 idx 244 -> 254244
  #
  # COLLISION-FREE: host_index is bounded 0..244 (preconditions.tf Guard 2:
  # 10 + host_index <= 254), so host_index < 1000 and each vlan_id owns a
  # distinct 1000-wide band. For two guests vmid1 = v1*1000+h1, vmid2 =
  # v2*1000+h2 with 0 <= h < 1000: vmid1 == vmid2 forces v1 == v2 AND h1 == h2
  # (the right side of (v1-v2)*1000 = h2-h1 has magnitude < 1000). Distinct
  # (vlan_id, host_index) pairs => distinct VMIDs, no overflow/aliasing.
  # REVERSIBLE by inspection, exactly like the IP: vlan_id = vmid / 1000,
  # host_index = vmid % 1000 — an operator reading 100001 knows VLAN 100,
  # host_index 1 (cf. reading 10.0.100.11).
  # LEGAL BAND: min 20000 (VLAN 20, idx 0), max 254244 (VLAN 254, idx 244) — well
  # within Proxmox's 100..999999999 (asserted defensively in preconditions.tf
  # Guard 6, which cannot fire while Guards 1/2 hold but guards a future bound
  # relaxation).
  #
  # The committed per-VLAN host-index ledger + Guard 4 (ADR-0015) already make
  # (vlan_id, host_index) unique WITHIN a VLAN across independent roots, so a
  # formula keyed on that pair inherits within-VLAN collision-freeness; distinct
  # VLANs never collide because each owns its own 1000-block. The parallel pure
  # Python mirror (netfoundation.proxmox_vmid) is cross-checked against this HCL
  # computation by the golden terraform-plan test (ADR-0022 drift guard).
  #
  # Each consuming root's container `vm_id` is
  #   var.<service>_vmid != 0 ? var.<service>_vmid : module.<x>.vmid
  # so an operator override wins when supplied (grandfathers an already-
  # provisioned guest's live VMID — no force-replace) and this computed value is
  # used otherwise (ADR-0022 grandfather posture).
  # ------------------------------------------------------------------------
  vmid = var.vlan_id * 1000 + var.host_index

  # ------------------------------------------------------------------------
  # service_role — the role the Ansible dynamic inventory groups this host by
  # (PF §8, integration-boundaries §4). It is the service identity, which is
  # `service_name` in this module's contract.
  # ------------------------------------------------------------------------
  service_role = var.service_name
}
