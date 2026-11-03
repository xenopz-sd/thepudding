"""Offline structural tests for the openbao-agent cross-guest Traefik ingress.

Spec: .kiro/specs/fix-openbao-agent-cross-guest-edge-ingress/ — bugfix.md
(1.1-1.3, 2.1-2.4, 3.1-3.5); design.md "Testing Strategy" (exploratory bug
condition + fix checking + preservation checking).

THE BUG. The shared `openbao-agent` role deploys the workload Compose stack with
HARDCODED co-located-Traefik `edge`-network wiring: the workload service is
unconditionally attached to the external `edge` network, carries Traefik
discovery labels, and the stack declares `dca_networks: [{name: edge,
external: true}]`. On a DEDICATED workload guest (a separate LXC on the project
VLAN, not the Traefik guest) `docker compose up` then fails — "network edge
declared as external, but could not be found" — because `edge` is host-local to
the Traefik guest and Traefik on another guest cannot read this guest's labels.
This is the ADR-0018 cross-guest-ingress gap the sibling zitadel-server /
openbao_install / grafana roles already closed.

THE FIX (mirrors zitadel-server tasks 5a-5d + the reusable `traefik_route` role):
path-select the workload service's `networks`/`labels`/`ports` and the stack's
`dca_networks` on `openbao_agent_mock_edge_network` (default FALSE = real
cross-guest path), and delegate a Traefik file-provider Route_Fragment via
`traefik_route` on the real path (backend `http://<bind_ip>:<port>` — the
hello-workload serves cleartext HTTP, so NO serversTransport vars, the
default-inert path like grafana).

WHAT THIS MODULE ASSERTS (PyYAML parse of the role defaults + tasks — pure
structure, offline, no infra):

 * PROPERTY 1 (Bug Condition / Fix Checking): the workload wiring set_fact's
   `networks`/`labels` + the new `ports`, and the deploy task's `dca_networks`,
   are Jinja CONDITIONALS keyed on `openbao_agent_mock_edge_network` carrying
   BOTH branches (REAL = VLAN-IP-bound port + no edge + no labels; MOCK = edge +
   labels + no port); the role includes `traefik_route` once, gated on the real
   path, with an `http://` backend built from the bind-IP + HTTP-port vars and NO
   serversTransport vars; the flag defaults FALSE and the route-enabled flag TRUE;
   an INFO swap-gate debug task keyed on the flag exists.
 * PROPERTY 2 (Preservation): the swap-gate (`depends_on … service_healthy`), the
   RSF `extra_env_files required: false`, the no-secret `dca_env`, and the Agent
   sidecar (no published port) are UNCHANGED.

On the UNFIXED role the Property-1 tests FAIL (networks is a constant list, no
traefik_route include, flag absent); after the fix they PASS. The Property-2
tests PASS on both the unfixed and fixed role (preservation baseline).

The task-dict helpers are LOCAL copies (the conftest-collision rule forbids a
cross-tree import), mirroring tests/installer/test_svc06_cross_guest.py. No
secret VALUE appears anywhere.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_openbao_agent_cross_guest.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROLE = _REPO_ROOT / "ansible" / "roles" / "openbao-agent"
_AGENT_DEFAULTS = _AGENT_ROLE / "defaults" / "main.yml"
_AGENT_TASKS = _AGENT_ROLE / "tasks" / "main.yml"


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
    raw = yaml.safe_load(_AGENT_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw, "openbao-agent defaults must be a non-empty mapping"
    return raw


def _tasks() -> list[dict]:
    return _load_task_file(_AGENT_TASKS)


def _find_set_fact(tasks: list[dict], key: str) -> dict:
    """Return the `set_fact` payload that defines ``key`` (raw, unresolved)."""
    for task in tasks:
        sf = task.get("ansible.builtin.set_fact") or task.get("set_fact")
        if isinstance(sf, dict) and key in sf:
            return sf
    raise AssertionError(f"no set_fact defining {key!r} found")


def _workload_wiring() -> dict:
    """The FIXED swap-gate workload wiring dict (networks/labels/ports/extra_env_files/
    depends_on) the role builds in the openbao_agent_workload_service_wiring set_fact."""
    sf = _find_set_fact(_tasks(), "openbao_agent_workload_service_wiring")
    return sf["openbao_agent_workload_service_wiring"]


def _dca_deploy_task() -> dict:
    """The include_role: docker-compose-app task that deploys the Agent + workload
    stack (dca_services carries the openbao-agent sidecar)."""
    deploys = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if not (isinstance(spec, dict) and spec.get("name") == "docker-compose-app"):
            continue
        deploys.append(t)
    assert len(deploys) == 1, (
        f"expected exactly one docker-compose-app deploy task in openbao-agent "
        f"main.yml; got {len(deploys)}"
    )
    return deploys[0]


def _traefik_route_includes() -> list[dict]:
    out = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if isinstance(spec, dict) and spec.get("name") == "traefik_route":
            out.append(t)
    return out


# =========================================================================== #
# PROPERTY 1 — BUG CONDITION / FIX CHECKING
# =========================================================================== #


def test_mock_edge_flag_defaults_false():
    """2.1/2.4: openbao_agent_mock_edge_network is declared and defaults FALSE —
    the REAL cross-guest path is the default production posture; the mock-edge
    path is an opt-in dev/offline affordance (mock-stand-ins.md).

    FAILS on the UNFIXED role (the flag does not exist)."""
    d = _defaults()
    assert "openbao_agent_mock_edge_network" in d, (
        "openbao_agent_mock_edge_network must be declared in openbao-agent defaults "
        "(the real-vs-mock-edge swap-gate flag, mirroring zitadel_mock_edge_network)"
    )
    assert d["openbao_agent_mock_edge_network"] is False, (
        "openbao_agent_mock_edge_network must default FALSE — the REAL cross-guest "
        "file-provider route is the default posture (2.1)"
    )


def test_route_enabled_defaults_true():
    """2.2: openbao_agent_traefik_route_enabled defaults TRUE — the real
    Route_Fragment is delegated onto the Traefik_Guest by default.

    FAILS on the UNFIXED role (the flag does not exist)."""
    d = _defaults()
    assert "openbao_agent_traefik_route_enabled" in d, (
        "openbao_agent_traefik_route_enabled must be declared in defaults"
    )
    assert d["openbao_agent_traefik_route_enabled"] is True, (
        "openbao_agent_traefik_route_enabled must default TRUE (fed to "
        "traefik_route_enabled, the real-route default posture)"
    )


def test_bind_ip_and_http_port_defaults_present():
    """2.1: the real-path published port needs a bind IP (the guest's OWN VLAN IP,
    never 0.0.0.0) and an HTTP port. Mirrors zitadel_bind_ip / zitadel_http_port.

    FAILS on the UNFIXED role (neither var exists)."""
    d = _defaults()
    assert "openbao_agent_workload_bind_ip" in d, (
        "openbao_agent_workload_bind_ip must be declared (the guest's own VLAN IP "
        "for the real-path published port — e.g. {{ ansible_host }})"
    )
    bind = str(d["openbao_agent_workload_bind_ip"])
    assert "0.0.0.0" not in bind, (
        "openbao_agent_workload_bind_ip must NOT be 0.0.0.0/LAN (it binds the "
        "guest's own VLAN IP; the default references ansible_host)"
    )
    assert "openbao_agent_workload_http_port" in d, (
        "openbao_agent_workload_http_port must be declared (the workload HTTP port "
        "published + used in the route backend)"
    )


def test_traefik_guest_host_default_present():
    """2.2: the delegate target (the Traefik_Guest, ADR-0015 10.0.20.12) must be
    a declared default fed to the traefik_route role.

    FAILS on the UNFIXED role (the var does not exist)."""
    d = _defaults()
    assert "openbao_agent_traefik_guest_host" in d, (
        "openbao_agent_traefik_guest_host must be declared (the Traefik_Guest "
        "delegate target for the file-provider Route_Fragment)"
    )


def test_workload_networks_conditional_edge_mock_only():
    """2.1/2.3: the workload service `networks` is a Jinja conditional on
    openbao_agent_mock_edge_network — the `edge` network applies ONLY on the mock
    path and resolves empty on the real path (both branches present).

    FAILS on the UNFIXED role (networks is the constant list
    ["{{ openbao_agent_edge_network }}"], no conditional)."""
    wiring = _workload_wiring()
    networks = wiring.get("networks")
    assert isinstance(networks, str) and "{{" in networks, (
        f"workload `networks` must be a Jinja conditional expression keyed on the "
        f"mock-edge flag; got {networks!r} (hardcoded list = the bug)"
    )
    compact = networks.replace(" ", "")
    assert "openbao_agent_mock_edge_network" in compact, (
        "`networks` must be conditional on openbao_agent_mock_edge_network"
    )
    assert "openbao_agent_edge_network" in compact, (
        "`networks` mock branch must reference openbao_agent_edge_network (the edge net)"
    )
    assert "[]" in compact, "`networks` real-path branch must resolve to [] (no cross-guest edge)"
    assert "else" in compact, "`networks` must carry BOTH branches (if/else keyed on the flag)"


def test_workload_labels_conditional_mock_only():
    """2.1/2.3: the workload `labels` is a Jinja conditional — the Traefik
    discovery labels apply ONLY on the mock path, empty on the real path.

    FAILS on the UNFIXED role (labels is a hardcoded two-entry list)."""
    wiring = _workload_wiring()
    labels = wiring.get("labels")
    assert isinstance(labels, str) and "{{" in labels, (
        f"workload `labels` must be a Jinja conditional keyed on the mock-edge flag; "
        f"got {labels!r} (hardcoded Traefik labels = the bug)"
    )
    compact = labels.replace(" ", "")
    assert "openbao_agent_mock_edge_network" in compact, (
        "`labels` must be conditional on openbao_agent_mock_edge_network"
    )
    assert "[]" in compact, "`labels` real-path branch must resolve to [] (no Traefik labels)"


def test_workload_ports_conditional_binds_vlan_ip_not_wildcard():
    """2.1: the workload `ports` is a Jinja conditional — the real branch publishes
    the HTTP port bound to the guest's OWN VLAN IP (openbao_agent_workload_bind_ip,
    never 0.0.0.0), the mock branch publishes nothing ([]).

    FAILS on the UNFIXED role (there is NO ports key at all — the workload
    publishes no port and relies solely on the co-located edge/labels)."""
    wiring = _workload_wiring()
    ports = wiring.get("ports")
    assert ports is not None, (
        "the FIXED workload wiring must carry a `ports` key (the real-path "
        "VLAN-IP-bound published port); the unfixed role has none (the bug)"
    )
    assert isinstance(ports, str) and "{{" in ports, (
        f"`ports` must be a Jinja conditional keyed on the mock-edge flag; got {ports!r}"
    )
    compact = ports.replace(" ", "")
    assert "openbao_agent_mock_edge_network" in compact, (
        "`ports` must be conditional on openbao_agent_mock_edge_network"
    )
    assert "openbao_agent_workload_bind_ip" in compact, (
        "the real-path port mapping must bind the guest's OWN VLAN IP via "
        "openbao_agent_workload_bind_ip (never a bare host-wide port)"
    )
    assert "openbao_agent_workload_http_port" in compact, (
        "the real-path port mapping must publish openbao_agent_workload_http_port"
    )
    assert "0.0.0.0" not in compact, "the workload port must NEVER bind 0.0.0.0 (2.1)"
    assert "[]" in compact, "`ports` mock-edge branch must resolve to [] (no published port)"


def test_dca_networks_conditional_external_edge_mock_only():
    """2.1/2.3: the stack `dca_networks` is a Jinja conditional — the external
    `edge` dependency is declared ONLY on the mock path, empty on the real path.

    FAILS on the UNFIXED role (dca_networks is the constant
    [{name: edge, external: true}])."""
    deploy = _dca_deploy_task()
    tvars = deploy.get("vars") or {}
    dca_networks = tvars.get("dca_networks")
    assert isinstance(dca_networks, str) and "{{" in dca_networks, (
        f"`dca_networks` must be a Jinja conditional keyed on the mock-edge flag; "
        f"got {dca_networks!r} (hardcoded external edge = the bug)"
    )
    compact = dca_networks.replace(" ", "")
    assert "openbao_agent_mock_edge_network" in compact, (
        "`dca_networks` must be conditional on openbao_agent_mock_edge_network"
    )
    assert "openbao_agent_edge_network" in compact and "external" in compact, (
        "`dca_networks` mock branch must declare the external `edge` dependency"
    )
    assert "[]" in compact, "`dca_networks` real-path branch must resolve to [] (no external edge)"


def test_traefik_route_include_real_path_http_backend_no_servers_transport():
    """2.2: the role includes traefik_route exactly once, gated on the real path,
    with route_name + route_host_rule (openbao_agent_workload_host_rule) + an
    http:// backend built from the bind-IP + HTTP-port vars, and NO
    serversTransport vars (the cleartext-HTTP default-inert path, like grafana).

    FAILS on the UNFIXED role (no traefik_route include exists at all)."""
    includes = _traefik_route_includes()
    assert len(includes) == 1, (
        f"openbao-agent main.yml must include the traefik_route role exactly once "
        f"(the real-path cross-guest Route_Fragment); got {len(includes)}"
    )
    t = includes[0]
    tvars = t.get("vars") or {}

    assert str(tvars.get("route_name", "")).strip() != "", "route_name must be set"
    assert "openbao_agent_workload_host_rule" in str(tvars.get("route_host_rule", "")), (
        f"route_host_rule must be openbao_agent_workload_host_rule; "
        f"got {tvars.get('route_host_rule')!r}"
    )

    backend = str(tvars.get("route_backend_url", ""))
    assert backend.strip().startswith("http://"), (
        f"route_backend_url must be a plain http:// VLAN url (hello-workload serves "
        f"cleartext HTTP, not h2c); got {backend!r}"
    )
    assert "openbao_agent_workload_bind_ip" in backend and "openbao_agent_workload_http_port" in backend, (
        "route_backend_url must build the backend from openbao_agent_workload_bind_ip "
        f"+ openbao_agent_workload_http_port; got {backend!r}"
    )

    # default-inert: NEITHER serversTransport var is passed (cleartext hop, no cert).
    assert "route_servers_transport_name" not in tvars, (
        "the openbao-agent traefik_route call must NOT pass route_servers_transport_name "
        "(cleartext HTTP hop — no backend cert to verify)"
    )
    assert "route_backend_root_cas" not in tvars, (
        "the openbao-agent traefik_route call must NOT pass route_backend_root_cas "
        "(cleartext HTTP hop — no CA material)"
    )

    when = str(t.get("when", "")).replace(" ", "")
    assert "not(openbao_agent_mock_edge_network|bool)" in when, (
        "the traefik_route include must be gated `when: not "
        f"(openbao_agent_mock_edge_network | bool)` (real path only); got when={t.get('when')!r}"
    )

    # the swap-gate is fed from openbao_agent_traefik_route_enabled.
    te = str(tvars.get("traefik_route_enabled", ""))
    assert "openbao_agent_traefik_route_enabled" in te, (
        f"traefik_route_enabled must be fed from openbao_agent_traefik_route_enabled; got {te!r}"
    )


def test_info_swap_gate_debug_task_keyed_on_mock_edge():
    """2.4: an INFO swap-gate `debug` task states which ingress path was selected
    and is keyed on openbao_agent_mock_edge_network (composition-wiring.md).

    FAILS on the UNFIXED role (no such debug task)."""
    for t in _tasks():
        spec = t.get("ansible.builtin.debug", t.get("debug"))
        if not isinstance(spec, dict):
            continue
        msg = str(spec.get("msg", ""))
        if "openbao_agent_mock_edge_network" in msg and (
            "swap-gate" in msg or "MOCK_EDGE" in msg or "mock-edge" in msg
        ):
            return
    raise AssertionError(
        "expected an INFO swap-gate debug task whose msg is keyed on "
        "openbao_agent_mock_edge_network and states the selected ingress path (2.4)"
    )


# =========================================================================== #
# PROPERTY 2 — PRESERVATION (passes on BOTH unfixed and fixed role)
# =========================================================================== #


def test_preserved_swap_gate_depends_on_service_healthy():
    """3.1: the workload still depends_on the Agent sidecar `condition:
    service_healthy` (the fail-closed swap-gate) — unchanged by the fix."""
    wiring = _workload_wiring()
    depends = wiring.get("depends_on") or {}
    assert "openbao-agent" in depends, f"workload must depend_on openbao-agent; got {depends!r}"
    cond = depends["openbao-agent"]
    cond_value = cond.get("condition") if isinstance(cond, dict) else cond
    assert cond_value == "service_healthy", (
        f"the swap-gate must remain `condition: service_healthy`; got {cond_value!r}"
    )


def test_preserved_rsf_extra_env_files_required_false():
    """3.2: the workload still layers the RSF via extra_env_files long-form
    `required: false` — unchanged by the fix."""
    wiring = _workload_wiring()
    extra = wiring.get("extra_env_files")
    assert isinstance(extra, list) and extra, f"extra_env_files must be present; got {extra!r}"
    rsf = next(
        (e for e in extra if isinstance(e, dict) and "rendered_secret_file" in str(e.get("path", ""))),
        None,
    )
    assert rsf is not None, f"the RSF must be the extra_env_files source; got {extra!r}"
    assert rsf.get("required") is False, (
        f"the RSF entry must remain long-form `required: false`; got {rsf!r}"
    )


def test_preserved_dca_env_is_nonsecret_workload_env():
    """3.3: dca_env remains the non-secret openbao_agent_workload_env (no secret
    value in the compose .env) — unchanged by the fix."""
    deploy = _dca_deploy_task()
    tvars = deploy.get("vars") or {}
    assert "openbao_agent_workload_env" in str(tvars.get("dca_env", "")), (
        f"dca_env must remain openbao_agent_workload_env (non-secret); got {tvars.get('dca_env')!r}"
    )


def test_preserved_agent_sidecar_has_no_published_port():
    """3.4: the Agent sidecar service in dca_services keeps NO published host
    port (it speaks only to the Primary and writes a local file) — unchanged."""
    deploy = _dca_deploy_task()
    tvars = deploy.get("vars") or {}
    services = tvars.get("dca_services") or []
    sidecar = next(
        (s for s in services if isinstance(s, dict) and s.get("name") == "openbao-agent"),
        None,
    )
    assert sidecar is not None, "the openbao-agent sidecar service must be present in dca_services"
    assert "ports" not in sidecar, (
        f"the Agent sidecar must publish NO host port; got {sidecar.get('ports')!r}"
    )


def test_preserved_no_log_posture():
    """3.3: openbao_no_log defaults closed (openbao_dev_expose_secrets=false) and
    no task hardcodes no_log off — the secret-hygiene posture is unchanged."""
    d = _defaults()
    assert "openbao_no_log" in d, "openbao_no_log must remain declared"
    assert d.get("openbao_dev_expose_secrets") is False, (
        "openbao_dev_expose_secrets must default False so openbao_no_log is True"
    )
    for t in _tasks():
        if "no_log" in t:
            assert t["no_log"] not in (False, "false", "no"), (
                f"a task hardcodes no_log off: {t.get('name')!r} — must never be weakened"
            )


# =========================================================================== #
# WORKLOAD RSF MOUNT BUGFIX
# Spec: .kiro/specs/fix-openbao-agent-workload-rsf-mount/ — bugfix.md
# (1.1-1.2, 2.1-2.2, 3.1-3.5); design.md "Testing Strategy".
#
# THE BUG. The workload service references the Rendered_Secret_File (RSF) via
# `extra_env_files` at the IN-CONTAINER path openbao_agent_rendered_secret_file
# (under openbao_agent_rendered_dir), but only the AGENT sidecar mounts the host
# dir backing that path (openbao_agent_host_rendered_dir ->
# openbao_agent_rendered_dir, read-write). The WORKLOAD has NO such mount, so the
# RSF path does not exist inside the workload container, Compose silently skips
# the `required: false` env_file, and the rendered secrets (e.g. DATABASE_URL)
# never reach the workload env — the workload never becomes healthy.
#
# THE FIX (Option A). The FIXED wiring (openbao_agent_workload_service_wiring)
# OWNS the workload `volumes` = the RSF read-only mount
# (openbao_agent_host_rendered_dir:openbao_agent_rendered_dir:ro) CONCATENATED
# with the caller's optional openbao_agent_workload_volumes; the `volumes` fold is
# DROPPED from the optional set. Wiring wins (folded last) so the RSF mount is
# always present; concatenation preserves any caller volume.
#
# PROPERTY 1 (Bug Condition / Fix Checking): the workload wiring carries a
#   `volumes` key with a read-only mount of the shared rendered dir, and the RSF
#   env_file path is UNDER the mounted rendered dir. FAILS on the unfixed role
#   (no `volumes` key on the wiring); PASSES after the fix.
# PROPERTY 2 (Preservation): the wiring `volumes` expression CONCATENATES the
#   caller's openbao_agent_workload_volumes (ADDED, not substituted), and the
#   Agent sidecar keeps its read-WRITE rendered-dir mount — unchanged.
# =========================================================================== #


def _agent_sidecar_service() -> dict:
    """The openbao-agent sidecar service dict from the deploy task's dca_services."""
    deploy = _dca_deploy_task()
    tvars = deploy.get("vars") or {}
    services = tvars.get("dca_services") or []
    sidecar = next(
        (s for s in services if isinstance(s, dict) and s.get("name") == "openbao-agent"),
        None,
    )
    assert sidecar is not None, "the openbao-agent sidecar service must be present in dca_services"
    return sidecar


