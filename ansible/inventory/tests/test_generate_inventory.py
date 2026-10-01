"""Tests for the Terraform -> Ansible inventory generator (component C4).

Feature: terraform-ansible-handoff (design Correctness Properties 1-5 + example
anchors). All tests run fully OFFLINE — they call the pure core
:func:`render_inventory` with synthesized dicts, or drive :func:`main` via the
``--from-json`` seam. No live ``terraform`` and no Proxmox API is ever invoked.

Invoke by absolute venv path: ``~/venv/devinfra/bin/pytest ansible/inventory/tests/ -q``
"""

from __future__ import annotations

import json

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

import generate_inventory as gi
from generate_inventory import InventoryError, main, render_inventory

# --------------------------------------------------------------------------- #
# Hypothesis strategies: build valid inventory_hosts payloads.
# --------------------------------------------------------------------------- #

# lowercase dns-ish label, kept short/fast
_label = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)
_token = st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=12).filter(
    lambda s: not s.startswith("-") and not s.endswith("-")
)
_octet = st.integers(min_value=0, max_value=255)
_ipv4 = st.builds(lambda a, b, c, d: f"{a}.{b}.{c}.{d}", _octet, _octet, _octet, _octet)
_vmid = st.one_of(st.none(), st.integers(min_value=100, max_value=999999))
# node_name: optional/nullable Proxmox node identifier (non-secret host metadata).
_node_name = st.one_of(st.none(), st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=2, max_size=12))


# Distinctive secret values that cannot coincide with a rendered IP octet,
# hostname label, role, or slug — so a "leak" assertion can only fire on a real
# leak, never on incidental overlap (e.g. a secret "0" matching "0.0.0.0").
_secret_val = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=6, max_size=16
).map(lambda s: "SECRET-" + s)


@st.composite
def _host(draw, *, with_secrets: bool = False) -> dict:
    host = {
        "hostname": draw(_label),
        "ipv4_address": draw(_ipv4),
        "service_role": draw(_token),
        "project_slug": draw(_token),
        "vmid": draw(_vmid),
        "node_name": draw(_node_name),
    }
    if with_secrets:
        # adversarial extra secret-shaped fields (Property 2)
        host["token"] = draw(_secret_val)
        host["password"] = draw(_secret_val)
        host["private_key"] = draw(_secret_val)
    return host


@st.composite
def _host_list(draw, *, with_secrets: bool = False, min_size: int = 1) -> list[dict]:
    n = draw(st.integers(min_value=min_size, max_value=6))
    hosts = []
    for i in range(n):
        h = draw(_host(with_secrets=with_secrets))
        # guarantee globally-unique hostnames with an index suffix so no host
        # silently overwrites another (the generator keys hosts by hostname).
        h["hostname"] = f"{h['hostname']}-{i}"
        hosts.append(h)
    return hosts


def _wrap(hosts: list[dict]) -> dict:
    """Wrap a host list in the terraform output -json envelope."""
    return {"inventory_hosts": {"value": hosts, "type": "list", "sensitive": False}}


# --------------------------------------------------------------------------- #
# Property 1: Coverage / grouping round-trip
# Feature: terraform-ansible-handoff, Property 1: coverage/grouping round-trip
# Validates: Requirements 3.3, 6.1
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(_host_list())
def test_property1_coverage_grouping_roundtrip(hosts):
    rendered = render_inventory(_wrap(hosts))
    parsed = yaml.safe_load(rendered)
    children = parsed["all"]["children"]

    for host in hosts:
        hn = host["hostname"]
        role = host["service_role"]
        slug = host["project_slug"]

        # appears under its service_role group with correct ansible_host
        assert role in children, f"{role} group missing"
        assert hn in children[role]["hosts"], f"{hn} not under service_role {role}"
        assert children[role]["hosts"][hn]["ansible_host"] == host["ipv4_address"]

        # appears under its project_slug group
        assert slug in children, f"{slug} group missing"
        assert hn in children[slug]["hosts"], f"{hn} not under project_slug {slug}"

    # No duplicates / no dropped: each input host appears exactly once with full
    # hostvars (ansible_host set) across all groups. A group whose name matches
    # both a service_role and a project_slug can carry project-only ({}) entries
    # too, so count only entries that carry the full hostvars block.
    full_entries = []
    for group in children.values():
        for hn, hv in group["hosts"].items():
            if "ansible_host" in hv:
                full_entries.append(hn)
    assert sorted(full_entries) == sorted(h["hostname"] for h in hosts)


