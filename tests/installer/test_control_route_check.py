"""WS-C64: fail fast when the control node's route to a platform VLAN does not go through the Proxmox host.

Bug condition (2026-11-19 rebuild): a workstation VPN routed 10.0.0.0/16 at a lower metric, every guest became
unreachable mid-run, and installers failed only after long SSH timeouts. Shapes are recorded in
tests/fixtures/live-shapes/ip-route-get.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "installer"))
import route_check as rc  # noqa: E402

FX = json.loads((REPO / "tests/fixtures/live-shapes/ip-route-get.json").read_text())
LAN = FX["proxmox_lan_ip"]


def test_fixture_and_symbol_exist():
    assert callable(rc.evaluate) and set(FX["cases"]) >= {"workstation_via_proxmox", "vpn_captures_range"}


def test_route_via_the_proxmox_host_passes():
    ok, msg = rc.evaluate(FX["cases"]["workstation_via_proxmox"], 20, LAN)
    assert ok, msg


def test_control_node_on_the_proxmox_host_passes():
    ok, msg = rc.evaluate(FX["cases"]["on_proxmox_host"], 20, LAN)
    assert ok, msg


def test_vpn_capturing_the_range_fails_naming_the_interface_and_remedy():
    ok, msg = rc.evaluate(FX["cases"]["vpn_captures_range"], 20, LAN)
    assert not ok
    assert "tun0" in msg and f"10.0.20.0/24 via {LAN}" in msg


def test_missing_platform_route_fails():
    ok, msg = rc.evaluate(FX["cases"]["default_route_only"], 20, LAN)
    assert not ok and "192.0.2.1" in msg


def test_vlan_gateway_address():
    assert rc.gateway_address(20) == "10.0.20.1" and rc.gateway_address(100) == "10.0.100.1"


_PREFLIGHTS = ["svc01", "svc03", "svc06", "svc07", "svc08", "svc09", "svc10", "mailcatcher", "obs", "consumer"]


def test_every_service_preflight_runs_the_route_check():
    missing = []
    for code in _PREFLIGHTS:
        tasks = yaml.safe_load((REPO / f"ansible/roles/{code}_preflight/tasks/main.yml").read_text()) or []
        names = [str((t.get("ansible.builtin.include_role") or t.get("ansible.builtin.import_role") or {}).get("name"))
                 for t in tasks if isinstance(t, dict)]
        if "control_route_check" not in names:
            missing.append(code)
    assert not missing, missing