# --------------------------------------------------------------------------- #
# PROPERTY 1 — BUG CONDITION / FIX CHECKING
# --------------------------------------------------------------------------- #


def test_workload_mounts_shared_rendered_dir_readonly():
    """2.1 (Property 1): the FIXED workload wiring carries a `volumes` key that
    mounts the shared rendered dir READ-ONLY — the RSF-sharing mount is present on
    the WORKLOAD (not only the Agent), so the RSF in-container path resolves.

    FAILS on the UNFIXED role: the workload wiring has NO `volumes` key at all
    (its only volumes came from the optional set), so the RSF path is unreachable
    inside the workload container (the "DATABASE_URL absent" counterexample)."""
    wiring = _workload_wiring()
    volumes = wiring.get("volumes")
    assert volumes is not None, (
        "the FIXED workload wiring must carry a `volumes` key mounting the shared "
        "rendered dir; the unfixed wiring has none, so the RSF path is unreachable "
        "inside the workload container (the bug)"
    )
    # Normalize to a comparable string (wiring volumes is a Jinja expression str).
    blob = volumes if isinstance(volumes, str) else " ".join(map(str, volumes))
    compact = blob.replace(" ", "")
    assert "openbao_agent_host_rendered_dir" in compact, (
        f"the workload `volumes` must mount openbao_agent_host_rendered_dir "
        f"(the host dir backing the RSF); got {volumes!r}"
    )
    assert "openbao_agent_rendered_dir" in compact, (
        f"the workload `volumes` must mount onto openbao_agent_rendered_dir "
        f"(the in-container path the RSF env_file references); got {volumes!r}"
    )
    assert ":ro" in compact, (
        f"the workload's rendered-dir mount must be READ-ONLY (:ro) — the workload "
        f"only reads the RSF; got {volumes!r}"
    )


