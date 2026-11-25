"""Offline tests for fix-consumer-ingress-browser-reachable-oidc (§6).

SECURITY-SENSITIVE (HTTP ingress + OIDC auth path).

Bug: a consumer project's user-facing app is healthy infra-side but a human
cannot complete interactive OIDC login — the ZITADEL issuer host is not
browser-reachable (and CONSUMER-JOURNEY §D wrongly claims the browser path
"needs nothing extra"), and the app has no Traefik router / base-domain hostname
(reached only by raw VLAN IP, so the registered redirect URI can never be hit).

Fix: an opt-in `expose` manifest block (sibling to `workload`) that drives the
EXISTING ADR-0018 traefik_route wiring under <host>.${THEPUDDING_BASE_DOMAIN}; a
scoped fail-closed cross-VLAN reachability preflight (naming §3); a dev
browser-reachability recipe in §D (correcting the wrong claim); and a handoff
banner. No secret value is embedded in these tests.

Run: ~/venv/devinfra/bin/pytest tests/installer/test_consumer_ingress_browser_oidc.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER = _REPO_ROOT / "scripts" / "installer"
sys.path.insert(0, str(_INSTALLER))

import consumer_manifest as cm  # noqa: E402

_CONSUMER_PROVISION = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "tasks" / "main.yml"
)
_CONSUMER_HANDOFF = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_handoff" / "tasks" / "main.yml"
)
_CONSUMER_JOURNEY = _REPO_ROOT / "docs" / "CONSUMER-JOURNEY.md"


def _m(**expose):
    return {"slug": "moto", "expose": expose}


# =========================================================================== #
# Component 1 — `expose` manifest schema.
# =========================================================================== #
def test_expose_in_consumable_service_keys():
    assert "expose" in cm.CONSUMABLE_SERVICE_KEYS


def test_expose_absent_is_backward_compatible():
    assert cm.validate_manifest({"slug": "moto"}) is not None


def test_expose_disabled_block_without_host_ok():
    assert cm.validate_manifest(_m(enabled=False))


def test_expose_enabled_requires_host():
    with pytest.raises(cm.ManifestValidationError, match="expose.host"):
        cm.validate_manifest(_m(enabled=True))


def test_expose_valid_full_block():
    m = _m(enabled=True, host="moto-maintenance", port=8080, tls="terminate")
    assert cm.validate_manifest(m) is m


def test_expose_host_fqdn_rejected():
    with pytest.raises(cm.ManifestValidationError, match="bare DNS subdomain"):
        cm.validate_manifest(_m(enabled=True, host="moto-maintenance.thepudding.internal"))


def test_expose_host_bad_label_rejected():
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest(_m(enabled=True, host="Moto_Maintenance"))  # upper + underscore


def test_expose_port_out_of_range_rejected():
    with pytest.raises(cm.ManifestValidationError, match="expose.port"):
        cm.validate_manifest(_m(enabled=True, host="moto", port=70000))


def test_expose_tls_enum_rejected():
    with pytest.raises(cm.ManifestValidationError, match="expose.tls"):
        cm.validate_manifest(_m(enabled=True, host="moto", tls="mtls"))


def test_expose_enabled_must_be_bool():
    with pytest.raises(cm.ManifestValidationError, match="expose.enabled"):
        cm.validate_manifest(_m(enabled="yes", host="moto"))


# =========================================================================== #
# Component 2/3 — deploy wiring + cross-VLAN preflight (structural).
# =========================================================================== #
_HELLO_WORKLOAD_DEPLOY = (
    _REPO_ROOT / "ansible" / "roles" / "hello_workload_deploy" / "tasks" / "main.yml"
)


def test_deploy_step_drives_expose_route():
    """Gated on expose.enabled, the deploy step (the run that renders the route)
    feeds the openbao-agent traefik_route path a real base-domain host rule and
    enables the real route.

    Retargeted by fix-consumer-expose-route-and-gateway-docs: these facts used to
    be set in consumer_provision, a separate process whose facts never reached the
    deploy run (caught live 2026-11-24). Detailed coverage:
    tests/installer/test_consumer_expose_route_wiring.py.
    """
    text = _HELLO_WORKLOAD_DEPLOY.read_text(encoding="utf-8")
    assert "expose" in text, "hello_workload_deploy must consume the expose block"
    assert "openbao_agent_workload_host_rule" in text, (
        "the expose path must feed a real base-domain openbao_agent_workload_host_rule"
    )
    assert "openbao_agent_traefik_route_enabled" in text, (
        "the expose path must select the real ADR-0018 cross-guest route"
    )


def test_consumer_provision_has_cross_vlan_preflight_naming_sdn_gateway_fix():
    """A scoped fail-closed preflight (Traefik_Guest -> backend reachability) must
    name fix-sdn-gateway-project-vlan-noop when the backend is unreachable."""
    text = _CONSUMER_PROVISION.read_text(encoding="utf-8")
    assert "sdn-gateway-project-vlan-noop" in text or "sdn_gateway" in text, (
        "the cross-VLAN reachability preflight must name the gateway dependency "
        "(fix-sdn-gateway-project-vlan-noop) on failure (FAILS on unfixed code)"
    )


# =========================================================================== #
# Component 4 — dev browser-reachability recipe corrects §D's wrong claim.
# =========================================================================== #
def test_consumer_journey_corrects_browser_needs_nothing_extra_claim():
    text = _CONSUMER_JOURNEY.read_text(encoding="utf-8")
    assert "nothing extra is needed for it" not in text, (
        "CONSUMER-JOURNEY §D must no longer claim the browser->issuer path 'needs "
        "nothing extra' (that is the §6 bug)"
    )
    # The browser recipe must map BOTH the issuer host AND the app host.
    assert "/etc/hosts" in text, "the dev browser-reachability recipe must document /etc/hosts"


# =========================================================================== #
# Component 5 — handoff visibility banner (structural, when handoff role exists).
# =========================================================================== #
def test_handoff_surfaces_browser_reachability_when_expose():
    if not _CONSUMER_HANDOFF.is_file():
        pytest.skip("consumer_handoff role not present")
    text = _CONSUMER_HANDOFF.read_text(encoding="utf-8")
    assert "expose" in text, (
        "the consumer handoff must surface the browser-reachability requirement "
        "for a user-facing (expose) workload"
    )


# =========================================================================== #
# Live-caught regression (tmp/live-verify-v0.13.0): the Phase-2 cross-VLAN
# preflight probed the APPLICATION port (expose.port) — but in Phase 2 the
# workload guest has only just been provisioned: Docker (common role) and the
# workload stack (openbao-agent deploy) only land in Phase 3, so nothing can be
# listening yet. The probe therefore failed closed on EVERY fresh bring-up and
# mis-blamed the §3 gateway. Fix: Phase 2 probes L3 reachability on a port that
# is guaranteed up at that point (sshd — the keyscan just succeeded), and a
# Phase-4 post-deploy end-to-end check THROUGH Traefik proves the route works.
# =========================================================================== #
import yaml  # noqa: E402

_CONSUMER_PROVISION_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_provision" / "defaults" / "main.yml"
)
_HELLO_WORKLOAD_DEPLOY_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "hello_workload_deploy" / "defaults" / "main.yml"
)


def _iter_tasks(node):
    """Yield every task mapping, descending into block/rescue/always."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            if key in node:
                yield from _iter_tasks(node[key])


