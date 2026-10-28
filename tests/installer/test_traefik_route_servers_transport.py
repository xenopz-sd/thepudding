"""Offline render tests for the traefik_route serversTransport extension.

Task 1.3 (spec: svc-07-cross-guest-ingress) — requirements.md R1.1, R1.2,
R1.3, R1.4, R1.6; design.md §"Component 1 — Route_Mechanism (traefik_route,
additively extended)", §"Interface — Route_Fragment schema (https backend, both
rendered forms)", §"Testing Strategy" (serversTransport default-inert /
mock-CA-trust / https-verbatim render tests).

This feature ADDITIVELY extends the shared `traefik_route` Route_Mechanism with
an OPTIONAL, DEFAULT-INERT `serversTransport` capability so an https backend
(OpenBao on :8200) can be reached over a VERIFIED Traefik->backend TLS hop. Two
new optional vars drive it:

  * route_servers_transport_name  (default "") — the serversTransport id + the
    service's loadBalancer.serversTransport reference.
  * route_backend_root_cas        (default "") — a path, ON THE TRAEFIK GUEST, to
    a PUBLIC CA bundle Traefik trusts to verify the backend cert.

Because `traefik_route` is a SHARED symbol (also consumed by
observability_stack/Grafana), the template is guarded so that when EITHER var is
empty (the default), the render is BYTE-IDENTICAL to today's output (no
`loadBalancer.serversTransport` key, no top-level `serversTransports` block) —
the preservation guard for the Grafana consumer. Only when BOTH are non-empty is
a serversTransport emitted.

This module pins:

  * (a) DEFAULT-INERT PRESERVATION (R1.1/R1.3) — with the new vars UNSET/empty
    and the Grafana-style vars, the parsed fragment has NO
    http.services.<name>.loadBalancer.serversTransport key AND NO top-level
    http.serversTransports block (shared-symbol/Grafana preservation guard).
  * (b) SET PATH (R1.1/R1.2) — with the OpenBao mock values, EXACTLY one router +
    one service + one serversTransport; the service's loadBalancer.serversTransport
    == "openbao"; http.serversTransports.openbao.rootCAs == [<mock CA path>]; and
    the server url is https://10.0.20.10:8200 VERBATIM.
  * (c) HTTPS SCHEME VERBATIM (R1.4/R1.6) — an https:// route_backend_url renders
    verbatim into the server url (scheme not rewritten/stripped) with the
    serversTransport vars empty, proving the file-provider url-scheme form carries
    https with no new field. Belt-and-suspenders: an h2c:// url also rides
    verbatim (the required case for THIS spec is https).
  * PARTIAL-SET GUARDRAIL — with ONLY route_servers_transport_name set (root_cas
    empty), NO serversTransport is emitted (both must be non-empty) — pinning the
    template's `_st` AND-guard so a half-configured call never emits a broken
    transport.

Pure-structure, offline: a Jinja render of the fragment template + a PyYAML
parse. No infra, no terraform binary. The offline Jinja harness (with the ansible
`bool` filter stand-in) is a LOCAL copy, mirroring the tests/installer/
test_traefik_route.py sibling — the conftest-collision rule forbids a cross-tree
import (tests/installer vs infra/tests share a same-named sys.path-injecting
conftest.py), and the same rule is honoured WITHIN tests/installer by not
cross-importing the sibling module's harness.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_traefik_route_servers_transport.py -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE = _REPO_ROOT / "ansible" / "roles" / "traefik_route"
_FRAGMENT_TMPL = _ROLE / "templates" / "traefik-route-fragment.yml.j2"


# --------------------------------------------------------------------------- #
# Faithful offline Jinja harness (local copy of the sibling render harness; the
# pytest roots run as SEPARATE invocations and the shared same-named conftest.py
# forbids a cross-tree import — the same rule applies to a cross-module import
# within this tree, so the harness is duplicated locally on purpose).
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    # Mirror Ansible's Jinja2 environment EXACTLY: Ansible renders templates with
    # trim_blocks=True, lstrip_blocks=False, keep_trailing_newline=True. Omitting
    # trim_blocks here renders block tags differently from the live playbook and
    # silently hid a real defect (a `{%- ... %}` leading-strip collapsed the
    # serversTransport lines into invalid YAML under Ansible while this harness —
    # with trim_blocks off — rendered valid YAML and passed). Pin the real
    # settings so this offline render faithfully reproduces the on-cluster render.
    env = Environment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    env.filters["trim"] = lambda s: str(s).strip()
    return env


def _render(variables: dict) -> str:
    assert _FRAGMENT_TMPL.is_file(), f"expected template at {_FRAGMENT_TMPL}"
    env = _make_env()
    tmpl = env.from_string(_FRAGMENT_TMPL.read_text(encoding="utf-8"))
    return tmpl.render(**variables)


def _render_doc(variables: dict) -> dict:
    doc = yaml.safe_load(_render(variables))
    assert isinstance(doc, dict) and "http" in doc, (
        f"fragment must parse to a Traefik v3 http doc; got {doc!r}"
    )
    return doc


# Defaults for the two new optional vars so a StrictUndefined render never trips
# on them when a test leaves them unset (mirrors the role defaults: both "").
_ST_DEFAULTS = {
    "route_servers_transport_name": "",
    "route_backend_root_cas": "",
}


def _grafana_vars(**overrides) -> dict:
    """Representative Grafana call-site vars (the shared-symbol consumer) with
    the serversTransport vars defaulting empty (preservation case)."""
    base = {
        "route_name": "grafana",
        "route_host_rule": "grafana.platform.example.test",
        "route_backend_url": "http://10.0.20.19:3000",
        "route_entrypoint": "websecure",
        "route_tls": True,
    }
    base.update(_ST_DEFAULTS)
    base.update(overrides)
    return base


def _openbao_vars(**overrides) -> dict:
    """OpenBao call-site vars: an https backend reached over a VERIFIED hop via
    the serversTransport capability (design §"Component 1" table)."""
    base = {
        "route_name": "openbao",
        "route_host_rule": "openbao.platform.example.test",
        "route_backend_url": "https://10.0.20.10:8200",
        "route_entrypoint": "websecure",
        "route_tls": True,
        "route_servers_transport_name": "openbao",
        "route_backend_root_cas": "/etc/traefik/dynamic/ca/openbao-mock-ca.crt",
    }
    base.update(overrides)
    return base


# =========================================================================== #
# (a) DEFAULT-INERT PRESERVATION — shared-symbol / Grafana guard (R1.1/R1.3).
# =========================================================================== #
def test_default_inert_no_servers_transport_on_service():
    """With the new serversTransport vars UNSET/empty (the default — Grafana's
    plain-http case), the service's loadBalancer carries NO `serversTransport`
    key. This is the shared-symbol preservation guard: the empty-default render
    must be byte-identical to the pre-extension output for the Grafana consumer.
    """
    doc = _render_doc(_grafana_vars())
    service = doc["http"]["services"]["grafana"]
    lb = service.get("loadBalancer") or {}
    assert "serversTransport" not in lb, (
        "default (empty serversTransport vars) must NOT render a "
        f"loadBalancer.serversTransport key (Grafana preservation); got lb={lb!r}"
    )
    # The single backend server is still rendered normally.
    assert lb.get("servers") == [{"url": "http://10.0.20.19:3000"}], (
        f"the single server url must be unchanged by the extension; got {lb.get('servers')!r}"
    )


def test_default_inert_no_top_level_servers_transports_block():
    """With the new vars UNSET/empty, the fragment emits NO top-level
    `http.serversTransports` block at all (preservation guard)."""
    doc = _render_doc(_grafana_vars())
    http = doc["http"]
    assert "serversTransports" not in http, (
        "default (empty serversTransport vars) must NOT render a top-level "
        f"http.serversTransports block; got http keys={list(http)!r}"
    )


# =========================================================================== #
# (b) SET PATH — one router + one service + one serversTransport (R1.1/R1.2).
# =========================================================================== #
def test_set_path_exactly_one_router_service_and_servers_transport():
    """With BOTH new vars set to OpenBao's mock values, the fragment renders
    EXACTLY one http.routers + one http.services + one http.serversTransports
    entry, each keyed on the OpenBao id."""
    doc = _render_doc(_openbao_vars())
    http = doc["http"]

    routers = http.get("routers") or {}
    services = http.get("services") or {}
    transports = http.get("serversTransports") or {}

    assert set(routers) == {"openbao"}, f"must render one router 'openbao'; got {list(routers)}"
    assert set(services) == {"openbao"}, f"must render one service 'openbao'; got {list(services)}"
    assert set(transports) == {"openbao"}, (
        f"must render EXACTLY one serversTransport 'openbao'; got {list(transports)}"
    )


def test_set_path_service_references_servers_transport_by_name():
    """The service's loadBalancer.serversTransport == the configured name, so
    Traefik uses the verified-backend transport for this service's hop."""
    doc = _render_doc(_openbao_vars())
    lb = doc["http"]["services"]["openbao"]["loadBalancer"]
    assert lb.get("serversTransport") == "openbao", (
        "the service's loadBalancer.serversTransport must reference the configured "
        f"route_servers_transport_name 'openbao'; got {lb.get('serversTransport')!r}"
    )