def test_rsf_env_file_path_is_under_mounted_rendered_dir():
    """2.2 (Property 1): path-consistency — the RSF the workload references via
    extra_env_files (openbao_agent_rendered_secret_file) is defined UNDER
    openbao_agent_rendered_dir, so it is reachable via the read-only rendered-dir
    mount asserted above.

    PASSES on both unfixed and fixed role (the defaults are unchanged); together
    with the mount assertion it proves the fix makes the RSF reachable."""
    d = _defaults()
    rsf = str(d.get("openbao_agent_rendered_secret_file", ""))
    assert "openbao_agent_rendered_dir" in rsf.replace(" ", ""), (
        f"openbao_agent_rendered_secret_file must be defined UNDER "
        f"openbao_agent_rendered_dir so it is reachable via the rendered-dir mount; "
        f"got {rsf!r}"
    )
    # The workload wiring still references that RSF var via extra_env_files.
    wiring = _workload_wiring()
    extra = wiring.get("extra_env_files") or []
    rsf_entry = next(
        (e for e in extra if isinstance(e, dict) and "rendered_secret_file" in str(e.get("path", ""))),
        None,
    )
    assert rsf_entry is not None, (
        f"the workload must still reference openbao_agent_rendered_secret_file via "
        f"extra_env_files; got {extra!r}"
    )


