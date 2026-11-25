"""Control-node route check for platform VLANs (WS-C64).

The control node reaches every platform VLAN `10.0.<vlan>.0/24` (NET-00) through a static route via the Proxmox
host, which owns the VLAN gateway `.1` (ADR-0004). Anything else that routes an overlapping range at a better metric
(typically a workstation VPN claiming `10.0.0.0/16`) silently takes the traffic, and installers only fail after long
SSH timeouts. This check runs `ip -j route get 10.0.<vlan>.1` and passes only when the route goes via the Proxmox
host's LAN address, or when the control node IS the Proxmox host (the VLAN gateway address is local).

CLI (exit 0 = ok, 1 = wrong route, 2 = cannot decide)::

    route_check.py --env-file cluster.env --vlan 20 [--vlan 100]

Recorded shapes: tests/fixtures/live-shapes/ip-route-get.json. No secret is read or printed.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster_env  # noqa: E402


def gateway_address(vlan: int) -> str:
    return f"10.0.{int(vlan)}.1"


def evaluate(route: list[dict], vlan: int, proxmox_lan_ip: str) -> tuple[bool, str]:
    """Judge one `ip -j route get` result for the VLAN's gateway address."""
    gw = gateway_address(vlan)
    if not route:
        return False, f"no route to {gw} (VLAN {vlan})"
    r = route[0]
    dev, via, src = r.get("dev", "?"), r.get("gateway"), r.get("prefsrc")
    if via == proxmox_lan_ip:
        return True, f"VLAN {vlan}: {gw} via the Proxmox host {via} (dev {dev})"
    if via is None and src == gw:
        return True, f"VLAN {vlan}: control node is the Proxmox host ({gw} is local on {dev})"
    found = f"via {via} on {dev}" if via else f"directly on {dev}"
    return False, (
        f"VLAN {vlan}: traffic to {gw} goes {found}, not via the Proxmox host {proxmox_lan_ip}. "
        f"Another route (often a VPN claiming 10.0.0.0/16) overlaps the platform range, so every guest on this "
        f"VLAN would be unreachable. Disconnect it, or add a more specific route that wins over a /16: "
        f"`sudo ip route add 10.0.{vlan}.0/24 via {proxmox_lan_ip}` (WS-C64)."
    )


def proxmox_lan_ip(env: dict[str, str]) -> str | None:
    explicit = (env.get("PROXMOX_HOST_LAN_IP") or "").strip()
    if explicit:
        return explicit
    host = urlparse((env.get("PROXMOX_ENDPOINT") or "").strip()).hostname or ""
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        try:
            return socket.gethostbyname(host)
        except OSError:
            return None


def route_get(addr: str) -> list[dict]:
    out = subprocess.run(["ip", "-j", "route", "get", addr], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return []
    return json.loads(out.stdout or "[]")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", required=True)
    ap.add_argument("--vlan", action="append", type=int, default=[])
    a = ap.parse_args(argv)
    env = cluster_env.load_env_file(a.env_file)
    lan = proxmox_lan_ip(env)
    if not lan:
        print(json.dumps({"ok": False, "messages": ["cannot determine the Proxmox host LAN address: set "
                                                    "PROXMOX_HOST_LAN_IP in the cluster env file"]}))
        return 2
    results = [evaluate(route_get(gateway_address(v)), v, lan) for v in (a.vlan or [20])]
    ok = all(r[0] for r in results)
    print(json.dumps({"ok": ok, "messages": [m for _, m in results]}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
