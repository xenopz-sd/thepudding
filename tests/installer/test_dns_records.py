"""Offline tests for scripts/installer/dns_records.py (spec svc-10-internal-dns, R1/R2, design D2/D3)."""

from __future__ import annotations

import json
import textwrap

import pytest

import dns_records as dr

_INV_A = textwrap.dedent("""
    all:
      children:
        garage:
          hosts:
            svc03-20-01: {ansible_host: 10.0.20.22, ansible_user: root}
            svc03-20-02: {ansible_host: 10.0.20.23}
        shared:
          hosts:
            svc03-20-01: {}
            svc03-20-02: {}
""")
_INV_B = textwrap.dedent("""
    all:
      children:
        openbao:
          hosts:
            svc07-20-01: {ansible_host: 10.0.20.10}
        openbao-unsealer:
          hosts:
            svc07-unsealer-20-01: {ansible_host: 10.0.20.11}
""")


@pytest.fixture
def inventories(tmp_path):
    a = tmp_path / "a.generated.yml"
    a.write_text(_INV_A)
    b = tmp_path / "b.generated.yml"
    b.write_text(_INV_B)
    return [a, b]


def test_desired_records_are_wildcards_plus_one_a_per_guest(inventories):
    want = dr.desired_records(inventories, "platform.example.test", "10.0.20.12")
    assert want == {
        ("*.platform.example.test", "A"): "10.0.20.12",
        ("*.web.platform.example.test", "A"): "10.0.20.12",
        ("svc03-20-01.platform.example.test", "A"): "10.0.20.22",
        ("svc03-20-02.platform.example.test", "A"): "10.0.20.23",
        ("svc07-20-01.platform.example.test", "A"): "10.0.20.10",
        ("svc07-unsealer-20-01.platform.example.test", "A"): "10.0.20.11",
    }


def test_a_host_listed_in_two_groups_yields_one_record(inventories):
    want = dr.desired_records(inventories, "platform.example.test", "10.0.20.12")
    assert sum(1 for (n, _t) in want if n.startswith("svc03-20-01.")) == 1


def test_conflicting_ips_for_one_host_fail_loudly(tmp_path):
    p = tmp_path / "x.generated.yml"
    p.write_text("all:\n  children:\n    g1: {hosts: {h1: {ansible_host: 10.0.20.1}}}\n"
                 "    g2: {hosts: {h1: {ansible_host: 10.0.20.2}}}\n")
    with pytest.raises(dr.RecordError, match="h1"):
        dr.desired_records([p], "platform.example.test", "10.0.20.12")


@pytest.mark.parametrize("domain", ["", "Example.test", "-bad.test", "a..b", "x" * 254])
def test_invalid_domains_are_rejected(inventories, domain):
    with pytest.raises(dr.RecordError):
        dr.desired_records(inventories, domain, "10.0.20.12")


def test_invalid_traefik_ip_is_rejected(inventories):
    with pytest.raises(dr.RecordError):
        dr.desired_records(inventories, "platform.example.test", "10.0.20")


_ZONE = textwrap.dedent("""\
    $ORIGIN .
    platform.example.test\t3600\tIN\tNS\tsvc10-20-01.platform.example.test.
    platform.example.test\t3600\tIN\tSOA\ta.misconfigured.dns.server.invalid hostmaster 1 10800 3600 604800 3600
    *.platform.example.test\t300\tIN\tA\t10.0.20.12
    svc03-20-01.platform.example.test\t300\tIN\tA\t10.0.20.99
    gone-20-01.platform.example.test\t300\tIN\tA\t10.0.20.50
    _acme-challenge.auth.platform.example.test\t60\tIN\tTXT\t"token"
""")


def test_plan_upserts_changed_and_new_records_and_deletes_stale_a_records(inventories):
    want = dr.desired_records(inventories, "platform.example.test", "10.0.20.12")
    plan = dr.plan(want, dr.parse_zone(_ZONE))
    upserts = {(u["name"], u["type"]): u["content"] for u in plan["upsert"]}
    assert ("*.platform.example.test", "A") not in upserts, "an unchanged record is not rewritten"
    assert upserts[("svc03-20-01.platform.example.test", "A")] == "10.0.20.22", "drifted IP is fixed"
    assert ("svc07-20-01.platform.example.test", "A") in upserts
    assert plan["delete"] == [{"name": "gone-20-01.platform.example.test", "type": "A"}]


def test_plan_never_touches_soa_ns_or_acme_txt_records(inventories):
    want = dr.desired_records(inventories, "platform.example.test", "10.0.20.12")
    plan = dr.plan(want, dr.parse_zone(_ZONE))
    touched = {(r["name"], r["type"]) for r in plan["upsert"] + plan["delete"]}
    assert not any(t in ("SOA", "NS", "TXT") for _n, t in touched), "only A records are managed (ACME TXT stays)"


def test_converged_zone_gives_an_empty_plan(inventories):
    want = dr.desired_records(inventories, "platform.example.test", "10.0.20.12")
    zone = "\n".join(f"{n}\t300\tIN\t{t}\t{c}" for (n, t), c in want.items())
    assert dr.plan(want, dr.parse_zone(zone)) == {"upsert": [], "delete": []}


def test_cli_prints_a_json_plan(inventories, tmp_path, capsys):
    zone = tmp_path / "zone.txt"
    zone.write_text(_ZONE)
    rc = dr.main(["--domain", "platform.example.test", "--traefik-ip", "10.0.20.12", "--current", str(zone),
                  *[str(p) for p in inventories]])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and set(out) == {"upsert", "delete", "desired_count"}
    assert out["desired_count"] == 6