# --------------------------------------------------------------------------- #
# PROPERTY 2 — PRESERVATION
# --------------------------------------------------------------------------- #


def test_workload_volumes_concatenate_caller_optional_volumes():
    """3.1/3.2 (Property 2): the FIXED wiring `volumes` expression CONCATENATES
    openbao_agent_workload_volumes — a caller-supplied volume (e.g. the
    hello-workload read-only entrypoint mount) is PRESERVED alongside the RSF mount
    (ADDED, not substituted). A caller passing none gets exactly the RSF mount.

    FAILS on the UNFIXED role (the wiring has no `volumes` key); PASSES after the
    fix. Recorded under preservation because it guards volume preservation."""
    wiring = _workload_wiring()
    volumes = wiring.get("volumes")
    assert volumes is not None, (
        "the FIXED workload wiring must own `volumes` (RSF mount + caller volumes)"
    )
    blob = volumes if isinstance(volumes, str) else " ".join(map(str, volumes))
    compact = blob.replace(" ", "")
    assert "openbao_agent_workload_volumes" in compact, (
        f"the workload `volumes` must CONCATENATE openbao_agent_workload_volumes so "
        f"caller-supplied mounts are preserved (ADDED, not substituted); got {volumes!r}"
    )
    # Concatenation form: a '+' joins the RSF mount list with the caller list.
    assert "+" in compact, (
        f"the workload `volumes` must list-CONCATENATE the RSF mount with "
        f"openbao_agent_workload_volumes (not replace); got {volumes!r}"
    )


