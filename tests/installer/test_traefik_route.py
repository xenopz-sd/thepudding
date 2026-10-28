"""Offline structural + render tests for the traefik_route Route_Mechanism.

Task 1.4 (spec: traefik-cross-guest-ingress) — requirements.md R1.1, R1.2,
R1.3, R1.4, R1.6, R3.4, R3.5; design.md §"Testing Strategy" (Offline merge gate:
Route_Fragment render test, Delegate-and-path test, Swap-gate test).

The traefik_route role (Shape B — a tiny role, per design §"Component 1") renders
ONE Traefik v3 file-provider Route_Fragment (exactly one http.routers + one
http.services entry) and DELEGATES it onto the Traefik_Guest's already-watched
file-provider directory, so Traefik discovers a backend on a SEPARATE guest with
no SVC-09 traefik_install edit. This module pins:

  * RENDER (R1.1/R1.3/R1.4) — rendering traefik-route-fragment.yml.j2 with
    representative Grafana vars yields valid Traefik v3 YAML with EXACTLY one
    http.routers entry (rule Host(`grafana.<domain>`), entryPoints [websecure],
    service, tls:{}) and EXACTLY one http.services entry (loadBalancer single url
    http://10.0.20.19:3000); route_tls=false => no tls key.
  * DELEGATE-PATH (R1.2) — the dir-ensure + fragment-render tasks delegate_to
    {{ traefik_guest_host }} and write under {{ traefik_dynamic_config_dir }}
    with the ansible_user/ansible_python_interpreter vars block (the
    mock_tls_self_signed delegate pattern); the fragment dest is
    {{ traefik_dynamic_config_dir }}/{{ route_name }}-route.yml.
  * NO SVC-09 EDIT (R1.6) — the role's tasks reference only the dynamic dir path,
    never the traefik_install role (structural).
  * SWAP-GATE (R3.4/R3.5) — an INFO debug keyed on traefik_route_enabled, and a
    fail-closed assert gated `when: traefik_route_enabled | bool` whose `that:`
    requires route_name/route_host_rule/route_backend_url/traefik_guest_host
    non-empty.

Pure-structure, offline: PyYAML parse of the role defaults + the role task file
+ a Jinja render of the fragment template — no infra, no terraform binary. The
task-dict helpers and the offline Jinja harness (with the ansible `bool` filter
stand-in) are LOCAL copies, mirroring the tests/installer/test_obs_*.py siblings
— the conftest-collision rule forbids a cross-tree import (tests/installer vs
infra/tests share a same-named sys.path-injecting conftest.py).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_traefik_route.py -v
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
_DEFAULTS = _ROLE / "defaults" / "main.yml"
_TASKS = _ROLE / "tasks" / "main.yml"
_FRAGMENT_TMPL = _ROLE / "templates" / "traefik-route-fragment.yml.j2"


# --------------------------------------------------------------------------- #
# Task-dict helpers (local copies; conftest-collision rule — no cross-tree import).
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_task_file(path: Path) -> list[dict]:
    out: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _defaults() -> dict:
    raw = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "traefik_route defaults must be a non-empty mapping"
    return raw


# --------------------------------------------------------------------------- #
# Faithful offline Jinja harness (local copy of the *_mocks.py render harness;
# the two pytest roots run as SEPARATE invocations and the shared same-named
# conftest.py forbids a cross-tree import).
# --------------------------------------------------------------------------- #
def _ansible_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["bool"] = _ansible_bool
    env.filters["lower"] = lambda s: str(s).lower()
    env.filters["basename"] = lambda s: os.path.basename(str(s))
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    return env


def _render(template_path: Path, variables: dict) -> str:
    assert template_path.is_file(), f"expected template at {template_path}"
    env = _make_env()
    tmpl = env.from_string(template_path.read_text(encoding="utf-8"))
    return tmpl.render(**variables)


def _grafana_vars(**overrides) -> dict:
    """Representative Grafana call-site vars (design §"Component 1" table / R4.2)."""
    base = {
        "route_name": "grafana",
        "route_host_rule": "grafana.platform.example.test",
        "route_backend_url": "http://10.0.20.19:3000",
        "route_entrypoint": "websecure",
        "route_tls": True,
    }
    base.update(overrides)
    return base


# =========================================================================== #
# RENDER — one router + one service, valid Traefik v3 YAML (R1.1/R1.3/R1.4).
# =========================================================================== #
def test_fragment_renders_exactly_one_router_and_one_service():
    """Rendering the fragment for Grafana yields valid Traefik v3 YAML with
    EXACTLY one http.routers entry and EXACTLY one http.services entry (R1.1)."""
    doc = yaml.safe_load(_render(_FRAGMENT_TMPL, _grafana_vars()))

    assert isinstance(doc, dict) and "http" in doc, "fragment must be a Traefik v3 http doc"
    http = doc["http"]

    routers = http.get("routers") or {}
    services = http.get("services") or {}
    assert len(routers) == 1, f"must render EXACTLY one http.routers entry, got {list(routers)}"
    assert len(services) == 1, f"must render EXACTLY one http.services entry, got {list(services)}"

    # Both keyed on route_name (router+service id = fragment basename).
    assert set(routers) == {"grafana"}, f"router id must be route_name 'grafana', got {list(routers)}"
    assert set(services) == {"grafana"}, f"service id must be route_name 'grafana', got {list(services)}"


def test_fragment_router_host_rule_websecure_service_and_tls():
    """The http.routers entry binds websecure, a Host(`grafana.<domain>`) rule,
    the service id, and TLS enabled via the default store (tls: {}) (R1.4)."""
    doc = yaml.safe_load(_render(_FRAGMENT_TMPL, _grafana_vars()))
    router = doc["http"]["routers"]["grafana"]

    assert router["rule"] == "Host(`grafana.platform.example.test`)", (
        f"router rule must be a Host() rule derived from the Platform_Domain; got {router['rule']!r}"
    )
    assert router["entryPoints"] == ["websecure"], (
        f"router must bind the websecure entrypoint; got {router.get('entryPoints')!r}"
    )
    assert router["service"] == "grafana", "router must reference its service by route_name"

    assert "tls" in router, "router must enable TLS (R1.4) when route_tls=true"
    assert router["tls"] == {} or router["tls"] is None, (
        f"router tls must use the DEFAULT certificate store (tls: {{}}); got {router['tls']!r}"
    )


def test_fragment_service_loadbalancer_single_vlan20_url():
    """The http.services entry sets a loadBalancer with a SINGLE server url =
    the Backend_Service's VLAN-20 IP:port (http://10.0.20.19:3000) (R1.3)."""
    doc = yaml.safe_load(_render(_FRAGMENT_TMPL, _grafana_vars()))
    service = doc["http"]["services"]["grafana"]

    lb = service.get("loadBalancer") or {}
    servers = lb.get("servers") or []
    assert len(servers) == 1, f"loadBalancer must have EXACTLY one server; got {servers!r}"
    assert servers[0] == {"url": "http://10.0.20.19:3000"}, (
        f"the single server url must be the VLAN-20 backend IP:port; got {servers[0]!r}"
    )


