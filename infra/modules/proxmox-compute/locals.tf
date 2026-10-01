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
#   * hostname      = <svc-code>[-<component>]-<vlan_id>-<instance>       (Req 3.1, 3.2)
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
# (and its concrete vmid) is wired when the guest-provisioning inputs are added
# in a later increment. vmid/service_role outputs are documented stand-ins
# until then (see outputs.tf). The address/hostname/tags derivation IS final
# and matches the design contract exactly.
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
  # Hostname (Requirement 3.1, 3.2, NET-00 §4) — machine-facing, VLAN-correlated.
  # <svc-code>-<vlan_id>-<instance>, or <svc-code>-<component>-<vlan_id>-<instance>
  # WHERE one catalog line item deploys more than one distinct running service.
  # The instance is already validated as a zero-padded 2-digit "01".."99" in
  # variables.tf, and svc_code is validated as the stable catalog slot "svcNN"
  # (never a product/technology name — Requirement 3.5).
  # ------------------------------------------------------------------------
  hostname = var.component == null ? "${var.svc_code}-${var.vlan_id}-${var.instance}" : "${var.svc_code}-${var.component}-${var.vlan_id}-${var.instance}"

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
  # vmid — documented stand-in (see scope decision above). No guest resource
  # exists in this module yet, so there is no provider-assigned VMID to echo.
  # Left null so the output is present in the contract; a later increment that
  # adds the guest resource sets this to
  # proxmox_virtual_environment_{vm,container}.this.vm_id.
  # ------------------------------------------------------------------------
  vmid = null

  # ------------------------------------------------------------------------
  # service_role — the role the Ansible dynamic inventory groups this host by
  # (PF §8, integration-boundaries §4). It is the service identity, which is
  # `service_name` in this module's contract.
  # ------------------------------------------------------------------------
  service_role = var.service_name
}