def test_preserved_optional_set_no_longer_owns_volumes():
    """3.2 (Property 2): the OPTIONAL set (openbao_agent_workload_service_optional)
    no longer folds a `volumes` key — the fixed wiring owns the full volumes list.
    This prevents the combine() overwrite that would drop one of them.

    FAILS on the UNFIXED role (the optional set still folds volumes); PASSES after
    the fix. Documents the Option-A ownership move."""
    sf = _find_set_fact(_tasks(), "openbao_agent_workload_service_optional")
    expr = str(sf["openbao_agent_workload_service_optional"]).replace(" ", "")
    assert "'volumes':" not in expr and "\"volumes\":" not in expr, (
        "the optional set must NOT fold a 'volumes' key anymore — the fixed wiring "
        f"owns the workload volumes (Option A); got {sf['openbao_agent_workload_service_optional']!r}"
    )


def test_preserved_agent_sidecar_rendered_dir_mount_is_read_write():
    """3.3 (Property 2): the Agent sidecar STILL mounts the shared rendered dir
    READ-WRITE (host_rendered_dir -> rendered_dir, no :ro) — the Agent writes the
    RSF, so its mount must stay writable. Unchanged by the fix.

    PASSES on both unfixed and fixed role (preservation baseline)."""
    sidecar = _agent_sidecar_service()
    vols = sidecar.get("volumes") or []
    rendered_mount = next(
        (
            v
            for v in vols
            if isinstance(v, str)
            and "openbao_agent_host_rendered_dir" in v.replace(" ", "")
            and "openbao_agent_rendered_dir" in v.replace(" ", "")
        ),
        None,
    )
    assert rendered_mount is not None, (
        f"the Agent sidecar must still mount the shared rendered dir; got {vols!r}"
    )
    assert not rendered_mount.replace(" ", "").endswith(":ro"), (
        f"the Agent sidecar's rendered-dir mount must remain READ-WRITE (it writes "
        f"the RSF); got {rendered_mount!r}"
    )


