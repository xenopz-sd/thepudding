"""SVC-08 offline tests: pki_helpers (idempotency predicate, payloads) bound to the
recorded OpenBao PKI shapes. Spec: .kiro/specs/svc-08-internal-pki/."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "installer"))
import pki_helpers as ph  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "live-shapes" / "openbao-pki.json"
NOW = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _make(cn, issuer_cert=None, issuer_key=None, sans=(), days=90, ca=False):
    key = ec.generate_private_key(ec.SECP256R1())
    b = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(issuer_cert.subject if issuer_cert else _name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    names = []
    for s in sans:
        k, v = ph.parse_san(s)
        names.append(x509.DNSName(v) if k == "DNS" else x509.IPAddress(ipaddress.ip_address(v)))
    if names:
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    cert = b.sign(issuer_key or key, hashes.SHA256())
    return cert, key


def _pem(*certs) -> bytes:
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


@pytest.fixture(scope="module")
def pki():
    root, rk = _make("root", ca=True, days=3650)
    inter, ik = _make("inter", root, rk, ca=True, days=1000)
    return root, inter, ik


def test_fixture_loaded_and_code_symbols_exist():
    fx = json.loads(FIXTURE.read_text())
    assert fx["mounts"] == {"root": ph.MOUNT_ROOT, "intermediate": ph.MOUNT_INT}
    assert callable(ph.needs_issue) and callable(ph.role_body)


def test_paths_bound_to_fixture():
    fx = json.loads(FIXTURE.read_text())["paths"]
    assert ph.issue_path("svc07") == fx["issue"].format(role="svc07")
    assert ph.acme_directory_path("traefik") == fx["acme_directory"].format(role="traefik")


def test_payload_shapes_bound_to_fixture():
    fx = json.loads(FIXTURE.read_text())
    tune = ph.mount_tune_body()
    assert tune["passthrough_request_headers"] == fx["acme"]["mount_tune_passthrough_request_headers"]
    assert tune["allowed_response_headers"] == fx["acme"]["mount_tune_allowed_response_headers"]
    acme = ph.acme_config_body("10.0.20.25:53")
    assert set(acme) <= set(fx["responses"]["config_acme"])
    assert acme["default_directory_policy"] == fx["acme"]["default_directory_policy_role_form"].format(role="traefik")
    assert acme["allowed_roles"] == ["traefik"]
    # ACME needs stored certificates (no_store=false); push roles store too, or /revoke cannot find the
    # serial (live finding, 2026-11-16).
    assert ph.role_body("d.example.test", acme=True)["no_store"] is False
    assert ph.role_body("d.example.test")["no_store"] is False


def test_policy_scopes_one_role_only():
    hcl = ph.policy_hcl("svc07")
    assert 'path "pki_int/issue/svc07"' in hcl
    assert hcl.count("path ") == 1 and "sudo" not in hcl and '"*"' not in hcl


def test_cluster_and_urls_use_the_given_base():
    assert ph.cluster_config_body("https://10.0.20.10:8200/")["path"] == "https://10.0.20.10:8200/v1/pki_int"
    assert ph.urls_config_body("https://10.0.20.10:8200")["crl_distribution_points"] == [
        "https://10.0.20.10:8200/v1/pki_int/crl"
    ]


def test_role_never_allows_client_certs_or_unbounded_ttl():
    r = ph.role_body("d.example.test", allow_subdomains=True, allow_wildcard=True)
    assert r["client_flag"] is False and r["max_ttl"] == "2160h"


# --- needs_issue idempotency predicate (R3, AC-5) ---------------------------


def test_no_cert_needs_issue(pki):
    root, *_ = pki
    assert ph.needs_issue(None, _pem(root), ["DNS:a.example.test"], NOW)[0] is True


def test_valid_chained_cert_is_not_reissued(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test", "IP:10.0.20.10"])
    need, why = ph.needs_issue(
        _pem(leaf, inter), _pem(root), ["DNS:A.example.test", "IP:10.0.20.10"], NOW
    )
    assert need is False, why


def test_renews_at_threshold(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test"], days=29)
    assert ph.needs_issue(_pem(leaf, inter), _pem(root), ["DNS:a.example.test"], NOW)[0] is True
    ok, _ = _make("a", inter, ik, sans=["DNS:a.example.test"], days=31)
    assert ph.needs_issue(_pem(ok, inter), _pem(root), ["DNS:a.example.test"], NOW)[0] is False


def test_missing_san_forces_issue(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test"])
    need, why = ph.needs_issue(_pem(leaf, inter), _pem(root), ["DNS:b.example.test"], NOW)
    assert need and "missing SANs" in why


def test_foreign_chain_forces_issue(pki):
    root, inter, ik = pki
    other_root, ork = _make("other", ca=True)
    other_inter, oik = _make("oi", other_root, ork, ca=True)
    leaf, _ = _make("a", other_inter, oik, sans=["DNS:a.example.test"])
    need, why = ph.needs_issue(_pem(leaf, other_inter), _pem(root), ["DNS:a.example.test"], NOW)
    assert need and "chain" in why


def test_missing_intermediate_in_file_forces_issue(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test"])
    assert ph.needs_issue(_pem(leaf), _pem(root), ["DNS:a.example.test"], NOW)[0] is True


def test_garbage_pem_forces_issue(pki):
    root, *_ = pki
    assert ph.needs_issue(b"not a cert", _pem(root), [], NOW)[0] is True


def test_bad_san_rejected():
    with pytest.raises(ValueError):
        ph.parse_san("EMAIL:x")


def test_metric_has_expiry_labels(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test"])
    m = ph.textfile_metric("/etc/x/cert.pem", "a.example.test", "svc07", _pem(leaf))
    assert 'platform_tls_cert_not_after_seconds{path="/etc/x/cert.pem",cn="a.example.test",service="svc07"}' in m
    assert "PRIVATE KEY" not in m


def test_plan_has_acme_role_storing_certs_and_least_privilege_policies():
    p = ph.plan(
        "https://10.0.20.10:8200", "10.0.20.25:53", "d.example.test",
        [{"name": "svc07", "subdomains": False}, {"name": "svc09", "subdomains": True}],
    )
    names = [r["name"] for r in p["roles"]]
    assert names == ["svc07", "svc09", "traefik"]
    by = {r["name"]: r for r in p["roles"]}
    assert by["traefik"]["body"]["no_store"] is False and "policy" not in by["traefik"]
    assert by["svc07"]["body"]["allow_subdomains"] is False
    assert by["svc09"]["policy"].count("path ") == 1
    assert p["acme"]["dns_resolver"] == "10.0.20.25:53"


def test_plan_cli_roundtrip():
    import subprocess

    out = subprocess.run(
        [sys.executable, str(REPO / "scripts/installer/pki_helpers.py"), "plan", "--base-url", "https://x:8200",
         "--dns-resolver", "1.2.3.4:53", "--domain", "d.example.test", "--consumers-json", '[{"name":"svc07"}]'],
        capture_output=True, text=True, check=True,
    ).stdout
    assert json.loads(out)["cluster"]["path"] == "https://x:8200/v1/pki_int"


def test_token_role_limits_children_to_pki_issue_policies():
    r = ph.token_role_body()
    assert r["allowed_policies_glob"] == ["pki-issue-*"] and r["orphan"] is True
    assert ph.plan("https://x:8200", "1.2.3.4:53", "d.example.test", [])["token_role"] == r
    assert r["token_max_ttl"] == "10m"


def test_crl_lists_revoked_serial_and_checks_signature(pki):
    root, inter, ik = pki
    leaf, _ = _make("a", inter, ik, sans=["DNS:a.example.test"])
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(inter.subject)
        .last_update(NOW)
        .next_update(NOW + dt.timedelta(days=3))
        .add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(leaf.serial_number).revocation_date(NOW).build()
        )
        .sign(ik, hashes.SHA256())
    )
    pem = crl.public_bytes(serialization.Encoding.PEM)
    colon = ":".join(f"{b:02x}" for b in leaf.serial_number.to_bytes((leaf.serial_number.bit_length() + 7) // 8, "big"))
    assert ph.crl_lists(pem, colon, _pem(inter)) is True
    assert ph.crl_lists(pem, "01:02", _pem(inter)) is False
    assert ph.crl_lists(pem, colon, _pem(root)) is False  # wrong signer
