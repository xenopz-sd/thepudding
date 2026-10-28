"""Offline structural tests for SVC-17/18 Grafana cross-guest Traefik ingress.

Task 3.6 (spec: traefik-cross-guest-ingress) — requirements.md R2.1, R2.2,
R3.2, R3.3, R4.1, R4.2, R4.5; design.md §"Testing Strategy" (Offline merge
gate: VLAN20_Port binding test, Route_Fragment delegate, edge-gated-behind-mock,
mocks default-off).

Wave-3 tasks 3.1–3.4 rewired ``observability_stack/tasks/grafana.yml`` + its
``defaults/main.yml`` so the Grafana docker-compose-app stack has TWO mutually
exclusive cross-guest ingress paths selected by ``obs_grafana_mock_edge``:

  * REAL path (default, obs_grafana_mock_edge=false, R3.1): the Grafana service
    publishes the VLAN20_Port bound to the guest's OWN VLAN-20 IP
    (``{{ ansible_host }}:3000:3000`` — host-IP-scoped, never 0.0.0.0/LAN,
    R2.1/R2.2/R4.1/R4.5); NO ``edge`` network, NO Traefik Docker labels; a
    Route_Fragment is delegated onto the Traefik_Guest via the ``traefik_route``
    role (R4.2).
  * MOCK-EDGE path (dev/offline, obs_grafana_mock_edge=true, R3.2/R3.3): NO
    published port (``ports: []``), attach to the local ``edge`` network + emit
    the Traefik discovery labels; NO file-provider route.

Because the ports/networks/labels/dca_networks are Jinja CONDITIONALS embedded
in the docker-compose-app include task
(``{{ [...] if (obs_grafana_mock_edge|bool) else [...] }}``), this module both
(a) asserts the raw conditional string expression carries the expected
both-branch tokens AND (b) RENDERS each conditional with ``obs_grafana_mock_edge``
true vs false via a faithful offline Jinja harness and asserts the resolved
values. It also asserts the ``traefik_route`` include contract (R4.2) and the two
mock default-off flags (R3.1).

Pure-structure, offline: PyYAML parse of the role defaults + grafana.yml + a
Jinja render of the extracted conditional expressions — no infra, no terraform
binary. The task-dict helpers and Jinja harness are LOCAL copies, mirroring the
tests/installer/test_obs_*.py + test_traefik_route.py siblings — the
conftest-collision rule forbids a cross-tree import.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_grafana_cross_guest.py -v
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
_OBS_ROLE = _REPO_ROOT / "ansible" / "roles" / "observability_stack"
_OBS_DEFAULTS = _OBS_ROLE / "defaults" / "main.yml"
_GRAFANA_TASKS = _OBS_ROLE / "tasks" / "grafana.yml"


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
    raw = yaml.safe_load(_OBS_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "observability_stack defaults must be a non-empty mapping"
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
    env.filters["string"] = lambda v: str(v)
    env.filters["to_json"] = lambda v, **kw: json.dumps(v)
    return env


def _render_expr(expr: str, variables: dict):
    """Render a Jinja ``{{ ... }}`` expression and parse the result as YAML so a
    rendered list/dict expression resolves to a Python object."""
    env = _make_env()
    rendered = env.from_string(expr).render(**variables)
    return yaml.safe_load(rendered)


# --------------------------------------------------------------------------- #
# Extract the docker-compose-app deploy task + its Grafana service spec.
# --------------------------------------------------------------------------- #
def _dca_deploy_task() -> dict:
    """Return the ``include_role: docker-compose-app`` task that deploys the
    Grafana Compose stack."""
    tasks = _load_task_file(_GRAFANA_TASKS)
    deploys = []
    for t in tasks:
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "docker-compose-app":
            deploys.append(t)
    assert len(deploys) == 1, (
        f"expected exactly one docker-compose-app deploy task in grafana.yml, got {len(deploys)}"
    )
    return deploys[0]


def _grafana_service(deploy_task: dict) -> dict:
    """The first (only) dca_services entry — the Grafana service spec."""
    tvars = deploy_task.get("vars") or {}
    services = tvars.get("dca_services") or []
    assert services and isinstance(services, list), "dca_services must be a non-empty list"
    svc = services[0]
    assert isinstance(svc, dict) and svc.get("name") == "grafana", (
        f"dca_services[0] must be the grafana service; got {svc!r}"
    )
    return svc


def _ingress_vars(mock_edge: bool) -> dict:
    """Minimal var context sufficient to render the ports/networks/labels/
    dca_networks conditionals both ways."""
    return {
        "obs_grafana_mock_edge": mock_edge,
        "ansible_host": "10.0.20.19",
        "observability_grafana_port": 3000,
        "observability_edge_network": "edge",
        # observability_grafana_labels is set by a prior set_fact (mock-edge path
        # only); supply a representative list so the labels conditional renders.
        "observability_grafana_labels": [
            "traefik.enable=true",
            "traefik.docker.network=edge",
        ],
    }


# =========================================================================== #
# VLAN20_PORT — real path binds the guest VLAN-20 IP; mock-edge publishes none.
# (R2.1, R2.2, R4.1, R4.5)
# =========================================================================== #
def test_grafana_ports_conditional_carries_both_branch_tokens():
    """The ports value is a Jinja conditional on obs_grafana_mock_edge whose
    text carries BOTH branches: the empty-list (mock-edge) branch AND the
    ansible_host-bound mapping (real) branch — never a bare 3000:3000 or
    0.0.0.0 binding (R2.1/R2.2)."""
    svc = _grafana_service(_dca_deploy_task())
    ports_expr = svc.get("ports")
    assert isinstance(ports_expr, str) and "{{" in ports_expr, (
        f"ports must be a Jinja conditional expression; got {ports_expr!r}"
    )
    compact = ports_expr.replace(" ", "")

    assert "obs_grafana_mock_edge" in compact, "ports must be conditional on obs_grafana_mock_edge"
    assert "ansible_host" in compact, (
        "the real-path port mapping must bind the guest's OWN VLAN-20 IP via ansible_host "
        "(R2.1), never a bare host-wide port"
    )
    # The real branch must NOT bind 0.0.0.0 / a LAN-facing address (R2.2).
    assert "0.0.0.0" not in compact, "the Grafana port must NEVER bind 0.0.0.0 (R2.2)"


def test_grafana_ports_render_real_path_binds_guest_vlan20_ip():
    """Rendering the ports conditional with obs_grafana_mock_edge=false resolves
    to a SINGLE host-IP-scoped mapping 10.0.20.19:3000:3000 (R2.1/R4.1)."""
    svc = _grafana_service(_dca_deploy_task())
    ports = _render_expr(svc["ports"], _ingress_vars(mock_edge=False))

    assert ports == ["10.0.20.19:3000:3000"], (
        "the real path must publish the VLAN20_Port bound to the guest's own VLAN-20 IP "
        f"(10.0.20.19:3000:3000 — host-IP-scoped, R2.1/R4.1); got {ports!r}"
    )
    # Host-IP-scoped: a leading IP segment, not a bare "3000:3000" (R2.1/R2.2).
    assert ports[0].startswith("10.0.20.19:"), (
        f"the published mapping must be host-IP-scoped (IP:hostport:containerport); got {ports[0]!r}"
    )
    assert not ports[0].startswith("0.0.0.0"), "must never bind 0.0.0.0 (R2.2)"


def test_grafana_ports_render_mock_edge_path_publishes_nothing():
    """Rendering the ports conditional with obs_grafana_mock_edge=true resolves
    to [] — no published port on the mock-edge path (discovery is via labels)."""
    svc = _grafana_service(_dca_deploy_task())
    ports = _render_expr(svc["ports"], _ingress_vars(mock_edge=True))
    assert ports == [], f"the mock-edge path must publish NO port (ports: []); got {ports!r}"


# =========================================================================== #
# ROUTE INCLUDE — the traefik_route include contract on the real path (R4.2).
# =========================================================================== #
def test_grafana_includes_traefik_route_with_grafana_contract():
    """grafana.yml include_role traefik_route with route_name=grafana,
    route_host_rule={{ observability_grafana_host }}, a route_backend_url built
    from ansible_host + observability_grafana_port, route_entrypoint=websecure,
    route_tls=true, traefik_guest_host={{ obs_traefik_guest_host }}, gated
    `when: not (obs_grafana_mock_edge | bool)` (R4.2)."""
    tasks = _load_task_file(_GRAFANA_TASKS)

    route_includes = []
    for t in tasks:
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "traefik_route":
            route_includes.append(t)
    assert len(route_includes) == 1, (
        f"grafana.yml must include the traefik_route role exactly once; got {len(route_includes)}"
    )
    t = route_includes[0]
    tvars = t.get("vars") or {}

    assert tvars.get("route_name") == "grafana", f"route_name must be 'grafana'; got {tvars.get('route_name')!r}"
    assert tvars.get("route_host_rule") == "{{ observability_grafana_host }}", (
        f"route_host_rule must be the Grafana Host() var; got {tvars.get('route_host_rule')!r}"
    )

    backend = str(tvars.get("route_backend_url", ""))
    assert "ansible_host" in backend and "observability_grafana_port" in backend, (
        "route_backend_url must build the VLAN-20 backend url from ansible_host + "
        f"observability_grafana_port; got {backend!r}"
    )
    assert backend.strip().startswith("http://"), (
        f"route_backend_url must be a plain-http VLAN-20 url (TLS terminates at Traefik); got {backend!r}"
    )

    assert tvars.get("route_entrypoint") == "websecure", (
        f"route_entrypoint must be websecure; got {tvars.get('route_entrypoint')!r}"
    )
    assert tvars.get("route_tls") is True, f"route_tls must be true; got {tvars.get('route_tls')!r}"
    assert tvars.get("traefik_guest_host") == "{{ obs_traefik_guest_host }}", (
        "traefik_guest_host must be the Mock-B delegate target var obs_traefik_guest_host; "
        f"got {tvars.get('traefik_guest_host')!r}"
    )

    when = str(t.get("when", "")).replace(" ", "")
    assert when == "not(obs_grafana_mock_edge|bool)", (
        "the traefik_route include must be gated `when: not (obs_grafana_mock_edge | bool)` — "
        f"real path only; got when={t.get('when')!r}"
    )


# =========================================================================== #
# EDGE GATED — the `edge` attachment + Traefik labels apply ONLY on mock-edge.
# (R3.2, R3.3)
# =========================================================================== #
def test_networks_and_dca_networks_edge_gated_behind_mock_edge():
    """The Grafana service `networks` and the stack `dca_networks` conditionals
    attach the `edge` network ONLY when obs_grafana_mock_edge is true; the real
    path resolves to [] (no cross-guest edge, R3.3)."""
    deploy = _dca_deploy_task()
    svc = _grafana_service(deploy)
    tvars = deploy.get("vars") or {}

    networks_expr = svc.get("networks")
    dca_networks_expr = tvars.get("dca_networks")
    assert isinstance(networks_expr, str) and "{{" in networks_expr, (
        f"service networks must be a Jinja conditional; got {networks_expr!r}"
    )
    assert isinstance(dca_networks_expr, str) and "{{" in dca_networks_expr, (
        f"dca_networks must be a Jinja conditional; got {dca_networks_expr!r}"
    )

    # Both conditionals keyed on the mock-edge flag + the edge network var.
    for label, expr in (("networks", networks_expr), ("dca_networks", dca_networks_expr)):
        compact = expr.replace(" ", "")
        assert "obs_grafana_mock_edge" in compact, f"{label} must be gated on obs_grafana_mock_edge"
        assert "observability_edge_network" in compact, (
            f"{label} must reference observability_edge_network (the `edge` net) in the mock branch"
        )

    # Real path (flag false): NO edge attachment anywhere (R3.3).
    assert _render_expr(networks_expr, _ingress_vars(mock_edge=False)) == [], (
        "the real path must NOT attach the Grafana service to a cross-guest `edge` network (R3.3)"
    )
    assert _render_expr(dca_networks_expr, _ingress_vars(mock_edge=False)) == [], (
        "the real path must declare NO external `edge` network dependency (R3.3)"
    )

    # Mock-edge path (flag true): the `edge` network IS attached (R3.2).
    assert _render_expr(networks_expr, _ingress_vars(mock_edge=True)) == ["edge"], (
        "the mock-edge path must attach the Grafana service to the local `edge` network (R3.2)"
    )
    dca_mock = _render_expr(dca_networks_expr, _ingress_vars(mock_edge=True))
    assert dca_mock == [{"name": "edge", "external": True}], (
        "the mock-edge path must declare the external `edge` network dependency "
        f"(name=edge, external=true); got {dca_mock!r}"
    )


def test_labels_gated_behind_mock_edge():
    """The Grafana service `labels` conditional emits the Traefik discovery
    labels ONLY on the mock-edge path; the real path resolves to [] (Traefik on
    another guest cannot read them — the file-provider route supersedes, R3.2)."""
    svc = _grafana_service(_dca_deploy_task())
    labels_expr = svc.get("labels")
    assert isinstance(labels_expr, str) and "{{" in labels_expr, (
        f"service labels must be a Jinja conditional; got {labels_expr!r}"
    )
    compact = labels_expr.replace(" ", "")
    assert "obs_grafana_mock_edge" in compact, "labels must be gated on obs_grafana_mock_edge"

    # Real path (flag false): NO Traefik labels.
    assert _render_expr(labels_expr, _ingress_vars(mock_edge=False)) == [], (
        "the real path must emit NO Traefik Docker labels (superseded by the file-provider "
        "route; Traefik on another guest cannot read them, R3.2)"
    )
    # Mock-edge path (flag true): the discovery labels are emitted.
    rendered = _render_expr(labels_expr, _ingress_vars(mock_edge=True))
    assert rendered == ["traefik.enable=true", "traefik.docker.network=edge"], (
        f"the mock-edge path must emit the Traefik discovery labels; got {rendered!r}"
    )


# =========================================================================== #
# MOCKS DEFAULT-OFF — the real route is the default production posture (R3.1).
# =========================================================================== #
def test_cross_guest_mock_flags_default_posture():
    """obs_grafana_mock_edge defaults FALSE (real file-provider route is the
    default, R3.1); obs_grafana_traefik_route_enabled defaults TRUE (the real
    route is delegated by default)."""
    d = _defaults()

    assert "obs_grafana_mock_edge" in d, "obs_grafana_mock_edge must be declared in defaults"
    assert d["obs_grafana_mock_edge"] is False, (
        "obs_grafana_mock_edge must default FALSE — the REAL cross-guest file-provider route "
        "is the default production posture; the mock-edge path is an opt-in dev/offline "
        "affordance (R3.1/R3.2)"
    )

    assert "obs_grafana_traefik_route_enabled" in d, (
        "obs_grafana_traefik_route_enabled must be declared in defaults"
    )
    assert d["obs_grafana_traefik_route_enabled"] is True, (
        "obs_grafana_traefik_route_enabled must default TRUE — the Grafana Route_Fragment is "
        "delegated onto the Traefik_Guest by default on the real path (R3.1)"
    )