# =========================================================================== #
# WORKLOAD env_file HOST-PATH BUGFIX
# Spec: .kiro/specs/fix-openbao-agent-env-file-host-path/ — bugfix.md
# (1.1-1.2, 2.1-2.2, 3.1-3.4); design.md "Testing Strategy".
#
# THE BUG. The workload references the Rendered_Secret_File (RSF) via
# `extra_env_files` at the IN-CONTAINER path openbao_agent_rendered_secret_file
# (under openbao_agent_rendered_dir). But Docker Compose resolves `env_file`
# entries on the HOST filesystem at `up`-time — NOT from inside the container.
# The host has no file at that in-container path, so Compose silently skips the
# `required: false` entry and injects no secrets (e.g. DATABASE_URL never reaches
# the workload env → /healthz marker absent → 503, never healthy).
#
# THE FIX. Introduce the HOST-RSF var openbao_agent_host_rendered_secret_file
# (= openbao_agent_host_rendered_dir/secrets.env — the host file the Agent
# actually writes) and point the workload's `extra_env_files` path at THAT host
# var instead of the in-container openbao_agent_rendered_secret_file. The Agent's
# own in-container paths (its `test -s` healthcheck probe and the agent.hcl
# destination) correctly stay the in-container openbao_agent_rendered_secret_file.
#
# PROPERTY 1 (Bug Condition / Fix Checking): the workload `extra_env_files` path
#   references the HOST var (openbao_agent_host_rendered_secret_file /
#   openbao_agent_host_rendered_dir), NOT the in-container
#   openbao_agent_rendered_secret_file/openbao_agent_rendered_dir; the host var is
#   declared and equals openbao_agent_host_rendered_dir/secrets.env; path-
#   consistency (agent-written host path == compose-read host path). FAILS on the
#   unfixed role (path is in-container; var undefined); PASSES after the fix.
# PROPERTY 2 (Preservation): the Agent sidecar healthcheck still probes the
#   IN-CONTAINER openbao_agent_rendered_secret_file — unchanged.
# =========================================================================== #


