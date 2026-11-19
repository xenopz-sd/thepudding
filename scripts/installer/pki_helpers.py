"""SVC-08 internal PKI helpers (OpenBao PKI secrets engine).

Pure functions that build the OpenBao PKI API payloads/policies the ``pki_ca`` and
``pki_issue`` roles send, and the idempotency predicate ``needs_issue`` the shared
issuance role uses so a re-run with a valid certificate issues nothing (R3).

No secret value is handled here: payloads carry names and TTLs only, and the CLI
reads certificate PEMs (public) from disk. The recorded real API shapes live in
``tests/fixtures/live-shapes/openbao-pki.json`` and are bound to this module by
``tests/installer/test_svc08_pki.py``.

CLI (used by the ``pki_issue`` role; exit 0 = nothing to issue, 1 = issue needed)::

    pki_helpers.py plan --base-url U --dns-resolver H:53 --domain D --consumers-json '[...]'
    pki_helpers.py crl-lists --crl C --serial aa:bb [--issuer I]
    pki_helpers.py needs-issue --cert C --root R --san DNS:a --san IP:10.0.20.10 --min-days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtensionOID

MOUNT_ROOT = "pki"
MOUNT_INT = "pki_int"
ROOT_MAX_TTL = "87600h"  # 10 years (R1)
INT_MAX_TTL = "26280h"  # 3 years (R1)
LEAF_DEFAULT_TTL = "2160h"  # 90 days (R3)
MIN_DAYS_DEFAULT = 30  # renew threshold (R3)
ACME_ROLE = "traefik"

# Response headers an ACME client needs through the mount (OpenBao PKI API: ACME).
ACME_PASSTHROUGH_REQUEST_HEADERS = ["If-Modified-Since"]
ACME_ALLOWED_RESPONSE_HEADERS = ["Replay-Nonce", "Link", "Location"]


def issue_path(role: str, mount: str = MOUNT_INT) -> str:
    return f"{mount}/issue/{role}"


def acme_directory_path(role: str = ACME_ROLE, mount: str = MOUNT_INT) -> str:
    return f"{mount}/roles/{role}/acme/directory"


def policy_name(service: str) -> str:
    return f"pki-issue-{service}"


TOKEN_ROLE = "pki-issue"


def token_role_body() -> dict:
    """Token role the issuer mints children from: only ``pki-issue-*`` policies, orphan, short-lived."""
    return {
        "allowed_policies_glob": ["pki-issue-*"],
        "orphan": True,
        "renewable": False,
        "token_no_default_policy": True,
        "token_max_ttl": "10m",
    }


def policy_hcl(service: str) -> str:
    """Least-privilege policy: issue on exactly one role of the intermediate (D3)."""
    return (
        f'path "{issue_path(service)}" {{\n'
        '  capabilities = ["update"]\n'
        "}\n"
    )


def mount_tune_body() -> dict:
    return {
        "default_lease_ttl": LEAF_DEFAULT_TTL,
        "max_lease_ttl": INT_MAX_TTL,
        "passthrough_request_headers": list(ACME_PASSTHROUGH_REQUEST_HEADERS),
        "allowed_response_headers": list(ACME_ALLOWED_RESPONSE_HEADERS),
    }


def cluster_config_body(base_url: str) -> dict:
    """``config/cluster`` for ACME: the externally reachable mount URL (D9)."""
    base = base_url.rstrip("/")
    return {"path": f"{base}/v1/{MOUNT_INT}", "aia_path": f"{base}/v1/{MOUNT_INT}"}


def urls_config_body(base_url: str) -> dict:
    base = base_url.rstrip("/")
    return {
        "issuing_certificates": [f"{base}/v1/{MOUNT_INT}/ca"],
        "crl_distribution_points": [f"{base}/v1/{MOUNT_INT}/crl"],
    }


def acme_config_body(dns_resolver: str) -> dict:
    return {
        "enabled": True,
        "allowed_roles": [ACME_ROLE],
        "default_directory_policy": f"role:{ACME_ROLE}",
        "eab_policy": "not-required",
        "dns_resolver": dns_resolver,
    }


def role_body(
    domain: str,
    *,
    allow_subdomains: bool = False,
    allow_wildcard: bool = False,
    allow_ip_sans: bool = True,
    acme: bool = False,
) -> dict:
    """PKI role for one consuming service (D2). All roles store certs (revocation, ACME)."""
    return {
        "allowed_domains": [domain],
        "allow_bare_domains": True,
        "allow_subdomains": allow_subdomains,
        "allow_wildcard_certificates": allow_wildcard,
        "allow_ip_sans": allow_ip_sans,
        "key_type": "ec",
        "key_bits": 256,
        "max_ttl": LEAF_DEFAULT_TTL,
        "ttl": LEAF_DEFAULT_TTL,
        "server_flag": True,
        "client_flag": False,
        # Stored so every certificate can be revoked by serial (R5; live finding: no_store=true makes
        # /revoke answer "certificate not found") and so ACME can serve it back.
        "no_store": False,
    }


def plan(base_url: str, dns_resolver: str, domain: str, consumers: list[dict]) -> dict:
    """Everything ``pki_ca`` sends, in one document (single source of truth)."""
    roles = []
    for c in consumers:
        roles.append(
            {
                "name": c["name"],
                "body": role_body(domain, allow_subdomains=bool(c.get("subdomains"))),
                "policy": policy_hcl(c["name"]),
            }
        )
    roles.append(
        {
            "name": ACME_ROLE,
            "body": role_body(domain, allow_subdomains=True, allow_wildcard=True, allow_ip_sans=False, acme=True),
        }
    )
    return {
        "tune": mount_tune_body(),
        "urls": urls_config_body(base_url),
        "cluster": cluster_config_body(base_url),
        "acme": acme_config_body(dns_resolver),
        "roles": roles,
        "token_role": token_role_body(),
    }


def parse_san(value: str) -> tuple[str, str]:
    kind, _, name = value.partition(":")
    kind = kind.upper()
    if kind not in ("DNS", "IP") or not name:
        raise ValueError(f"SAN must be DNS:<name> or IP:<addr>, got {value!r}")
    if kind == "IP":
        name = str(ipaddress.ip_address(name))
    else:
        name = name.lower()
    return kind, name


def _cert_sans(cert: x509.Certificate) -> set[tuple[str, str]]:
    try:
        ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return set()
    out: set[tuple[str, str]] = set()
    for n in ext.value.get_values_for_type(x509.DNSName):
        out.add(("DNS", n.lower()))
    for n in ext.value.get_values_for_type(x509.IPAddress):
        out.add(("IP", str(n)))
    return out


def _load_certs(pem: bytes) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates(pem)


def _signed_by(child: x509.Certificate, parent: x509.Certificate) -> bool:
    try:
        child.verify_directly_issued_by(parent)
    except Exception:  # noqa: BLE001 - any verification failure means "not issued by"
        return False
    return True


def needs_issue(
    cert_pem: bytes | None,
    root_pem: bytes,
    sans: list[str],
    now: dt.datetime,
    min_days: int = MIN_DAYS_DEFAULT,
) -> tuple[bool, str]:
    """Return ``(issue_needed, reason)``.

    A certificate is reusable only if it chains to the platform root, covers every
    requested SAN and has more than ``min_days`` left. The chain may be the leaf
    followed by intermediates (what ``cert.pem`` holds); the last link must be
    issued by the root.
    """
    if not cert_pem:
        return True, "no certificate at the path"
    try:
        chain = _load_certs(cert_pem)
    except ValueError:
        return True, "certificate file is not valid PEM"
    if not chain:
        return True, "certificate file is empty"
    root = _load_certs(root_pem)[0]
    leaf = chain[0]
    links = chain + [root]
    for child, parent in zip(links, links[1:]):
        if not _signed_by(child, parent):
            return True, "does not chain to the platform root"
    wanted = {parse_san(s) for s in sans}
    missing = wanted - _cert_sans(leaf)
    if missing:
        return True, "missing SANs: " + ", ".join(f"{k}:{v}" for k, v in sorted(missing))
    left = leaf.not_valid_after_utc - now
    if left <= dt.timedelta(days=min_days):
        return True, f"expires in {left.days} days (renew at <= {min_days})"
    return False, f"valid, {left.days} days left"


def crl_lists(crl_pem: bytes, serial: str, issuer_pem: bytes | None = None) -> bool:
    """True when the CRL lists ``serial`` (OpenBao's colon-hex form) and, if given, is signed by ``issuer_pem``."""
    crl = x509.load_pem_x509_crl(crl_pem)
    if issuer_pem is not None and not crl.is_signature_valid(_load_certs(issuer_pem)[0].public_key()):
        return False
    return crl.get_revoked_certificate_by_serial_number(int(serial.replace(":", ""), 16)) is not None


def not_after(cert_pem: bytes) -> dt.datetime:
    return _load_certs(cert_pem)[0].not_valid_after_utc


def textfile_metric(path: str, cn: str, service: str, cert_pem: bytes) -> str:
    """node_exporter textfile body for ``PlatformCertExpiringSoon`` (D7)."""
    ts = int(not_after(cert_pem).timestamp())
    labels = f'path="{path}",cn="{cn}",service="{service}"'
    return (
        "# HELP platform_tls_cert_not_after_seconds Certificate expiry (unix seconds).\n"
        "# TYPE platform_tls_cert_not_after_seconds gauge\n"
        f"platform_tls_cert_not_after_seconds{{{labels}}} {ts}\n"
    )


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("needs-issue")
    n.add_argument("--cert", required=True)
    n.add_argument("--root", required=True)
    n.add_argument("--san", action="append", default=[])
    n.add_argument("--min-days", type=int, default=MIN_DAYS_DEFAULT)
    pl = sub.add_parser("plan")
    pl.add_argument("--base-url", required=True)
    pl.add_argument("--dns-resolver", required=True)
    pl.add_argument("--domain", required=True)
    pl.add_argument("--consumers-json", required=True)
    c = sub.add_parser("crl-lists")
    c.add_argument("--crl", required=True)
    c.add_argument("--serial", required=True)
    c.add_argument("--issuer")
    m = sub.add_parser("metric")
    m.add_argument("--cert", required=True)
    m.add_argument("--cn", required=True)
    m.add_argument("--service", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "needs-issue":
        cert = Path(a.cert)
        pem = cert.read_bytes() if cert.is_file() else None
        need, reason = needs_issue(
            pem, Path(a.root).read_bytes(), a.san, dt.datetime.now(dt.timezone.utc), a.min_days
        )
        print(json.dumps({"issue": need, "reason": reason}))
        return 1 if need else 0
    if a.cmd == "crl-lists":
        issuer = Path(a.issuer).read_bytes() if a.issuer else None
        return 0 if crl_lists(Path(a.crl).read_bytes(), a.serial, issuer) else 1
    if a.cmd == "plan":
        print(json.dumps(plan(a.base_url, a.dns_resolver, a.domain, json.loads(a.consumers_json))))
        return 0
    print(textfile_metric(a.cert, a.cn, a.service, Path(a.cert).read_bytes()), end="")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
