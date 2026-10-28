"""Offline structural tests for SVC-07 OpenBao cross-guest Traefik ingress.

Task 3.4 (spec: svc-07-cross-guest-ingress) — requirements.md R2.1, R2.2, R2.3,
R2.4, R2.5, R2.6, R3.1, R3.2, R3.3, R3.4, R3.5; design.md §"Testing Strategy"
(Offline merge gate: VLAN20_Port bind, swap-gate, mock-CA public-cert-only
delegate, mock-edge preservation).

Wave-3 tasks 3.1–3.3 rewired ``openbao_install/tasks/main.yml`` + its
``defaults/main.yml`` so the OpenBao PRIMARY docker-compose-app stack has TWO
mutually exclusive cross-guest ingress paths selected by
``openbao_mock_edge_network``, mirroring the merged Grafana conversion EXACTLY:

  * REAL path (default, openbao_mock_edge_network=false, R3.1): the primary
    service publishes the VLAN20_Port bound to the guest's OWN VLAN-20 IP
    (``{{ openbao_primary_ip }}:8200:8200`` — host-IP-scoped, never 0.0.0.0/LAN,
    R2.1/R2.2); NO ``edge`` network, NO Traefik Docker labels; a Route_Fragment
    is delegated onto the Traefik_Guest via the ``traefik_route`` role, with a
    ``https://…:8200`` backend and (on the mock-cert path) a verified
    serversTransport trusting the delegated PUBLIC mock CA (R2.3).
  * MOCK-EDGE path (dev/offline, openbao_mock_edge_network=true, R2.5/R3.2): NO
    published port (``ports: []``), attach to the local ``edge`` network + emit
    the Traefik discovery labels (incl. scheme=https); NO file-provider route.

Because the ports/networks/labels/dca_networks are Jinja CONDITIONALS embedded
in the docker-compose-app include task
(``{{ [...] if (openbao_mock_edge_network|bool) else [...] }}``), this module
asserts the raw conditional string expressions carry the expected both-branch
tokens keyed on ``openbao_mock_edge_network`` (mirroring how
test_obs_grafana_cross_guest.py asserts the grafana ports/networks/labels
conditionals), and parses the role defaults + task file via PyYAML.

Pure-structure, offline: PyYAML parse of the role defaults + main.yml — no infra,
no terraform binary. The task-dict helpers are LOCAL copies, mirroring the
tests/installer/test_obs_*.py siblings — the conftest-collision rule forbids a
cross-tree import.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc07_cross_guest.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENBAO_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao_install"
_OPENBAO_DEFAULTS = _OPENBAO_ROLE / "defaults" / "main.yml"
_OPENBAO_TASKS = _OPENBAO_ROLE / "tasks" / "main.yml"


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
    raw = yaml.safe_load(_OPENBAO_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "openbao_install defaults must be a non-empty mapping"
    return raw


def _tasks() -> list[dict]:
    return _load_task_file(_OPENBAO_TASKS)


# --------------------------------------------------------------------------- #
# Extract the PRIMARY docker-compose-app deploy task + its openbao service spec.
# --------------------------------------------------------------------------- #
def _primary_dca_deploy_task() -> dict:
    """Return the ``include_role: docker-compose-app`` task that deploys the
    PRIMARY OpenBao Compose stack (dca_app_name == openbao_stack_name)."""
    deploys = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if not (isinstance(spec, dict) and spec.get("name") == "docker-compose-app"):
            continue
        tvars = t.get("vars") or {}
        if str(tvars.get("dca_app_name", "")).strip() == "{{ openbao_stack_name }}":
            deploys.append(t)
    assert len(deploys) == 1, (
        f"expected exactly one PRIMARY docker-compose-app deploy task in openbao_install "
        f"main.yml, got {len(deploys)}"
    )
    return deploys[0]


def _primary_service(deploy_task: dict) -> dict:
    """The first (only) dca_services entry — the openbao primary service spec."""
    tvars = deploy_task.get("vars") or {}
    services = tvars.get("dca_services") or []
    assert services and isinstance(services, list), "dca_services must be a non-empty list"
    svc = services[0]
    assert isinstance(svc, dict) and svc.get("name") == "openbao", (
        f"dca_services[0] must be the openbao primary service; got {svc!r}"
    )
    return svc


# =========================================================================== #
# (1) VLAN20_PORT bind — real path binds the guest VLAN-20 IP; mock publishes [].
#     The edge network + labels are mock-only. (R2.1/R2.2/R2.4/R2.5/R3.5)
# =========================================================================== #
def test_primary_ports_binds_vlan20_ip_not_wildcard():
    """The primary `ports` publishes the VLAN20_Port bound to the guest's OWN
    VLAN-20 IP (openbao_primary_ip:openbao_api_port) and NEVER 0.0.0.0 — R2.1/R2.2.

    UPDATED by fix-svc07-openbao-metrics-port-publish: the `:8200` host-publish
    is now DECOUPLED from the edge/Traefik path and published UNCONDITIONALLY on
    BOTH paths (mirroring the unsealer's unconditional static `ports`), so the
    Day-0 mock-edge bring-up still host-publishes `:8200` for the direct VLAN-20
    Prometheus scrape (previously the mock-edge branch yielded `[]` → OpenBao
    `up == 0`). `ports` is therefore no longer a Jinja conditional keyed on
    openbao_mock_edge_network; the edge-discovery wiring (networks/labels/
    dca_networks) stays path-selected and is asserted by the sibling test
    test_primary_networks_labels_dca_networks_edge_and_labels_mock_only."""
    svc = _primary_service(_primary_dca_deploy_task())
    ports_expr = svc.get("ports")
    # Now an unconditional static list, mirroring the unsealer; stringify to
    # assert on content regardless of exact shape.
    compact = "".join(str(p) for p in ports_expr).replace(" ", "") if isinstance(
        ports_expr, (list, tuple)
    ) else str(ports_expr).replace(" ", "")
    assert compact, f"ports must declare a value; got {ports_expr!r}"

    # DECOUPLED: the host-port publish must NOT be keyed on the edge-path flag.
    assert "openbao_mock_edge_network" not in compact, (
        "the primary `ports` must be UNCONDITIONAL (decoupled from the edge path, "
        "fix-svc07-openbao-metrics-port-publish) — it must NOT be keyed on "
        "openbao_mock_edge_network, so the Day-0 mock-edge bring-up still "
        "host-publishes :8200 for the direct VLAN-20 scrape"
    )
    assert "openbao_primary_ip" in compact, (
        "the port mapping must bind the guest's OWN VLAN-20 IP via "
        "openbao_primary_ip (R2.1), never a bare host-wide port"
    )
    assert "openbao_api_port" in compact, (
        "the port mapping must publish openbao_api_port (8200)"
    )
    # NEVER bind 0.0.0.0 / a LAN-facing address (R2.2).
    assert "0.0.0.0" not in compact, "the OpenBao primary port must NEVER bind 0.0.0.0 (R2.2)"


def test_primary_networks_labels_dca_networks_edge_and_labels_mock_only():
    """The primary service `networks`/`labels` and the stack `dca_networks` are
    Jinja conditionals: the `edge` network + Traefik labels apply ONLY on the
    mock-edge path and resolve empty on the real path (R2.4/R2.5/R3.5)."""
    deploy = _primary_dca_deploy_task()
    svc = _primary_service(deploy)
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
        assert "openbao_mock_edge_network" in compact, (
            f"{label} must be gated on openbao_mock_edge_network (both branches)"
        )

    # networks: edge net var in the mock branch, empty on the real path (R2.4).
    net_compact = networks_expr.replace(" ", "")
    assert "openbao_traefik_network" in net_compact, (
        "networks must reference openbao_traefik_network (the `edge` net) in the mock branch"
    )
    assert "[]" in net_compact, "networks real-path branch must resolve to [] (no cross-guest edge)"

    # labels: the mock-only label set var in the mock branch, empty on real (R2.4).
    lbl_compact = labels_expr.replace(" ", "")
    assert "openbao_primary_labels" in lbl_compact, (
        "labels must reference the mock-only openbao_primary_labels set in the mock branch"
    )
    assert "[]" in lbl_compact, "labels real-path branch must resolve to [] (no Traefik labels)"

    # dca_networks: external edge dependency on mock only, empty on real (R2.4).
    dca_compact = dca_networks_expr.replace(" ", "")
    assert "openbao_traefik_network" in dca_compact and "external" in dca_compact, (
        "dca_networks must declare the external `edge` dependency (name+external) in the mock branch"
    )
    assert "[]" in dca_compact, "dca_networks real-path branch must resolve to [] (no external edge)"


# =========================================================================== #
# (2) SWAP-GATE — default-real route, INFO decision, traefik_route wiring.
#     (R3.1/R3.3/R3.4)
# =========================================================================== #
def test_route_enabled_defaults_true():
    """openbao_traefik_route_enabled defaults TRUE — the real cross-guest
    file-provider route is the default production posture (R3.1)."""
    d = _defaults()
    assert "openbao_traefik_route_enabled" in d, (
        "openbao_traefik_route_enabled must be declared in defaults"
    )
    assert d["openbao_traefik_route_enabled"] is True, (
        "openbao_traefik_route_enabled must default TRUE — the real Route_Fragment is "
        "delegated onto the Traefik_Guest by default (fed to traefik_route_enabled, R3.1)"
    )


def test_info_swap_gate_debug_task_keyed_on_mock_edge():
    """An INFO swap-gate `debug` task states which ingress path was selected and
    is keyed on openbao_mock_edge_network (R3.3)."""
    debug_tasks = []
    for t in _tasks():
        spec = t.get("ansible.builtin.debug", t.get("debug"))
        if not isinstance(spec, dict):
            continue
        msg = str(spec.get("msg", ""))
        if "openbao_mock_edge_network" in msg and ("swap-gate" in msg or "MOCK_EDGE" in msg):
            debug_tasks.append(t)
    assert debug_tasks, (
        "expected an INFO swap-gate debug task whose msg is keyed on openbao_mock_edge_network "
        "and states the selected ingress path (R3.3)"
    )


def test_traefik_route_include_wires_route_identity_and_https_backend():
    """The REAL-path include_role traefik_route passes traefik_route_enabled from
    openbao_traefik_route_enabled plus the route identity vars — a
    https://…:8200 backend url, route_servers_transport_name, and
    route_backend_root_cas — and is gated `when: not (openbao_mock_edge_network |
    bool)` (R3.1/R3.3/R3.4; R2.3)."""
    route_includes = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "traefik_route":
            route_includes.append(t)
    assert len(route_includes) == 1, (
        f"openbao_install main.yml must include the traefik_route role exactly once; "
        f"got {len(route_includes)}"
    )
    t = route_includes[0]
    tvars = t.get("vars") or {}

    # Swap-gate: traefik_route_enabled fed from openbao_traefik_route_enabled.
    te = str(tvars.get("traefik_route_enabled", ""))
    assert "openbao_traefik_route_enabled" in te, (
        f"traefik_route_enabled must be fed from openbao_traefik_route_enabled; got {te!r}"
    )

    assert tvars.get("route_name") == "openbao", (
        f"route_name must be 'openbao'; got {tvars.get('route_name')!r}"
    )
    assert "openbao" in str(tvars.get("route_host_rule", "")), (
        f"route_host_rule must be the OpenBao Host() var; got {tvars.get('route_host_rule')!r}"
    )

    backend = str(tvars.get("route_backend_url", ""))
    assert backend.strip().startswith("https://"), (
        f"route_backend_url must be an https:// VLAN-20 url (OpenBao terminates TLS); got {backend!r}"
    )
    assert "openbao_primary_ip" in backend and "openbao_api_port" in backend, (
        "route_backend_url must build the backend from openbao_primary_ip + openbao_api_port "
        f"(https://…:8200); got {backend!r}"
    )

    # The verified-hop wiring (R2.3): both serversTransport vars are wired through.
    assert "route_servers_transport_name" in tvars, (
        "the traefik_route call must pass route_servers_transport_name (verified https hop)"
    )
    rbrc = str(tvars.get("route_backend_root_cas", ""))
    assert "openbao_route_backend_root_cas" in rbrc, (
        "route_backend_root_cas must be fed from openbao_route_backend_root_cas; "
        f"got {rbrc!r}"
    )

    when = str(t.get("when", "")).replace(" ", "")
    assert "not(openbao_mock_edge_network|bool)" in when, (
        "the traefik_route include must be gated `when: not (openbao_mock_edge_network | bool)` "
        f"— real path only; got when={t.get('when')!r}"
    )


# =========================================================================== #
# (3) MOCK-CA DELEGATE — public cert only, 0644, delegate_to Traefik_Guest,
#     gated on NOT mock-edge AND self-signed; private key NEVER copied.
#     (R2.3/R2.6)
# =========================================================================== #
def _delegated_to_traefik_guest(task: dict) -> bool:
    return str(task.get("delegate_to", "")).replace(" ", "") == "{{openbao_traefik_guest_host}}"


def test_mock_ca_delegate_copies_public_cert_only_0644_gated():
    """The mock-CA delegate copies the PUBLIC cert (references
    openbao_tls_cert_file, NOT the key path), mode 0644, delegate_to the
    Traefik_Guest, gated on BOTH `not (openbao_mock_edge_network|bool)` AND
    `openbao_tls_self_signed|bool` (R2.3/R2.6)."""
    copy_tasks = []
    for t in _tasks():
        spec = t.get("ansible.builtin.copy", t.get("copy"))
        if isinstance(spec, dict) and _delegated_to_traefik_guest(t):
            copy_tasks.append((t, spec))
    assert len(copy_tasks) == 1, (
        f"expected exactly one delegated copy of the mock CA cert onto the Traefik_Guest; "
        f"got {len(copy_tasks)}"
    )
    t, spec = copy_tasks[0]

    # Public cert only: the delegated content derives from the public cert (read
    # via a slurp of openbao_tls_cert_file), destined for the mock-CA dest path.
    assert str(spec.get("dest", "")).replace(" ", "") == "{{openbao_mock_ca_delegate_dest}}", (
        f"the delegated cert must land at openbao_mock_ca_delegate_dest; got {spec.get('dest')!r}"
    )
    assert str(spec.get("mode")) == "0644", (
        f"the delegated PUBLIC cert must be mode 0644 (public, non-secret); got {spec.get('mode')!r}"
    )

    # Gating: real path (NOT mock-edge) AND the self-signed mock-cert opt-in.
    when = t.get("when")
    assert isinstance(when, list), f"the delegate step must carry a list of when gates; got {when!r}"
    when_compact = [str(c).replace(" ", "") for c in when]
    assert any("not(openbao_mock_edge_network|bool)" in c for c in when_compact), (
        "the mock-CA delegate must be gated on NOT the mock-edge path (real path only, R2.6)"
    )
    assert any("openbao_tls_self_signed|bool" in c for c in when_compact), (
        "the mock-CA delegate must be gated on openbao_tls_self_signed (mock-cert path only, R2.3)"
    )


def test_no_delegated_task_copies_the_private_key_to_traefik_guest():
    """NO task in the role copies OpenBao's PRIVATE key (openbao_tls_key_file) to
    the Traefik_Guest — only the PUBLIC cert is ever delegated (R2.6 / NFR4)."""
    for t in _tasks():
        if not _delegated_to_traefik_guest(t):
            continue
        # Flatten every string value in the task to scan for the private-key var.
        blob = yaml.safe_dump(t)
        assert "openbao_tls_key_file" not in blob, (
            "a task delegated to the Traefik_Guest references the PRIVATE key "
            f"(openbao_tls_key_file) — the private key must NEVER be delegated: {t.get('name')!r}"
        )


def test_route_backend_root_cas_default_is_conditional_on_self_signed():
    """openbao_route_backend_root_cas defaults to the conditional: the mock dest
    when openbao_tls_self_signed else '' (empty in prod → no serversTransport,
    system trust store) — R2.3/R2.6."""
    d = _defaults()
    assert "openbao_route_backend_root_cas" in d, (
        "openbao_route_backend_root_cas must be declared in defaults"
    )
    expr = str(d["openbao_route_backend_root_cas"]).replace(" ", "")
    assert "openbao_mock_ca_delegate_dest" in expr, (
        "the default must use openbao_mock_ca_delegate_dest on the self-signed path"
    )
    assert "openbao_tls_self_signed|bool" in expr, (
        "the default must be conditional on openbao_tls_self_signed"
    )
    assert "else''" in expr, (
        "the default must resolve to EMPTY in production (no serversTransport, system trust store)"
    )


# =========================================================================== #
# (4) MOCK-EDGE PRESERVATION — default-off, retained with edge + scheme=https.
#     (R3.2)
# =========================================================================== #
def test_mock_edge_network_default_off_and_retained():
    """openbao_mock_edge_network remains declared and defaults FALSE — the
    permanent, default-off dev/offline affordance per mock-stand-ins.md (R3.2)."""
    d = _defaults()
    assert "openbao_mock_edge_network" in d, (
        "openbao_mock_edge_network must remain declared in defaults (not removed, R3.2)"
    )
    assert d["openbao_mock_edge_network"] is False, (
        "openbao_mock_edge_network must default FALSE — the REAL cross-guest file-provider "
        "route is the default posture; the mock-edge path is an opt-in dev/offline affordance (R3.2)"
    )


def test_mock_edge_labels_retained_with_https_scheme():
    """The mock-edge path keeps the Traefik discovery labels, including the
    backend scheme=https label, in a MOCK-EDGE-ONLY set_fact gated on
    openbao_mock_edge_network (R3.2)."""
    label_setfacts = []
    for t in _tasks():
        spec = t.get("ansible.builtin.set_fact", t.get("set_fact"))
        if isinstance(spec, dict) and "openbao_primary_labels" in spec:
            label_setfacts.append(t)
    assert len(label_setfacts) == 1, (
        f"expected exactly one set_fact composing openbao_primary_labels; got {len(label_setfacts)}"
    )
    t = label_setfacts[0]
    labels = (t.get("ansible.builtin.set_fact", t.get("set_fact")) or {})["openbao_primary_labels"]
    joined = "".join(str(x) for x in labels)
    assert "traefik.enable=true" in joined, "the mock-edge label set must retain traefik.enable"
    assert "loadbalancer.server.scheme=https" in joined, (
        "the mock-edge label set must retain the backend scheme=https label (R3.2)"
    )

    # Gated on the mock-edge path (labels are inert cross-guest).
    when = t.get("when")
    assert isinstance(when, list), f"the label set_fact must carry a list of when gates; got {when!r}"
    when_compact = [str(c).replace(" ", "") for c in when]
    assert any("openbao_mock_edge_network|bool" in c for c in when_compact), (
        "the mock-edge label set_fact must be gated on openbao_mock_edge_network (mock path only)"
    )