# --------------------------------------------------------------------------- #
# PROPERTY 1 — BUG CONDITION / FIX CHECKING
# --------------------------------------------------------------------------- #


def test_workload_env_file_references_host_rsf_path():
    """2.1 (Property 1): the workload `extra_env_files` RSF entry's `path`
    references the HOST RSF var (openbao_agent_host_rendered_secret_file /
    openbao_agent_host_rendered_dir) — the path Docker Compose reads host-side —
    and NOT the in-container openbao_agent_rendered_dir path.

    FAILS on the UNFIXED role: the path is `{{ openbao_agent_rendered_secret_file }}`
    (under openbao_agent_rendered_dir, the in-container dir), which does not exist
    on the host, so Compose silently skips the `required: false` env_file (the
    "DATABASE_URL absent" counterexample)."""
    wiring = _workload_wiring()
    extra = wiring.get("extra_env_files") or []
    rsf_entry = next(
        (e for e in extra if isinstance(e, dict) and "rendered_secret_file" in str(e.get("path", ""))),
        None,
    )
    assert rsf_entry is not None, (
        f"the workload must reference the RSF via extra_env_files; got {extra!r}"
    )
    path = str(rsf_entry.get("path", "")).replace(" ", "")
    assert "openbao_agent_host_rendered_secret_file" in path, (
        f"the workload extra_env_files path must be the HOST RSF var "
        f"openbao_agent_host_rendered_secret_file (Compose reads env_file HOST-side); "
        f"got {rsf_entry.get('path')!r} (the in-container path is the bug)"
    )
    # Guard against regressing to the in-container var name exactly. The host var
    # name legitimately CONTAINS 'rendered_secret_file' as a substring, so compare
    # against the exact in-container var token with its surrounding braces.
    assert "{{openbao_agent_rendered_secret_file}}" not in path, (
        f"the workload extra_env_files path must NOT be the in-container "
        f"openbao_agent_rendered_secret_file (Compose cannot resolve it host-side); "
        f"got {rsf_entry.get('path')!r}"
    )


