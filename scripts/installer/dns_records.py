#!/usr/bin/env python3
"""Compute the SVC-10 platform-zone records from the generated inventories (spec svc-10-internal-dns, D2/D3).

Pure and stdlib-only except PyYAML (already in the venv). The installer calls it on the control node:

  dns_records.py --domain <platform-domain> --traefik-ip 10.0.20.12 --current <zone listing> \\
      ansible/inventory/*.generated.yml

It prints a JSON plan ``{"upsert": [...], "delete": [...], "desired_count": n}`` that the configurator applies with
``pdnsutil``. Desired records (design D2):

* ``*.<domain>`` and ``*.web.<domain>`` A -> the Traefik guest (every HTTP name, consumer ``expose`` hosts and
  website hosts resolve without a per-service record);
* one A record per guest hostname found in any generated inventory -> its ``ansible_host``.

Only A records are managed: SOA, NS and anything else (for example ACME ``_acme-challenge`` TXT records written by a
DNS-01 client) are never upserted or deleted. Records come from the file-mediated inventories only, never from
Terraform state (integration-boundaries §4).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path

import yaml

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_TTL = 300


class RecordError(ValueError):
    """Invalid input (domain, IP, conflicting inventories)."""


def _check_domain(domain: str) -> None:
    if not domain or len(domain) > 253 or domain != domain.lower():
        raise RecordError(f"invalid platform domain {domain!r}")
    labels = domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(lab) for lab in labels):
        raise RecordError(f"invalid platform domain {domain!r}")


def _check_ip(ip: str, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address(ip))
    except ValueError as err:
        raise RecordError(f"invalid IPv4 address for {what}: {ip!r}") from err


def _merge(out: dict[str, dict], name: str, hvars: dict) -> None:
    cur = out.setdefault(name, {})
    for key, val in (hvars or {}).items():
        if key == "ansible_host" and cur.get(key) not in (None, val):
            raise RecordError(f"host {name} has two addresses: {cur[key]} and {val}")
        cur[key] = val


def _hosts(node: dict) -> dict[str, dict]:
    """Collect {hostname: vars} from an inventory group tree."""
    out: dict[str, dict] = {}
    for name, hvars in (node.get("hosts") or {}).items():
        _merge(out, name, hvars)
    for child in (node.get("children") or {}).values():
        for name, hvars in _hosts(child or {}).items():
            _merge(out, name, hvars)
    return out


def guest_addresses(inventories: list[Path]) -> dict[str, str]:
    """{hostname: ip} across every inventory; a host with two different IPs is an error."""
    seen: dict[str, str] = {}
    for path in inventories:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for name, hvars in _hosts(doc.get("all") or {}).items():
            ip = hvars.get("ansible_host")
            if not ip:
                continue
            ip = _check_ip(str(ip), name)
            if name in seen and seen[name] != ip:
                raise RecordError(f"host {name} has two addresses: {seen[name]} and {ip}")
            if not _LABEL.match(name):
                raise RecordError(f"hostname {name!r} is not a DNS label")
            seen[name] = ip
    return seen


def desired_records(inventories: list[Path], domain: str, traefik_ip: str) -> dict[tuple[str, str], str]:
    _check_domain(domain)
    traefik_ip = _check_ip(traefik_ip, "the Traefik guest")
    want = {(f"*.{domain}", "A"): traefik_ip, (f"*.web.{domain}", "A"): traefik_ip}
    for name, ip in guest_addresses(inventories).items():
        want[(f"{name}.{domain}", "A")] = ip
    return want


def parse_zone(text: str) -> dict[tuple[str, str], str]:
    """Parse ``pdnsutil list-zone`` output into {(name, type): content} (A records with one value only)."""
    out: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or parts[2] != "IN":
            continue
        name, _ttl, _cls, rtype, content = parts
        out[(name.rstrip("."), rtype)] = content.strip()
    return out


def plan(want: dict[tuple[str, str], str], current: dict[tuple[str, str], str]) -> dict[str, list[dict]]:
    upsert = [{"name": n, "type": t, "ttl": _TTL, "content": c}
              for (n, t), c in sorted(want.items()) if current.get((n, t)) != c]
    delete = [{"name": n, "type": t} for (n, t) in sorted(current) if t == "A" and (n, t) not in want]
    return {"upsert": upsert, "delete": delete}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--domain", required=True)
    ap.add_argument("--traefik-ip", required=True)
    ap.add_argument("--current", required=True, help="file with the `pdnsutil list-zone` output ('' = empty zone)")
    ap.add_argument("inventories", nargs="*")
    args = ap.parse_args(argv)
    try:
        want = desired_records([Path(p) for p in args.inventories], args.domain, args.traefik_ip)
        current = parse_zone(Path(args.current).read_text(encoding="utf-8"))
    except (RecordError, OSError, yaml.YAMLError) as err:
        print(json.dumps({"error": str(err)}))
        return 1
    result = plan(want, current)
    result["desired_count"] = len(want)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