# --------------------------------------------------------------------------- #
# Property 2: No-secret invariant
# Feature: terraform-ansible-handoff, Property 2: no-secret invariant
# Validates: Requirements 3.5
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(_host_list(with_secrets=True))
def test_property2_no_secret(hosts):
    rendered = render_inventory(_wrap(hosts))
    for host in hosts:
        for secret_key in ("token", "password", "private_key"):
            assert secret_key not in rendered, f"secret KEY {secret_key} leaked"
            secret_val = host[secret_key]
            # non-empty secret values must not appear verbatim in the output
            if secret_val:
                assert secret_val not in rendered, f"secret VALUE for {secret_key} leaked"


# --------------------------------------------------------------------------- #
# Property 3: Determinism
# Feature: terraform-ansible-handoff, Property 3: determinism
# Validates: Requirements 3.4, 7.2
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(_host_list())
def test_property3_determinism(hosts):
    payload = _wrap(hosts)
    first = render_inventory(payload)
    second = render_inventory(payload)
    assert first == second


# --------------------------------------------------------------------------- #
# Property 4: Null-vmid tolerance
# Feature: terraform-ansible-handoff, Property 4: null-vmid tolerance
# Validates: Requirements 3.3, 7.2
# --------------------------------------------------------------------------- #
@settings(max_examples=150)
@given(_host_list())
def test_property4_null_vmid_tolerance(hosts):
    # Randomly force some vmids to null / absent to exercise tolerance.
    for i, h in enumerate(hosts):
        if i % 2 == 0:
            h["vmid"] = None
        elif i % 3 == 0:
            h.pop("vmid", None)
    # Also force some node_names to null/absent to exercise the same tolerance.
    for i, h in enumerate(hosts):
        if i % 2 == 1:
            h["node_name"] = None
        elif i % 5 == 0:
            h.pop("node_name", None)
    rendered = render_inventory(_wrap(hosts))
    parsed = yaml.safe_load(rendered)
    children = parsed["all"]["children"]

    for h in hosts:
        block = children[h["service_role"]]["hosts"][h["hostname"]]
        if h.get("vmid") is None:
            assert "vmid" not in block, "null/absent vmid should be omitted"
        else:
            assert block["vmid"] == h["vmid"], "non-null vmid must be carried through"
        if h.get("node_name") is None:
            assert "node_name" not in block, "null/absent node_name should be omitted"
        else:
            assert block["node_name"] == h["node_name"], (
                "non-null node_name must be carried through"
            )


# --------------------------------------------------------------------------- #
# Property 5: Fail-closed on empty/malformed input
# Feature: terraform-ansible-handoff, Property 5: fail-closed
# Validates: Requirements 3.6
# --------------------------------------------------------------------------- #
_malformed = st.one_of(
    st.none(),
    st.integers(),
    st.text(),
    st.lists(st.integers()),  # not a dict
    st.just({}),  # dict without inventory_hosts
    st.just({"inventory_hosts": {"value": []}}),  # empty list
    st.just({"inventory_hosts": {"value": "not-a-list"}}),  # value not a list
    st.just({"inventory_hosts": {"value": [{"hostname": "h"}]}}),  # missing required fields
)


@settings(max_examples=100)
@given(_malformed)
def test_property5_fail_closed_raises(payload):
    with pytest.raises(InventoryError):
        render_inventory(payload)


# --------------------------------------------------------------------------- #
# Example unit tests
# --------------------------------------------------------------------------- #
SVC07_HOSTS = [
    {
        "hostname": "svc07-20-01",
        "ipv4_address": "10.0.20.10",
        "service_role": "openbao",
        "project_slug": "shared",
        "vmid": None,
    },
    {
        "hostname": "svc07-unsealer-20-01",
        "ipv4_address": "10.0.20.11",
        "service_role": "openbao-unsealer",
        "project_slug": "shared",
        "vmid": None,
    },
]