def test_host_rsf_var_declared_and_equals_host_dir_secrets_env():
    """2.1 (Property 1): the defaults declare openbao_agent_host_rendered_secret_file
    and it equals `{{ openbao_agent_host_rendered_dir }}/secrets.env` — the mirror
    of the in-container RSF under the host dir.

    FAILS on the UNFIXED role (the var does not exist)."""
    d = _defaults()
    assert "openbao_agent_host_rendered_secret_file" in d, (
        "openbao_agent_host_rendered_secret_file must be declared in openbao-agent "
        "defaults (the HOST path the workload's env_file references)"
    )
    val = str(d["openbao_agent_host_rendered_secret_file"]).replace(" ", "")
    assert "openbao_agent_host_rendered_dir" in val and val.endswith("/secrets.env"), (
        f"openbao_agent_host_rendered_secret_file must equal "
        f"'{{{{ openbao_agent_host_rendered_dir }}}}/secrets.env'; got "
        f"{d['openbao_agent_host_rendered_secret_file']!r}"
    )


def test_path_consistency_agent_written_equals_compose_read():
    """2.2 (Property 1): path-consistency — the host path Compose reads
    (openbao_agent_host_rendered_secret_file = host_rendered_dir/secrets.env) is
    the SAME host file the Agent writes. The Agent's agent.hcl destination is the
    in-container openbao_agent_rendered_secret_file (= rendered_dir/secrets.env),
    and the Agent mounts host_rendered_dir onto rendered_dir — so both resolve to
    host_rendered_dir/secrets.env on the host (staged-by-agent == read-by-compose).

    FAILS on the UNFIXED role via the missing host var (test above)."""
    d = _defaults()
    host_rsf = str(d.get("openbao_agent_host_rendered_secret_file", "")).replace(" ", "")
    in_rsf = str(d.get("openbao_agent_rendered_secret_file", "")).replace(" ", "")
    # Both filenames are secrets.env, one under the host dir, one under the
    # in-container dir which the Agent mounts FROM the host dir.
    assert host_rsf.endswith("/secrets.env"), f"host RSF must end /secrets.env; got {host_rsf!r}"
    assert in_rsf.endswith("/secrets.env"), f"in-container RSF must end /secrets.env; got {in_rsf!r}"
    assert "openbao_agent_host_rendered_dir" in host_rsf, (
        f"host RSF must be under openbao_agent_host_rendered_dir; got {host_rsf!r}"
    )
    assert "openbao_agent_rendered_dir" in in_rsf, (
        f"in-container RSF must be under openbao_agent_rendered_dir; got {in_rsf!r}"
    )
    # The Agent sidecar mounts host_rendered_dir -> rendered_dir (read-write), so
    # rendered_dir/secrets.env (what the Agent writes) == host_rendered_dir/secrets.env
    # (what Compose reads). Assert that RW mount exists to close the consistency loop.
    sidecar = _agent_sidecar_service()
    vols = sidecar.get("volumes") or []
    rw_mount = next(
        (
            v
            for v in vols
            if isinstance(v, str)
            and "openbao_agent_host_rendered_dir" in v.replace(" ", "")
            and "openbao_agent_rendered_dir" in v.replace(" ", "")
            and not v.replace(" ", "").endswith(":ro")
        ),
        None,
    )
    assert rw_mount is not None, (
        f"the Agent sidecar must mount host_rendered_dir -> rendered_dir read-write "
        f"(so the file it writes at the in-container destination IS the host file "
        f"Compose reads); got {vols!r}"
    )


# --------------------------------------------------------------------------- #
# PROPERTY 2 — PRESERVATION
# --------------------------------------------------------------------------- #


def test_preserved_agent_healthcheck_probes_in_container_rsf_path():
    """3.1/3.2 (Property 2): the Agent sidecar RSF-presence healthcheck still
    probes the IN-CONTAINER openbao_agent_rendered_secret_file (`test -s …` runs
    inside the Agent container — correct and unchanged by the host-path fix).

    PASSES on both unfixed and fixed role (preservation baseline)."""
    sidecar = _agent_sidecar_service()
    hc = sidecar.get("healthcheck") or {}
    test = hc.get("test")
    blob = " ".join(map(str, test)) if isinstance(test, list) else str(test)
    compact = blob.replace(" ", "")
    assert "openbao_agent_rendered_secret_file" in compact, (
        f"the Agent healthcheck must probe the IN-CONTAINER "
        f"openbao_agent_rendered_secret_file (it runs inside the Agent container); "
        f"got {test!r}"
    )
    assert "openbao_agent_host_rendered_secret_file" not in compact, (
        f"the Agent healthcheck must NOT use the HOST var — it runs in-container; "
        f"got {test!r}"
    )
