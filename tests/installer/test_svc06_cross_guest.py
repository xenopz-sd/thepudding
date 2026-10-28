"""Offline structural tests for SVC-06 ZITADEL cross-guest Traefik ingress.

Task 1.3 (spec: svc-06-cross-guest-ingress) — requirements.md R1.1, R1.2, R1.3,
R1.5, R2.1, R2.2, R2.5, R2.6, R3.1, R3.2, R3.3, R3.5; design.md §"Testing
Strategy" (Offline merge gate: h2c render / no-serversTransport, VLAN20_Port
bind, swap-gate, mock-edge preservation).

Wave-0/1 tasks 1.1–1.2 added the ZITADEL ingress defaults
(``zitadel_traefik_route_enabled`` / ``zitadel_traefik_guest_host``) and rewired
``zitadel-server/tasks/main.yml`` so the ZITADEL docker-compose-app stack has TWO
mutually exclusive cross-guest ingress paths selected by
``zitadel_mock_edge_network``, mirroring the merged Grafana (``http``) and
OpenBao (``https``) conversions — but SIMPLER, because ZITADEL serves cleartext
``h2c`` (``ZITADEL_TLS_ENABLED=false``):

  * REAL path (default, zitadel_mock_edge_network=false, R3.1): the zitadel
    service publishes the VLAN20_Port bound to the guest's OWN VLAN-20 IP
    (``{{ zitadel_bind_ip }}:8080:8080`` — host-IP-scoped, never 0.0.0.0/LAN,
    R2.1/R2.2); NO ``edge`` network, NO Traefik Docker labels; a Route_Fragment
    is delegated onto the Traefik_Guest via the ``traefik_route`` role, with an
    ``h2c://…:8080`` backend and NO serversTransport vars (the default-inert
    path — the cleartext hop has no backend cert, R1.3).
  * MOCK-EDGE path (dev/offline, zitadel_mock_edge_network=true, R2.6/R3.2): NO
    published port (``ports: []``), attach to the local ``edge`` network + emit
    the Traefik discovery labels (incl. scheme=h2c); NO file-provider route.

Because the ports/networks/labels/dca_networks are Jinja CONDITIONALS embedded
in the docker-compose-app include task
(``{{ [...] if (zitadel_mock_edge_network|bool) else [...] }}``), this module
asserts the raw conditional string expressions carry the expected both-branch
tokens keyed on ``zitadel_mock_edge_network`` (mirroring how
test_svc07_cross_guest.py asserts the openbao ports/networks/labels
conditionals), and parses the role defaults + task file via PyYAML.

Pure-structure, offline: PyYAML parse of the role defaults + main.yml — no infra,
no terraform binary. The task-dict helpers are LOCAL copies, mirroring the
tests/installer/test_svc07_cross_guest.py sibling — the conftest-collision rule
forbids a cross-tree import.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_cross_guest.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ZITADEL_ROLE = _REPO_ROOT / "ansible" / "roles" / "zitadel-server"
_ZITADEL_DEFAULTS = _ZITADEL_ROLE / "defaults" / "main.yml"
_ZITADEL_TASKS = _ZITADEL_ROLE / "tasks" / "main.yml"


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
    raw = yaml.safe_load(_ZITADEL_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "zitadel-server defaults must be a non-empty mapping"
    return raw


def _tasks() -> list[dict]:
    return _load_task_file(_ZITADEL_TASKS)


# --------------------------------------------------------------------------- #
# Extract the ZITADEL docker-compose-app deploy task + its zitadel service spec.
# --------------------------------------------------------------------------- #
def _zitadel_dca_deploy_task() -> dict:
    """Return the ``include_role: docker-compose-app`` task that deploys the
    ZITADEL Compose stack (dca_app_name == zitadel_stack_name)."""
    deploys = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if not (isinstance(spec, dict) and spec.get("name") == "docker-compose-app"):
            continue
        tvars = t.get("vars") or {}
        if str(tvars.get("dca_app_name", "")).strip() == "{{ zitadel_stack_name }}":
            deploys.append(t)
    assert len(deploys) == 1, (
        f"expected exactly one ZITADEL docker-compose-app deploy task in zitadel-server "
        f"main.yml, got {len(deploys)}"
    )
    return deploys[0]


def _zitadel_service(deploy_task: dict) -> dict:
    """The first (only) dca_services entry — the zitadel service spec."""
    tvars = deploy_task.get("vars") or {}
    services = tvars.get("dca_services") or []
    assert services and isinstance(services, list), "dca_services must be a non-empty list"
    svc = services[0]
    assert isinstance(svc, dict) and svc.get("name") == "zitadel", (
        f"dca_services[0] must be the zitadel service; got {svc!r}"
    )
    return svc


# =========================================================================== #
# (1) h2c RENDER / NO serversTransport — the real-path traefik_route include
#     passes an h2c:// backend built from zitadel_bind_ip + zitadel_http_port,
#     does NOT pass route_servers_transport_name / route_backend_root_cas (the
#     default-inert path, so NO serversTransport renders), and is gated
#     `when: not (zitadel_mock_edge_network | bool)`. (R1.1/R1.2/R1.3/R1.5)
# =========================================================================== #
def test_traefik_route_include_wires_h2c_backend_and_no_servers_transport():
    """The REAL-path include_role traefik_route passes an h2c:// backend URL
    built from zitadel_bind_ip + zitadel_http_port and leaves the serversTransport
    vars ABSENT (the default-inert path — no serversTransport block renders),
    gated `when: not (zitadel_mock_edge_network | bool)` (R1.1/R1.2/R1.3/R1.5)."""
    route_includes = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "traefik_route":
            route_includes.append(t)
    assert len(route_includes) == 1, (
        f"zitadel-server main.yml must include the traefik_route role exactly once; "
        f"got {len(route_includes)}"
    )
    t = route_includes[0]
    tvars = t.get("vars") or {}

    assert tvars.get("route_name") == "zitadel", (
        f"route_name must be 'zitadel'; got {tvars.get('route_name')!r}"
    )
    assert "zitadel_external_domain" in str(tvars.get("route_host_rule", "")), (
        f"route_host_rule must be the ZITADEL external-domain var; "
        f"got {tvars.get('route_host_rule')!r}"
    )

    backend = str(tvars.get("route_backend_url", ""))
    assert backend.strip().startswith("h2c://"), (
        f"route_backend_url must be an h2c:// VLAN-20 url (ZITADEL serves cleartext "
        f"h2c); got {backend!r}"
    )
    assert "zitadel_bind_ip" in backend and "zitadel_http_port" in backend, (
        "route_backend_url must build the backend from zitadel_bind_ip + "
        f"zitadel_http_port (h2c://…:8080); got {backend!r}"
    )

    # The default-inert path (R1.3): NEITHER serversTransport var is passed, so
    # the rendered fragment carries NO serversTransport block / key.
    assert "route_servers_transport_name" not in tvars, (
        "the ZITADEL traefik_route call must NOT pass route_servers_transport_name "
        "(h2c hop is cleartext — no backend cert to verify, R1.3)"
    )
    assert "route_backend_root_cas" not in tvars, (
        "the ZITADEL traefik_route call must NOT pass route_backend_root_cas "
        "(h2c hop is cleartext — no CA material, R1.3)"
    )

    when = str(t.get("when", "")).replace(" ", "")
    assert "not(zitadel_mock_edge_network|bool)" in when, (
        "the traefik_route include must be gated `when: not (zitadel_mock_edge_network | bool)` "
        f"— real path only; got when={t.get('when')!r}"
    )


# =========================================================================== #
# (2) VLAN20_PORT bind — real path binds the guest VLAN-20 IP; mock publishes [].
#     The edge network + labels are mock-only. (R2.1/R2.2/R2.5/R2.6/R3.5)
# =========================================================================== #
def test_zitadel_ports_conditional_binds_vlan20_ip_not_wildcard():
    """The zitadel ports value is a Jinja conditional on zitadel_mock_edge_network
    whose text references zitadel_bind_ip (the guest VLAN-20 IP) and NOT 0.0.0.0,
    and carries BOTH branches (the empty-list mock branch AND the host-IP-bound
    real branch) — R2.1/R2.2."""
    svc = _zitadel_service(_zitadel_dca_deploy_task())
    ports_expr = svc.get("ports")
    assert isinstance(ports_expr, str) and "{{" in ports_expr, (
        f"ports must be a Jinja conditional expression; got {ports_expr!r}"
    )
    compact = ports_expr.replace(" ", "")

    assert "zitadel_mock_edge_network" in compact, (
        "ports must be conditional on zitadel_mock_edge_network"
    )
    assert "zitadel_bind_ip" in compact, (
        "the real-path port mapping must bind the guest's OWN VLAN-20 IP via "
        "zitadel_bind_ip (R2.1), never a bare host-wide port"
    )
    assert "zitadel_http_port" in compact, (
        "the real-path port mapping must publish zitadel_http_port (8080)"
    )
    # The real branch must NEVER bind 0.0.0.0 / a LAN-facing address (R2.2).
    assert "0.0.0.0" not in compact, "the ZITADEL port must NEVER bind 0.0.0.0 (R2.2)"
    # Both branches present: empty-list (mock) branch + the mapping (real) branch.
    assert "[]" in compact, "ports conditional must carry the empty-list (mock-edge) branch"
    assert "else" in compact, "ports conditional must carry both branches (if/else keyed on the flag)"


def test_zitadel_networks_labels_dca_networks_edge_and_labels_mock_only():
    """The zitadel service `networks`/`labels` and the stack `dca_networks` are
    Jinja conditionals: the `edge` network + Traefik labels apply ONLY on the
    mock-edge path and resolve empty on the real path (R2.5/R2.6/R3.5)."""
    deploy = _zitadel_dca_deploy_task()
    svc = _zitadel_service(deploy)
    tvars = deploy.get("vars") or {}

    networks_expr = svc.get("networks")
    labels_expr = svc.get("labels")
    dca_networks_expr = tvars.get("dca_networks")

    for label, expr in (
        ("networks", networks_expr),
        ("labels", labels_expr),
        ("dca_networks", dca_networks_expr),
    ):
        assert isinstance(expr, str) and "{{" in expr, (
            f"{label} must be a Jinja conditional; got {expr!r}"
        )
        compact = expr.replace(" ", "")
        assert "zitadel_mock_edge_network" in compact, (
            f"{label} must be gated on zitadel_mock_edge_network (both branches)"
        )

    # networks: edge net var in the mock branch, empty on the real path (R2.5).
    net_compact = networks_expr.replace(" ", "")
    assert "zitadel_traefik_network" in net_compact, (
        "networks must reference zitadel_traefik_network (the `edge` net) in the mock branch"
    )
    assert "[]" in net_compact, "networks real-path branch must resolve to [] (no cross-guest edge)"

    # labels: the mock-only label set var in the mock branch, empty on real (R2.5).
    lbl_compact = labels_expr.replace(" ", "")
    assert "zitadel_traefik_labels" in lbl_compact, (
        "labels must reference the mock-only zitadel_traefik_labels set in the mock branch"
    )
    assert "[]" in lbl_compact, "labels real-path branch must resolve to [] (no Traefik labels)"

    # dca_networks: external edge dependency on mock only, empty on real (R2.5).
    dca_compact = dca_networks_expr.replace(" ", "")
    assert "zitadel_traefik_network" in dca_compact and "external" in dca_compact, (
        "dca_networks must declare the external `edge` dependency (name+external) in the mock branch"
    )
    assert "[]" in dca_compact, "dca_networks real-path branch must resolve to [] (no external edge)"


# =========================================================================== #
# (3) SWAP-GATE — default-real route, mechanism wiring, INFO decision.
#     (R3.1/R3.3)
# =========================================================================== #
def test_route_enabled_defaults_true():
    """zitadel_traefik_route_enabled defaults TRUE — the real cross-guest
    file-provider route is the default production posture (R3.1)."""
    d = _defaults()
    assert "zitadel_traefik_route_enabled" in d, (
        "zitadel_traefik_route_enabled must be declared in defaults"
    )
    assert d["zitadel_traefik_route_enabled"] is True, (
        "zitadel_traefik_route_enabled must default TRUE — the real Route_Fragment is "
        "delegated onto the Traefik_Guest by default (fed to traefik_route_enabled, R3.1)"
    )


def test_traefik_route_include_wires_swap_gate_from_route_enabled():
    """The REAL-path include_role traefik_route feeds traefik_route_enabled from
    zitadel_traefik_route_enabled — the swap-gate (R3.1/R3.3)."""
    route_includes = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "traefik_route":
            route_includes.append(t)
    assert len(route_includes) == 1, (
        f"zitadel-server main.yml must include the traefik_route role exactly once; "
        f"got {len(route_includes)}"
    )
    tvars = route_includes[0].get("vars") or {}
    te = str(tvars.get("traefik_route_enabled", ""))
    assert "zitadel_traefik_route_enabled" in te, (
        f"traefik_route_enabled must be fed from zitadel_traefik_route_enabled; got {te!r}"
    )


def test_info_swap_gate_debug_task_keyed_on_mock_edge():
    """An INFO swap-gate `debug` task states which ingress path was selected and
    is keyed on zitadel_mock_edge_network (R3.3)."""
    debug_tasks = []
    for t in _tasks():
        spec = t.get("ansible.builtin.debug", t.get("debug"))
        if not isinstance(spec, dict):
            continue
        msg = str(spec.get("msg", ""))
        if "zitadel_mock_edge_network" in msg and ("swap-gate" in msg or "MOCK_EDGE" in msg):
            debug_tasks.append(t)
    assert debug_tasks, (
        "expected an INFO swap-gate debug task whose msg is keyed on zitadel_mock_edge_network "
        "and states the selected ingress path (R3.3)"
    )


# =========================================================================== #
# (4) MOCK-EDGE PRESERVATION — default-off, retained with edge + scheme=h2c.
#     (R3.2)
# =========================================================================== #
def test_mock_edge_network_default_off_and_retained():
    """zitadel_mock_edge_network remains declared and defaults FALSE — the
    permanent, default-off dev/offline affordance per mock-stand-ins.md (R3.2)."""
    d = _defaults()
    assert "zitadel_mock_edge_network" in d, (
        "zitadel_mock_edge_network must remain declared in defaults (not removed, R3.2)"
    )
    assert d["zitadel_mock_edge_network"] is False, (
        "zitadel_mock_edge_network must default FALSE — the REAL cross-guest file-provider "
        "route is the default posture; the mock-edge path is an opt-in dev/offline affordance (R3.2)"
    )


def test_mock_edge_labels_retained_with_h2c_scheme():
    """The mock-edge path keeps the Traefik discovery labels, including the
    backend scheme=h2c label, in a MOCK-EDGE-ONLY set_fact gated on
    zitadel_mock_edge_network (R3.2)."""
    label_setfacts = []
    for t in _tasks():
        spec = t.get("ansible.builtin.set_fact", t.get("set_fact"))
        if isinstance(spec, dict) and "zitadel_traefik_labels" in spec:
            label_setfacts.append(t)
    assert len(label_setfacts) == 1, (
        f"expected exactly one set_fact composing zitadel_traefik_labels; got {len(label_setfacts)}"
    )
    t = label_setfacts[0]
    labels = (t.get("ansible.builtin.set_fact", t.get("set_fact")) or {})["zitadel_traefik_labels"]
    joined = "".join(str(x) for x in labels)
    assert "traefik.enable=true" in joined, "the mock-edge label set must retain traefik.enable"
    assert "loadbalancer.server.scheme=h2c" in joined, (
        "the mock-edge label set must retain the backend scheme=h2c label (R3.2)"
    )

    # Gated on the mock-edge path (labels are inert cross-guest).
    when = str(t.get("when", "")).replace(" ", "")
    assert "zitadel_mock_edge_network|bool" in when, (
        "the mock-edge label set_fact must be gated on zitadel_mock_edge_network (mock path only)"
    )