def test_fragment_route_tls_false_omits_tls_key():
    """route_tls=false => the router renders NO tls key (plain-HTTP router)."""
    doc = yaml.safe_load(_render(_FRAGMENT_TMPL, _grafana_vars(route_tls=False)))
    router = doc["http"]["routers"]["grafana"]
    assert "tls" not in router, (
        f"route_tls=false must omit the tls key entirely; got router={router!r}"
    )
    # Still a valid single router/service doc.
    assert len(doc["http"]["routers"]) == 1 and len(doc["http"]["services"]) == 1


# =========================================================================== #
# DELEGATE-PATH — delegate_to traefik_guest_host, write under the dynamic dir,
# the mock_tls_self_signed delegate vars block, and the *-route.yml dest (R1.2).
# =========================================================================== #
def test_dir_ensure_and_render_delegate_to_traefik_guest_host():
    """The dir-ensure (file) + fragment-render (template) tasks both delegate_to
    {{ traefik_guest_host }} with the ansible_user/ansible_python_interpreter vars
    block (the mock_tls_self_signed delegate pattern)."""
    tasks = _load_task_file(_TASKS)

    dir_tasks = [
        t for t in tasks
        if ("ansible.builtin.file" in t or t.get("file") is not None)
        and isinstance(t.get("ansible.builtin.file", t.get("file")), dict)
        and t.get("ansible.builtin.file", t.get("file", {})).get("state") == "directory"
    ]
    tmpl_tasks = [t for t in tasks if ("ansible.builtin.template" in t or "template" in t)]

    assert dir_tasks, "role must ensure the file-provider dir exists on the Traefik_Guest"
    assert tmpl_tasks, "role must render the Route_Fragment via ansible.builtin.template"

    for t in dir_tasks + tmpl_tasks:
        assert t.get("delegate_to") == "{{ traefik_guest_host }}", (
            "every task that touches the Traefik_Guest must "
            f"delegate_to '{{{{ traefik_guest_host }}}}'; got {t.get('delegate_to')!r}"
        )
        tvars = t.get("vars") or {}
        assert "ansible_user" in tvars and "ansible_python_interpreter" in tvars, (
            "the delegate task must carry the ansible_user / ansible_python_interpreter vars "
            f"block (mock_tls_self_signed delegate pattern); got vars={tvars!r}"
        )