def test_set_path_servers_transport_root_cas_is_the_mock_ca_path():
    """http.serversTransports.openbao.rootCAs is a single-element list carrying
    the delegated mock CA path verbatim (the PUBLIC cert Traefik trusts)."""
    doc = _render_doc(_openbao_vars())
    transport = doc["http"]["serversTransports"]["openbao"]
    assert transport.get("rootCAs") == [
        "/etc/traefik/dynamic/ca/openbao-mock-ca.crt"
    ], (
        "serversTransports.openbao.rootCAs must be exactly the delegated mock CA "
        f"path; got {transport.get('rootCAs')!r}"
    )


def test_set_path_server_url_is_https_backend_verbatim():
    """On the set path the single backend server url is the OpenBao VLAN-20
    https IP:port VERBATIM (scheme not rewritten/stripped)."""
    doc = _render_doc(_openbao_vars())
    servers = doc["http"]["services"]["openbao"]["loadBalancer"]["servers"]
    assert servers == [{"url": "https://10.0.20.10:8200"}], (
        f"the backend server url must be https://10.0.20.10:8200 verbatim; got {servers!r}"
    )


# =========================================================================== #
# (c) HTTPS SCHEME VERBATIM — file-provider url-scheme form, no new field
# (R1.4/R1.6).
# =========================================================================== #
def test_https_scheme_rides_verbatim_with_servers_transport_empty():
    """The REQUIRED case for THIS spec: an https:// route_backend_url renders
    verbatim into the server url even with the serversTransport vars EMPTY —
    proving the file-provider url-scheme form carries https with NO new field and
    the scheme is neither rewritten nor stripped (R1.4/R1.6)."""
    variables = _grafana_vars(
        route_name="openbao",
        route_host_rule="openbao.platform.example.test",
        route_backend_url="https://10.0.20.10:8200",
    )
    doc = _render_doc(variables)
    service = doc["http"]["services"]["openbao"]
    lb = service["loadBalancer"]

    assert lb["servers"] == [{"url": "https://10.0.20.10:8200"}], (
        "https backend url must ride through verbatim; "
        f"got {lb['servers']!r}"
    )
    # And with the serversTransport vars empty, no transport wiring appears.
    assert "serversTransport" not in lb, (
        "empty serversTransport vars must not add a loadBalancer.serversTransport "
        "key even for an https backend (system-trust-store / prod posture)"
    )
    assert "serversTransports" not in doc["http"], (
        "empty serversTransport vars must not emit a top-level serversTransports block"
    )