def _tasks(path):
    return list(_iter_tasks(yaml.safe_load(path.read_text(encoding="utf-8"))))


def test_phase2_preflight_probes_l3_not_the_not_yet_deployed_app_port():
    """The Phase-2 Traefik->backend probe must NOT target expose.port (nothing
    listens there until Phase 3); it targets the L3 probe port (sshd)."""
    probes = [
        t for t in _tasks(_CONSUMER_PROVISION)
        if "ansible.builtin.wait_for" in t
        and "consumer_traefik_guest_host" in str(t.get("delegate_to", ""))
    ]
    assert probes, "expected the delegated Traefik->backend wait_for preflight"
    for t in probes:
        port = str(t["ansible.builtin.wait_for"].get("port", ""))
        assert "expose.port" not in port, (
            "Phase-2 preflight must not probe the application port — the workload "
            f"is not deployed until Phase 3 (got port={port!r})"
        )
        assert "consumer_expose_l3_probe_port" in port, (
            f"Phase-2 preflight must probe consumer_expose_l3_probe_port (got {port!r})"
        )
    defaults = yaml.safe_load(_CONSUMER_PROVISION_DEFAULTS.read_text(encoding="utf-8"))
    assert int(defaults["consumer_expose_l3_probe_port"]) == 22


def test_deploy_step_verifies_exposed_route_end_to_end_through_traefik():
    """The deploy step must prove the published route works THROUGH Traefik (Host
    header = the published FQDN, sent to the Traefik ingress), gated on expose, and
    fail closed on a missing route (404) or dead backend (502-504).

    Retargeted by fix-consumer-expose-route-and-gateway-docs: the check used to sit
    in the onboarding handoff (Phase 4), which runs before the workload is deployed
    and probed "/"; it now lives in the deploy step and requests expose.health_path
    (operator decision Q1: onboarding makes no route check).
    """
    tasks = _tasks(_HELLO_WORKLOAD_DEPLOY)
    route_probes = [
        t for t in tasks
        if "ansible.builtin.uri" in t
        and "hello_workload_traefik_ingress_host" in str(t["ansible.builtin.uri"].get("url", ""))
    ]
    assert route_probes, "the deploy step must probe the published route via the Traefik ingress"
    probe = route_probes[0]
    uri = probe["ansible.builtin.uri"]
    assert "openbao_agent_workload_host_rule" in str(uri.get("headers", {}).get("Host", "")), (
        "the route probe must send the published FQDN as the Host header"
    )
    assert "health_path" in str(uri.get("url", "")), "the probe must request expose.health_path"
    assert "expose" in str(probe.get("when", "")), "probe must be gated on expose"
    asserts = [
        t for t in tasks
        if "ansible.builtin.assert" in t
        and "hello_workload_route_probe" in str(t["ansible.builtin.assert"].get("that", ""))
    ]
    assert asserts, "a fail-closed assert on the route probe result is required"
    that = " ".join(map(str, asserts[0]["ansible.builtin.assert"]["that"]))
    for code in ("404", "502", "503", "504"):
        assert code in that, f"the route assert must treat HTTP {code} as failure"
    assert not [t for t in _tasks(_CONSUMER_HANDOFF) if "ansible.builtin.uri" in t and "expose" in str(t)], (
        "the onboarding handoff must no longer probe the route"
    )
    defaults = yaml.safe_load(_HELLO_WORKLOAD_DEPLOY_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults["hello_workload_traefik_ingress_host"] == "10.0.20.12"