def test_dir_ensure_targets_the_dynamic_config_dir():
    """The dir-ensure task ensures {{ traefik_dynamic_config_dir }} (SVC-09's
    already-watched file-provider dir), not a fresh/re-owned path."""
    tasks = _load_task_file(_TASKS)
    dir_tasks = [
        t for t in tasks
        if isinstance(t.get("ansible.builtin.file", t.get("file")), dict)
        and t.get("ansible.builtin.file", t.get("file", {})).get("state") == "directory"
    ]
    assert dir_tasks, "role must ensure the file-provider dir exists"
    for t in dir_tasks:
        spec = t.get("ansible.builtin.file", t.get("file", {}))
        assert spec.get("path") == "{{ traefik_dynamic_config_dir }}", (
            f"dir-ensure must target the dynamic config dir var; got path={spec.get('path')!r}"
        )


def test_fragment_dest_is_route_name_route_yml_under_dynamic_dir():
    """The fragment dest is {{ traefik_dynamic_config_dir }}/{{ route_name }}-route.yml."""
    tasks = _load_task_file(_TASKS)
    tmpl_tasks = [t for t in tasks if ("ansible.builtin.template" in t or "template" in t)]
    assert tmpl_tasks, "role must render the Route_Fragment via a template task"
    for t in tmpl_tasks:
        spec = t.get("ansible.builtin.template", t.get("template", {}))
        assert spec.get("src") == "traefik-route-fragment.yml.j2", (
            f"template src must be the Route_Fragment template; got src={spec.get('src')!r}"
        )
        assert spec.get("dest") == (
            "{{ traefik_dynamic_config_dir }}/{{ route_name }}-route.yml"
        ), (
            "fragment dest must be "
            "'{{ traefik_dynamic_config_dir }}/{{ route_name }}-route.yml'; "
            f"got dest={spec.get('dest')!r}"
        )


