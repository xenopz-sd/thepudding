"""netfoundation — pure derivation layer for the Proxmox network foundation.

This package holds the deterministic, side-effect-free formulas that turn a
project slug plus onboarding order into a VLAN ID, a /24 CIDR, a gateway, a
host IP, and (in later tasks) a hostname/tag. It is the single, testable
source of the allocation, addressing, and hostname/tag math described in
``NET-00-vlan-ip-addressing-plan.md`` and the feature design's Correctness
Properties.

The onboarding script (``onboard_project.py``) and the property tests import
from this package rather than re-implementing the formulas.
"""

from netfoundation.derivation import (
    HostAddressExhaustionError,
    HostnameCollisionError,
    ManagementVlanPlacementError,
    OperatorIpLiteralError,
    VlanExhaustionError,
    assert_unique_hostnames,
    gateway_ip,
    host_ip,
    hostname,
    next_vlan_id,
    parse_hostname,
    proxmox_tag,
    reject_ip_literal,
    reject_management_vlan,
    subnet_cidr,
)
from netfoundation.registry_views import (
    DEFAULT_STATUS,
    VALID_STATUSES,
    active_vlan_ids,
    all_vlan_ids,
    normalize_status,
)

__all__ = [
    "DEFAULT_STATUS",
    "HostAddressExhaustionError",
    "HostnameCollisionError",
    "ManagementVlanPlacementError",
    "OperatorIpLiteralError",
    "VALID_STATUSES",
    "VlanExhaustionError",
    "active_vlan_ids",
    "all_vlan_ids",
    "assert_unique_hostnames",
    "gateway_ip",
    "host_ip",
    "hostname",
    "next_vlan_id",
    "normalize_status",
    "parse_hostname",
    "proxmox_tag",
    "reject_ip_literal",
    "reject_management_vlan",
    "subnet_cidr",
]
