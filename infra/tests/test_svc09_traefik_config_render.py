"""Offline config-validation tests for the rendered Traefik static + dynamic config.

Slice-1 Task 3 (spec: svc-09-reverse-proxy) — requirements.md FR-1/FR-4/FR-5 and
design.md §3.2 (traefik_install role). OFFLINE unit / config-validation tests for
the CONFIGURATION layer of SVC-09 — the artifacts the ``traefik_install`` role
templates (``templates/traefik.yml.j2`` static + ``templates/dynamic.yml.j2``
file-provider), rendered with the role's ``defaults/main.yml`` variable set.

Mirrors the sibling ``test_svc07_openbao_config_render.py`` render harness: load
the role defaults with PyYAML, resolve the shallow jinja self-references with a
fixed-point pass, then render each template with a stock jinja2 Environment
(registering the ``bool``/``lower``/``basename`` filters the templates use so a
faithful render matches Ansible's runtime). Always runs offline (no terraform
binary, no live infra).

What is asserted (Task 3 contract):
  Static traefik.yml:
    * entrypoints ``web`` (:80) + ``websecure`` (:443) exist (FR-5), plus the
      internal-only ``metrics`` entrypoint (§13).
    * Docker provider ``exposedByDefault: false`` (FR-1).
    * ``api.insecure: false`` — dashboard never on raw :8080 (FR-6).
    * HTTP->HTTPS redirect on the ``web`` entrypoint (FR-4).
    * a ``letsencrypt`` certificatesResolver is present (FR-2).
  Compose stack (asserted against the role's docker-compose-app invocation, read
    structurally from tasks/main.yml):
    * read-only Docker socket mount (``:ro``) (§12).
    * persistent named acme volume (not tmpfs) (§7).
    * attaches to the external ``edge`` network, and the role creates it (FR-11).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc09_traefik_config_render.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "traefik_install"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TEMPLATES = _ROLE_DIR / "templates"
_STATIC_TMPL = _TEMPLATES / "traefik.yml.j2"
_DYNAMIC_TMPL = _TEMPLATES / "dynamic.yml.j2"
_TASKS_MAIN = _ROLE_DIR / "tasks" / "main.yml"


# --------------------------------------------------------------------------- #
# Load + resolve the role defaults (shallow jinja self-references).
# --------------------------------------------------------------------------- #
def _resolve_self_references(raw: dict) -> dict:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    resolved = dict(raw)

    def _render_scalars(d: dict) -> tuple[dict, bool]:
        changed = False
        out = {}
        for key, val in d.items():
            if isinstance(val, str) and "{{" in val:
                try:
                    rendered = env.from_string(val).render(**resolved)
                except Exception:
                    rendered = val
                if rendered != val:
                    changed = True
                out[key] = rendered
            elif isinstance(val, list):
                new_list = []
                for item in val:
                    if isinstance(item, str) and "{{" in item:
                        try:
                            r = env.from_string(item).render(**resolved)
                        except Exception:
                            r = item
                        if r != item:
                            changed = True
                        new_list.append(r)
                    else:
                        new_list.append(item)
                out[key] = new_list
            else:
                out[key] = val
        return out, changed

    for _ in range(10):
        resolved, changed = _render_scalars(resolved)
        if not changed:
            break
    return resolved


@pytest.fixture(scope="module")
def role_vars() -> dict:
    assert _DEFAULTS.is_file(), f"expected role defaults at {_DEFAULTS}"
    raw = yaml.safe_load(_DEFAULTS.read_text())
    assert isinstance(raw, dict) and raw, "defaults/main.yml must be a non-empty mapping"
    return _resolve_self_references(raw)


def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _render_template(template_path: Path, variables: dict) -> str:
    assert template_path.is_file(), f"expected template at {template_path}"
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    tmpl = env.from_string(template_path.read_text())
    return tmpl.render(**variables)


# --------------------------------------------------------------------------- #
# Static traefik.yml assertions.
# --------------------------------------------------------------------------- #
class TestStaticConfigRender:
    @pytest.fixture(scope="class")
    def static_doc(self, role_vars) -> dict:
        rendered = _render_template(_STATIC_TMPL, role_vars)
        doc = yaml.safe_load(rendered)
        assert isinstance(doc, dict), "rendered traefik.yml must parse as a YAML mapping"
        return doc

    def test_web_and_websecure_entrypoints_present(self, static_doc):
        eps = static_doc.get("entryPoints", {})
        assert "web" in eps and "websecure" in eps, (
            "traefik.yml must define `web` and `websecure` entrypoints (FR-5)"
        )
        assert str(eps["web"]["address"]).endswith(":80"), "web entrypoint must bind :80 (FR-5)"
        assert str(eps["websecure"]["address"]).endswith(":443"), "websecure entrypoint must bind :443 (FR-5)"

    def test_internal_metrics_entrypoint_present(self, static_doc):
        eps = static_doc.get("entryPoints", {})
        assert "metrics" in eps, (
            "traefik.yml must define a dedicated internal-only `metrics` entrypoint (§13)"
        )
        # Metrics must NOT share the public web/websecure ports.
        assert eps["metrics"]["address"] not in (eps["web"]["address"], eps["websecure"]["address"]), (
            "the metrics entrypoint must be a dedicated internal port, not web/websecure (§13)"
        )

    def test_prometheus_metrics_on_internal_entrypoint(self, static_doc):
        metrics = static_doc.get("metrics", {}).get("prometheus", {})
        assert metrics, "traefik.yml must enable the Prometheus metrics provider (§13)"
        assert metrics.get("entryPoint") == "metrics", (
            "Prometheus metrics must be scraped on the dedicated internal `metrics` entrypoint (§13)"
        )

    def test_docker_provider_exposed_by_default_false(self, static_doc):
        docker = static_doc.get("providers", {}).get("docker", {})
        assert docker, "traefik.yml must declare the Docker provider (FR-1)"
        assert docker.get("exposedByDefault") is False, (
            "Docker provider must set exposedByDefault: false so a container must opt in "
            "with traefik.enable=true (FR-1)"
        )

    def test_api_dashboard_not_insecure(self, static_doc):
        api = static_doc.get("api", {})
        assert api.get("dashboard") is True, "dashboard must be enabled (FR-6)"
        assert api.get("insecure") is False, (
            "api.insecure must be false — the dashboard is NEVER served on raw :8080 (FR-6)"
        )

    def test_http_to_https_redirect(self, static_doc):
        web = static_doc.get("entryPoints", {}).get("web", {})
        redir = web.get("http", {}).get("redirections", {}).get("entryPoint", {})
        assert redir.get("to") == "websecure" and redir.get("scheme") == "https", (
            "the web entrypoint must redirect HTTP->HTTPS (to: websecure, scheme: https) (FR-4)"
        )

    def test_letsencrypt_resolver_present(self, static_doc):
        resolvers = static_doc.get("certificatesResolvers", {})
        assert "letsencrypt" in resolvers, (
            "a `letsencrypt` certificatesResolver must be present for public ACME issuance (FR-2)"
        )

    def test_docker_socket_endpoint_is_unix(self, static_doc):
        docker = static_doc.get("providers", {}).get("docker", {})
        assert str(docker.get("endpoint", "")).startswith("unix://"), (
            "Docker provider endpoint must be the unix socket (FR-1)"
        )


# --------------------------------------------------------------------------- #
# Dynamic file-provider config assertions (default posture: both mocks OFF).
# --------------------------------------------------------------------------- #
class TestDynamicConfigRender:
    def test_renders_and_parses_with_mocks_off(self, role_vars):
        rendered = _render_template(_DYNAMIC_TMPL, role_vars)
        doc = yaml.safe_load(rendered)
        assert isinstance(doc, dict), "rendered dynamic.yml must parse as a YAML mapping"
        # Mocks off by default -> no self-signed `tls.stores.default` block.
        assert "tls" not in doc, (
            "with the self-signed TLS mock OFF (default), dynamic.yml must NOT define a "
            "default certificate store (the real resolver issues certs)"
        )

    def test_self_signed_default_cert_rendered_when_mock_on(self, role_vars):
        v = dict(role_vars)
        v["traefik_tls_self_signed"] = True
        rendered = _render_template(_DYNAMIC_TMPL, v)
        doc = yaml.safe_load(rendered)
        store = doc.get("tls", {}).get("stores", {}).get("default", {})
        assert store.get("defaultCertificate"), (
            "with traefik_tls_self_signed=true, dynamic.yml must define a default certificate "
            "the websecure entrypoint serves (Mock 3)"
        )


# --------------------------------------------------------------------------- #
# Compose-stack structural assertions (read from tasks/main.yml).
# --------------------------------------------------------------------------- #
class TestComposeStackShape:
    @pytest.fixture(scope="class")
    def tasks_text(self) -> str:
        assert _TASKS_MAIN.is_file(), f"expected role tasks at {_TASKS_MAIN}"
        return _TASKS_MAIN.read_text()

    def test_docker_socket_mounted_read_only(self, tasks_text):
        assert re.search(r"docker_socket\s*\}\}:ro", tasks_text) or re.search(
            r"/var/run/docker\.sock.*:ro", tasks_text
        ), "the Docker socket must be mounted READ-ONLY (:ro) into the Traefik stack (§12)"

    def test_persistent_acme_named_volume(self, tasks_text):
        # The acme volume is a named volume listed in dca_volumes (persistent),
        # mounted at the acme mount — never a tmpfs.
        assert "traefik_acme_volume" in tasks_text, (
            "the acme.json store must be a persistent NAMED volume (§7) referenced in the stack"
        )
        assert "dca_volumes" in tasks_text, (
            "the named acme volume must be declared in dca_volumes (persistent, not tmpfs)"
        )

    def test_creates_and_owns_real_edge_network(self, tasks_text):
        assert re.search(r"community\.docker\.docker_network", tasks_text), (
            "the role must create the real external `edge` network via community.docker.docker_network (FR-11)"
        )
        assert re.search(r"name:\s*edge", tasks_text), "the created network must be named `edge` (FR-11)"

    def test_attaches_to_external_edge_network(self, tasks_text):
        assert re.search(r'name:\s*"edge",\s*external:\s*true', tasks_text) or (
            "edge" in tasks_text and "external: true" in tasks_text
        ), "the Traefik stack must attach to the external `edge` network (FR-11)"

    def test_ingress_host_ports_bound(self, tasks_text):
        # Traefik IS the ingress edge -> it binds :80 and :443 (the documented
        # FR-7 exception). Assert both entrypoint ports appear in a ports list.
        assert "entrypoint_web_port" in tasks_text and "entrypoint_websecure_port" in tasks_text, (
            "Traefik must bind the web (:80) and websecure (:443) host ports — the documented "
            "ingress-edge exception to the no-host-ports rule (FR-5/§12)"
        )