# =========================================================================== #
# NO SVC-09 EDIT (R1.6) — the role references only the dynamic dir path, never
# the traefik_install role (structural).
# =========================================================================== #
def test_role_does_not_touch_traefik_install_role():
    """The role's tasks/defaults never reference ansible/roles/traefik_install/
    nor include/import that role — it only drops a *-route.yml into the dynamic
    dir SVC-09 already enables + watches (R1.6)."""
    tasks_text = _TASKS.read_text(encoding="utf-8")
    defaults_text = _DEFAULTS.read_text(encoding="utf-8")

    for text, label in ((tasks_text, "tasks/main.yml"), (defaults_text, "defaults/main.yml")):
        assert "roles/traefik_install" not in text, (
            f"{label} must not reference the SVC-09 traefik_install role path (R1.6)"
        )

    # No include_role / import_role / role dependency pulls in traefik_install.
    tasks = _load_task_file(_TASKS)
    for t in tasks:
        for key in (
            "ansible.builtin.include_role", "include_role",
            "ansible.builtin.import_role", "import_role",
        ):
            spec = t.get(key)
            if isinstance(spec, dict):
                assert spec.get("name") != "traefik_install", (
                    f"the role must not {key} traefik_install (R1.6 — no SVC-09 edit)"
                )

    # The only Traefik path the role touches is the dynamic-dir var (not a
    # traefik_install template/config path).
    assert "traefik_dynamic_config_dir" in tasks_text, (
        "the role must reference only the shared traefik_dynamic_config_dir var"
    )


# =========================================================================== #
# SWAP-GATE (R3.4/R3.5) — INFO debug keyed on the flag + fail-closed assert.
# =========================================================================== #
def test_swap_gate_flag_defaults_true():
    """traefik_route_enabled defaults TRUE — the real cross-guest route is the
    default production posture (R3.1)."""
    d = _defaults()
    assert "traefik_route_enabled" in d, "traefik_route_enabled must be declared in defaults"
    assert d["traefik_route_enabled"] is True, (
        "traefik_route_enabled must default TRUE (real file-provider route is the default "
        "production posture, R3.1)"
    )


def test_swap_gate_info_debug_keyed_on_flag():
    """An INFO debug task states the selected path and why, keyed on
    traefik_route_enabled (R3.4 / composition-wiring swap-gate step 4)."""
    tasks = _load_task_file(_TASKS)
    debugs = [t for t in tasks if ("ansible.builtin.debug" in t or "debug" in t)]
    assert debugs, "role must emit an INFO swap-gate debug line (R3.4)"

    keyed = []
    for t in debugs:
        spec = t.get("ansible.builtin.debug", t.get("debug", {})) or {}
        msg = str(spec.get("msg", ""))
        if "traefik_route_enabled" in msg:
            keyed.append(t)
    assert keyed, (
        "the swap-gate INFO debug msg must be keyed on traefik_route_enabled, stating whether "
        "the real route or the mock-edge path was selected and why (R3.4)"
    )


def test_swap_gate_fail_closed_assert_requires_identity_vars():
    """A fail-closed assert gated `when: traefik_route_enabled | bool` requires
    route_name / route_host_rule / route_backend_url / traefik_guest_host be
    non-empty — never render a broken/partial route (R3.5)."""
    tasks = _load_task_file(_TASKS)
    asserts = [t for t in tasks if ("ansible.builtin.assert" in t or "assert" in t)]
    assert asserts, "role must have a fail-closed assert (R3.5)"

    fail_closed = []
    for t in asserts:
        when = str(t.get("when", "")).replace(" ", "")
        if "traefik_route_enabled|bool" not in when or "not" in when:
            continue
        spec = t.get("ansible.builtin.assert", t.get("assert", {})) or {}
        clauses = " ".join(str(c) for c in (spec.get("that") or []))
        required = ("route_name", "route_host_rule", "route_backend_url", "traefik_guest_host")
        if all(v in clauses for v in required):
            fail_closed.append(t)

    assert fail_closed, (
        "role must fail closed (assert, when: traefik_route_enabled | bool) requiring "
        "route_name, route_host_rule, route_backend_url AND traefik_guest_host to be "
        "non-empty before rendering any fragment (R3.5)"
    )
