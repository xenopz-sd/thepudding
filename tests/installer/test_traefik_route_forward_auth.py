"""WS-C23 — the shared ``traefik_route`` role can put a Traefik forwardAuth middleware in front of a route.

Background (found preparing WS-C19): the mail catcher accepted ``mailcatcher_forward_auth_address`` as an access control,
but ``traefik_route`` rendered one router + one service and could not attach a middleware, so the option was accepted and
never enforced. It was removed (ADR-0025 follow-up). This adds the capability properly, as an ADDITIVE, DEFAULT-INERT
extension of a SHARED symbol (consumers: Grafana, OpenBao, ZITADEL, the mail catcher, the hello workload), exactly like the
earlier serversTransport extension:

* with ``route_forward_auth_address`` empty (default) the render is BYTE-IDENTICAL to today's (golden hashes below were taken
  from the unmodified template);
* with it set, the fragment gains ``http.middlewares.<route>-auth.forwardAuth`` and the router lists it;
* a malformed address / header is refused by the role (fail closed), never rendered.

Pure offline: a Jinja render with Ansible's real settings + a PyYAML parse, and the role's own assert evaluated by Ansible's
templater.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar, trust_as_template  # noqa: E402

_ROLE = Path(__file__).resolve().parents[2] / "ansible" / "roles" / "traefik_route"
_TMPL = _ROLE / "templates" / "traefik-route-fragment.yml.j2"

# sha256 of the render of the UNMODIFIED template (taken before this change).
_GOLDEN = {
    "grafana": "97f589dcd2a7a8356c197defb8c6ac1d7ce37f8626cfb20745f38c9ecc225323",
    "openbao": "56f6f647eaf6b9db42873c5298c175ecc126b6fb84ea6b98167c987a383208fe",
    "notls": "b85930a5f02d641bbb29761eef0347463cec2bd8b89e4baf6e4d5e487826bc1c",
}


def _env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=False,
                      keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["trim"] = lambda s: str(s).strip()
    return env


def _render(variables: dict) -> str:
    return _env().from_string(_TMPL.read_text(encoding="utf-8")).render(**variables)


def _grafana(**kw):
    base = {"route_name": "grafana", "route_host_rule": "grafana.platform.example.test",
            "route_backend_url": "http://10.0.20.19:3000", "route_entrypoint": "websecure", "route_tls": True,
            "route_servers_transport_name": "", "route_backend_root_cas": ""}
    base.update(kw)
    return base


def _openbao(**kw):
    return _grafana(route_name="openbao", route_host_rule="openbao.platform.example.test",
                    route_backend_url="https://10.0.20.10:8200", route_servers_transport_name="openbao",
                    route_backend_root_cas="/etc/traefik/dynamic/ca/openbao-mock-ca.crt", **kw)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Preservation: default-inert, byte-identical
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,variables", [("grafana", _grafana()), ("openbao", _openbao()),
                                            ("notls", _grafana(route_tls=False))])
def test_unset_renders_byte_identically_to_the_unmodified_template(name, variables):
    assert _sha(_render(variables)) == _GOLDEN[name]


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_address_is_the_same_as_unset(empty):
    assert _sha(_render(_grafana(route_forward_auth_address=empty))) == _GOLDEN["grafana"]


def test_no_middlewares_key_anywhere_when_unset():
    doc = yaml.safe_load(_render(_grafana()))
    assert "middlewares" not in doc["http"] and "middlewares" not in doc["http"]["routers"]["grafana"]


# --------------------------------------------------------------------------- #
# Set path
# --------------------------------------------------------------------------- #
def _doc(**kw):
    return yaml.safe_load(_render(_grafana(**kw)))


def test_set_renders_one_forward_auth_middleware_and_the_router_references_it():
    """Bug condition: there was no way to attach a middleware to a route at all."""
    doc = _doc(route_forward_auth_address="http://10.0.20.15:4181/auth")
    mw = doc["http"]["middlewares"]
    assert list(mw) == ["grafana-auth"]
    assert mw["grafana-auth"] == {"forwardAuth": {"address": "http://10.0.20.15:4181/auth"}}
    assert doc["http"]["routers"]["grafana"]["middlewares"] == ["grafana-auth"]


def test_set_keeps_the_router_service_and_tls_exactly_as_before():
    base = yaml.safe_load(_render(_grafana()))
    doc = _doc(route_forward_auth_address="https://auth.platform.example.test/verify")
    assert doc["http"]["services"] == base["http"]["services"]
    router = dict(doc["http"]["routers"]["grafana"])
    router.pop("middlewares")
    assert router == base["http"]["routers"]["grafana"]


def test_response_headers_are_rendered_only_when_given():
    none = _doc(route_forward_auth_address="http://a.example.test/v")
    assert "authResponseHeaders" not in none["http"]["middlewares"]["grafana-auth"]["forwardAuth"]
    some = _doc(route_forward_auth_address="http://a.example.test/v",
                route_forward_auth_response_headers=["X-Forwarded-User", "X-Auth-Role"])
    assert some["http"]["middlewares"]["grafana-auth"]["forwardAuth"]["authResponseHeaders"] == [
        "X-Forwarded-User", "X-Auth-Role"]


def test_it_composes_with_the_servers_transport_extension():
    doc = yaml.safe_load(_render(_openbao(route_forward_auth_address="http://10.0.20.15:4181/auth")))
    assert doc["http"]["serversTransports"]["openbao"]["rootCAs"] == ["/etc/traefik/dynamic/ca/openbao-mock-ca.crt"]
    assert doc["http"]["routers"]["openbao"]["middlewares"] == ["openbao-auth"]


def test_the_rendered_fragment_is_a_single_valid_yaml_document_with_the_managed_header():
    text = _render(_grafana(route_forward_auth_address="http://10.0.20.15:4181/auth"))
    assert text.startswith("# MANAGED BY ANSIBLE")
    assert len(list(yaml.safe_load_all(text))) == 1


# --------------------------------------------------------------------------- #
# Role: defaults, fail-closed validation, no secret
# --------------------------------------------------------------------------- #
def _defaults() -> dict:
    return yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")) if isinstance(t, dict)]


def test_defaults_are_inert():
    d = _defaults()
    assert d["route_forward_auth_address"] == "" and d["route_forward_auth_response_headers"] == []


def _validator():
    return next(t for t in _tasks() if "forward-auth" in str(t.get("name")).lower() and "ansible.builtin.assert" in t)


def _verdict(address, headers=None) -> bool:
    t = _validator()
    variables = {"route_name": "grafana", "route_forward_auth_address": address,
                 "route_forward_auth_response_headers": headers or [], "traefik_route_enabled": True}
    tpl = Templar(loader=DataLoader(), variables=variables)
    gate = all(bool(tpl.template(trust_as_template("{{ " + str(w) + " }}"))) for w in
               (t["when"] if isinstance(t["when"], list) else [t["when"]]))
    if not gate:
        return True  # the validator does not run => nothing to refuse
    return all(bool(tpl.template(trust_as_template("{{ " + " ".join(str(c).split()) + " }}")))
               for c in t["ansible.builtin.assert"]["that"])


@pytest.mark.parametrize("address", ["", "http://10.0.20.15:4181/auth", "https://auth.platform.example.test/verify?x=1"])
def test_valid_or_unset_addresses_pass(address):
    assert _verdict(address) is True


@pytest.mark.parametrize("address", [
    "ftp://x.example.test/a", "10.0.20.15:4181/auth", "//x.example.test", "javascript:alert(1)",
    'http://x.example.test/a"\n  other: injected', "http://x.example.test/a b", "http://x.example.test/a\nmiddlewares:"])
def test_malformed_addresses_are_refused_not_rendered(address):
    assert _verdict(address) is False


@pytest.mark.parametrize("headers,ok", [(["X-Forwarded-User"], True), (["Bad Header"], False), (["X-A\n- injected"], False),
                                        ("X-Forwarded-User", False)])
def test_response_header_names_are_validated(headers, ok):
    assert _verdict("http://x.example.test/v", headers) is ok


def test_the_validator_fails_with_a_message_naming_the_remedy():
    msg = " ".join(str(_validator()["ansible.builtin.assert"]["fail_msg"]).split())
    assert "route_forward_auth_address" in msg and "http://" in msg


def test_the_template_never_renders_a_secret_shaped_value():
    import re
    text = re.sub(r"\{#.*?#\}", "", _TMPL.read_text(encoding="utf-8"), flags=re.S).lower()  # code, not the doc comment
    for bad in ("password", "token", "secret", "authorization"):
        assert bad not in text.replace("authresponseheaders", ""), bad