def test_example_svc07_two_host_case():
    rendered = render_inventory(_wrap(SVC07_HOSTS), root="infra/projects/svc-07-secrets-manager")
    parsed = yaml.safe_load(rendered)
    children = parsed["all"]["children"]

    assert children["openbao"]["hosts"]["svc07-20-01"]["ansible_host"] == "10.0.20.10"
    assert children["openbao"]["hosts"]["svc07-20-01"]["ansible_user"] == "root"
    assert (
        children["openbao-unsealer"]["hosts"]["svc07-unsealer-20-01"]["ansible_host"]
        == "10.0.20.11"
    )
    # both under the shared project_slug group
    assert set(children["shared"]["hosts"].keys()) == {
        "svc07-20-01",
        "svc07-unsealer-20-01",
    }
    # null vmid omitted
    assert "vmid" not in children["openbao"]["hosts"]["svc07-20-01"]
    # header present
    assert rendered.startswith("# GENERATED")
    assert "svc-07-secrets-manager" in rendered


def test_example_single_host_root():
    hosts = [
        {
            "hostname": "svc01-100-01",
            "ipv4_address": "10.0.100.10",
            "service_role": "postgres",
            "project_slug": "myproject",
            "vmid": 12345,
        }
    ]
    rendered = render_inventory(_wrap(hosts))
    parsed = yaml.safe_load(rendered)
    children = parsed["all"]["children"]
    assert children["postgres"]["hosts"]["svc01-100-01"]["ansible_host"] == "10.0.100.10"
    # non-null vmid carried through
    assert children["postgres"]["hosts"]["svc01-100-01"]["vmid"] == 12345
    assert children["myproject"]["hosts"]["svc01-100-01"] == {}


def test_example_null_vmid_host():
    hosts = [
        {
            "hostname": "h1",
            "ipv4_address": "10.0.20.5",
            "service_role": "r1",
            "project_slug": "p1",
            "vmid": None,
        }
    ]
    rendered = render_inventory(_wrap(hosts))
    parsed = yaml.safe_load(rendered)
    block = parsed["all"]["children"]["r1"]["hosts"]["h1"]
    assert "vmid" not in block
    assert block["ansible_host"] == "10.0.20.5"


# --- fail-closed main()-level cases: assert non-zero exit AND no file written --- #


def test_main_fail_closed_malformed_json(tmp_path):
    out = tmp_path / "out.generated.yml"
    src = tmp_path / "bad.json"
    src.write_text("{ this is not valid json ")
    rc = main(["--from-json", str(src), "--out", str(out)])
    assert rc != 0
    assert not out.exists(), "no file must be written on malformed JSON"


def test_main_fail_closed_inventory_hosts_absent(tmp_path):
    out = tmp_path / "out.generated.yml"
    src = tmp_path / "payload.json"
    src.write_text(json.dumps({"some_other_output": {"value": 1}}))
    rc = main(["--from-json", str(src), "--out", str(out)])
    assert rc != 0
    assert not out.exists(), "no file must be written when inventory_hosts absent"


def test_main_fail_closed_empty_inventory_hosts(tmp_path):
    out = tmp_path / "out.generated.yml"
    src = tmp_path / "payload.json"
    src.write_text(json.dumps({"inventory_hosts": {"value": []}}))
    rc = main(["--from-json", str(src), "--out", str(out)])
    assert rc != 0
    assert not out.exists(), "no file must be written on empty inventory_hosts"


def test_main_success_writes_file(tmp_path):
    out = tmp_path / "svc07.generated.yml"
    src = tmp_path / "payload.json"
    src.write_text(json.dumps(_wrap(SVC07_HOSTS)))
    rc = main(["--from-json", str(src), "--out", str(out)])
    assert rc == 0
    assert out.exists()
    parsed = yaml.safe_load(out.read_text())
    assert "openbao" in parsed["all"]["children"]


def test_main_requires_root_or_from_json():
    rc = main([])
    assert rc != 0


def test_main_no_terraform_invoked_in_from_json_path(tmp_path, monkeypatch):
    """The --from-json path must never shell out to terraform."""
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must NOT be called on --from-json path")

    monkeypatch.setattr(gi.subprocess, "run", _boom)
    out = tmp_path / "out.generated.yml"
    src = tmp_path / "payload.json"
    src.write_text(json.dumps(_wrap(SVC07_HOSTS)))
    rc = main(["--from-json", str(src), "--out", str(out)])
    assert rc == 0
    assert out.exists()
