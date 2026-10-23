"""Drift-guards for the SVC-06 (ZITADEL) permanent default-off mock stand-ins.

Slice-3 Task 15 (spec: svc-06-identity-provider) — design.md §4 + mock-stand-ins.md.
Per the platform's mock philosophy ("proxies must stay"; development affordances
that never weaken the production posture), the zitadel-server role ships two
PERMANENT, OPT-IN, DEFAULT-OFF mocks:
  * Mock 5 — zitadel_tls_self_signed: self-signed cert (control-node generation)
    standing in for the real Traefik/CA TLS when SVC-09 is absent.
  * Mock 6 — zitadel_mock_edge_network: placeholder `edge` Docker network when
    Traefik is not deployed on the target.

This guard pins (pure PyYAML / text scan, always runs):
  1. both flags default FALSE in defaults/main.yml;
  2. the TLS mock include is GATED on the flag; the cert is generated on the
     CONTROL NODE (delegate_to: localhost) and key tasks are no_log; generation is
     idempotent (stat-skip via a *_generate fact);
  3. the edge-network mock is gated on the flag and idempotent (docker_network
     state: present);
  4. both catalogue rows (Mock 5 + Mock 6) are present in mock-stand-ins.md;
  5. the SVC-07 Mock 1 + Mock 2 catalogue rows are preserved (not clobbered).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_mocks.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "zitadel-server"
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_MAIN = _ROLE / "tasks" / "main.yml"
_TLS = _ROLE / "tasks" / "tls_self_signed.yml"
_CATALOGUE = _REPO_ROOT / ".kiro" / "steering" / "mock-stand-ins.md"


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- #
# 1. Both flags default FALSE.
# --------------------------------------------------------------------------- #
def test_both_mock_flags_default_false():
    d = _defaults()
    assert d.get("zitadel_tls_self_signed") is False, "zitadel_tls_self_signed must default FALSE (opt-in)"
    assert d.get("zitadel_mock_edge_network") is False, "zitadel_mock_edge_network must default FALSE (opt-in)"


# --------------------------------------------------------------------------- #
# 2. TLS mock: gated include, control-node generation, no_log key, idempotent.
# --------------------------------------------------------------------------- #
def test_tls_mock_include_is_gated():
    tasks = _load_tasks(_MAIN)
    inc = [t for t in tasks if "ansible.builtin.include_tasks" in t
           and "tls_self_signed" in str(t.get("ansible.builtin.include_tasks", ""))]
    assert inc, "main.yml must include tls_self_signed.yml"
    for t in inc:
        assert "zitadel_tls_self_signed" in str(t.get("when", "")), (
            "the TLS mock include must be gated on zitadel_tls_self_signed"
        )


def test_tls_mock_generates_on_control_node_and_no_log_key():
    assert _TLS.is_file(), "the TLS self-signed mock task file must exist"
    tasks = _load_tasks(_TLS)
    # The privatekey/csr/cert generation must delegate to localhost (control node).
    gen = [t for t in tasks if any(m in str(t) for m in
           ("community.crypto.openssl_privatekey", "community.crypto.x509_certificate"))]
    assert gen, "the mock must generate the key/cert via community.crypto"
    for t in gen:
        assert t.get("delegate_to") == "localhost", (
            "cert/key generation must delegate_to localhost (the guest lacks python-cryptography)"
        )
    # The private-key task must be no_log.
    key_tasks = [t for t in tasks if "community.crypto.openssl_privatekey" in str(t)]
    assert key_tasks and all(t.get("no_log") is True for t in key_tasks), (
        "the private-key generation task must be no_log: true"
    )


def test_tls_mock_is_idempotent_stat_skip():
    text = _TLS.read_text(encoding="utf-8")
    assert "zitadel_mock_tls_generate" in text, (
        "the TLS mock must gate (re)generation on a *_generate fact derived from a stat of the target cert "
        "(idempotent — a present cert is kept, no restart churn)"
    )
    assert "stat" in text.lower(), "the TLS mock must stat the target cert to decide whether to regenerate"


# --------------------------------------------------------------------------- #
# 3. Edge-network mock: gated + idempotent.
# --------------------------------------------------------------------------- #
def test_edge_network_mock_gated_and_idempotent():
    tasks = _load_tasks(_MAIN)
    net = [t for t in tasks if "community.docker.docker_network" in t]
    assert net, "main.yml must create the placeholder edge network via community.docker.docker_network"
    for t in net:
        assert "zitadel_mock_edge_network" in str(t.get("when", "")), (
            "the edge-network mock must be gated on zitadel_mock_edge_network"
        )
        spec = t["community.docker.docker_network"]
        assert spec.get("state") == "present", "docker_network must be state: present (create-if-absent, idempotent)"


# --------------------------------------------------------------------------- #
# 4. Catalogue rows present (Mock 5 + Mock 6).
# --------------------------------------------------------------------------- #
def test_catalogue_rows_present():
    text = _CATALOGUE.read_text(encoding="utf-8")
    assert "### Mock 5" in text and "zitadel_tls_self_signed" in text, (
        "mock-stand-ins.md must carry the Mock 5 (zitadel_tls_self_signed) catalogue row"
    )
    assert "### Mock 6" in text and "zitadel_mock_edge_network" in text, (
        "mock-stand-ins.md must carry the Mock 6 (zitadel_mock_edge_network) catalogue row"
    )


# --------------------------------------------------------------------------- #
# 5. SVC-07 Mock 1 + Mock 2 preserved.
# --------------------------------------------------------------------------- #
def test_svc07_mocks_preserved():
    text = _CATALOGUE.read_text(encoding="utf-8")
    assert "### Mock 1" in text and "openbao_tls_self_signed" in text, "SVC-07 Mock 1 row must be preserved"
    assert "### Mock 2" in text and "openbao_mock_edge_network" in text, "SVC-07 Mock 2 row must be preserved"