def test_h2c_scheme_rides_verbatim_belt_and_suspenders():
    """Belt-and-suspenders for the sibling SVC-06 (ZITADEL) spec: an h2c:// url
    also rides verbatim with the serversTransport vars empty. (The REQUIRED case
    for THIS spec is the https one above.)"""
    variables = _grafana_vars(
        route_name="zitadel",
        route_host_rule="zitadel.platform.example.test",
        route_backend_url="h2c://10.0.20.11:8080",
    )
    doc = _render_doc(variables)
    servers = doc["http"]["services"]["zitadel"]["loadBalancer"]["servers"]
    assert servers == [{"url": "h2c://10.0.20.11:8080"}], (
        f"h2c backend url must ride through verbatim; got {servers!r}"
    )


# =========================================================================== #
# PARTIAL-SET GUARDRAIL — the `_st` AND-guard (both must be non-empty).
# =========================================================================== #
def test_partial_set_name_only_emits_no_servers_transport():
    """With ONLY route_servers_transport_name set (route_backend_root_cas empty),
    NO serversTransport is emitted — pinning the template's `_st` AND-guard so a
    half-configured call never ships a broken/partial transport (a serversTransport
    id with no rootCAs, or a service referencing a missing transport)."""
    variables = _openbao_vars(route_backend_root_cas="")
    doc = _render_doc(variables)
    http = doc["http"]
    lb = http["services"]["openbao"]["loadBalancer"]

    assert "serversTransport" not in lb, (
        "name-only (root_cas empty) must NOT set loadBalancer.serversTransport; "
        f"got lb={lb!r}"
    )
    assert "serversTransports" not in http, (
        "name-only (root_cas empty) must NOT emit a top-level serversTransports "
        f"block; got http keys={list(http)!r}"
    )
    # The backend server url is still rendered normally.
    assert lb.get("servers") == [{"url": "https://10.0.20.10:8200"}]


def test_partial_set_root_cas_only_emits_no_servers_transport():
    """Symmetric guardrail: with ONLY route_backend_root_cas set (the name
    empty), NO serversTransport is emitted — both vars must be non-empty."""
    variables = _openbao_vars(route_servers_transport_name="")
    doc = _render_doc(variables)
    http = doc["http"]
    lb = http["services"]["openbao"]["loadBalancer"]

    assert "serversTransport" not in lb, (
        "root_cas-only (name empty) must NOT set loadBalancer.serversTransport; "
        f"got lb={lb!r}"
    )
    assert "serversTransports" not in http, (
        "root_cas-only (name empty) must NOT emit a top-level serversTransports "
        f"block; got http keys={list(http)!r}"
    )
