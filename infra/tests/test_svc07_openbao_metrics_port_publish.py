"""Offline bug-condition exploration + primary-`ports`-contract test for the
SVC-07 OpenBao metrics-port-publish bugfix.

Spec: .kiro/specs/fix-svc07-openbao-metrics-port-publish/
      (bugfix.md, design.md, tasks.md).

Background: the merged `fix-obs-ac3-targets-up` bugfix closed 3/4 of the
SVC-17/18 AC-3 V1_Scrape_Set (SVC-01 postgres / SVC-09 Traefik / SVC-06 ZITADEL
at `up == 1`) and removed the SVC-07 OpenBao scrape TLS-trust blocker — the
OpenBao scrape error moved `x509: certificate signed by unknown authority` ->
`connection refused on 10.0.20.10:8200`, proving TLS trust is resolved and that
the lone remaining half is pure transport reachability. OpenBao stayed `up == 0`
for a distinct reason: the OpenBao primary container does not host-publish its
`:8200` metrics/API port.

Root cause (confirmed via `docker inspect` live + a code trace offline): the
primary `- name: openbao` service in
`ansible/roles/openbao_install/tasks/main.yml` ("Build the PRIMARY OpenBao
Compose stack" docker-compose-app include) makes its `ports` a PATH-SELECTED
folded-Jinja expression keyed on `openbao_mock_edge_network`:
`ports: >- {{ [] if (openbao_mock_edge_network | bool) else [openbao_primary_ip ~ ':' ~ (openbao_api_port|string) ~ ':' ~ (openbao_api_port|string)] }}`
So the mock-edge branch yields `[]` (publish NOTHING). `svc07_bootstrap`
correctly deploys the primary with `openbao_mock_edge_network=true` at SVC-07
Day-0 (SVC-09/Traefik does not exist yet, so the primary needs the placeholder
`edge` network). The mock-edge path's side effect — `ports: []` — means `:8200`
is never host-published and the DIRECT VLAN-20 Prometheus scrape of
`10.0.20.10:8200` gets `connection refused` -> OpenBao `up == 0`.

The fix (Option A, operator-approved): decouple the `:8200` publish from the
edge path — make the primary UNCONDITIONALLY publish
`{{ openbao_primary_ip }}:{{ openbao_api_port }}:{{ openbao_api_port }}` on BOTH
paths (an internal VLAN-20 bind to the primary's own VLAN-20 IP, never
`0.0.0.0`/LAN), mirroring the SVC-07 unsealer's existing unconditional static
`ports`. ONLY the host-port publish decouples; the `networks` / `labels` /
`dca_networks` path-selection on `openbao_mock_edge_network` stays UNCHANGED.

This module encodes the FIXED primary-`ports` contract (Property 1) so the fix
is validated when it passes after implementation, and the regression cannot
silently return. On the UNFIXED tree the assertion FAILS — the primary `ports`
IS keyed on `openbao_mock_edge_network` (the conditional yields `[]` on the
mock-edge path) — and that failure CONFIRMS the bug (bug-condition exploration
test).

Pure PyYAML + text parse; no ansible-playbook binary, no live infra. Mirrors the
task-walking helpers (`_iter_tasks` / `_load_tasks` / `_dca_services_vars`) of
the sibling tests infra/tests/test_obs_ac3_targets_up.py and
infra/tests/test_obs_scrape_multi_inventory.py.

Run:  set +H
      ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_metrics_port_publish.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_OPENBAO_TASKS = _ROLES / "openbao_install" / "tasks" / "main.yml"

# The two role vars the primary VLAN20_Port publish must reference.
_PRIMARY_IP_VAR = "openbao_primary_ip"
_API_PORT_VAR = "openbao_api_port"
# The edge-path flag the primary `ports` must NOT be keyed on (the defect).
_MOCK_EDGE_FLAG = "openbao_mock_edge_network"


# ---------------------------------------------------------------------------
# Shared task-walking helpers (mirror the sibling obs tests)
# ---------------------------------------------------------------------------

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


def _dca_services_vars(tasks: list[dict]) -> list[dict]:
    """Every `dca_services` list entry found in the `vars` of a
    docker-compose-app include task."""
    out: list[dict] = []
    for t in tasks:
        task_vars = t.get("vars") or {}
        if isinstance(task_vars, dict) and "dca_services" in task_vars:
            svcs = task_vars["dca_services"]
            if isinstance(svcs, list):
                out.extend(s for s in svcs if isinstance(s, dict))
    return out


def _primary_service_entry(tasks: list[dict]) -> dict:
    """The PRIMARY `dca_services` entry — the one whose `name == 'openbao'`
    exactly (the unsealer entry is `name: openbao-unsealer`)."""
    for svc in _dca_services_vars(tasks):
        if svc.get("name") == "openbao":
            return svc
    raise AssertionError(
        "no primary `dca_services` entry with `name: openbao` found in "
        f"{_OPENBAO_TASKS} (expected the 'Build the PRIMARY OpenBao Compose "
        "stack' docker-compose-app include)."
    )


def _ports_as_text(svc: dict) -> str:
    """Stringify the service's `ports` value regardless of shape — it may be a
    folded-Jinja STRING on the unfixed tree
    (`>- {{ [] if (openbao_mock_edge_network|bool) else [...] }}`) or a plain
    LIST on the fixed tree. Returns the concatenated string content so the
    assertions can inspect it uniformly."""
    ports = svc.get("ports")
    if ports is None:
        return ""
    if isinstance(ports, str):
        return ports
    if isinstance(ports, (list, tuple)):
        return "\n".join(str(p) for p in ports)
    return str(ports)


# ---------------------------------------------------------------------------
# Property 1 — Bug Condition (this FAILS on the unfixed tree)
# ---------------------------------------------------------------------------

def test_primary_ports_is_unconditional_vlan20_publish():
    """The PRIMARY OpenBao service's `ports` must be an UNCONDITIONAL publish of
    the VLAN20_Port, mirroring the unsealer's unconditional static `ports`:

      (a) it references BOTH `openbao_primary_ip` AND `openbao_api_port`;
      (b) it is NOT keyed on `openbao_mock_edge_network` (the ports value does
          not contain that string — the host-port publish must be decoupled from
          the edge/Traefik ingress path selection);
      (c) it never contains `0.0.0.0` (it is an INTERNAL VLAN-20 bind to the
          primary's own VLAN-20 IP, never a LAN/Traefik ingress host port).

    Parsed robustly: the primary `ports` value may be a folded-Jinja STRING on
    the unfixed tree or a plain LIST on the fixed tree — the assertions inspect
    the stringified content regardless of shape.

    Unfixed tree: the primary `ports` IS keyed on `openbao_mock_edge_network`
    (`[] if (openbao_mock_edge_network | bool) else [...]`), so on the Day-0
    mock-edge path it resolves to `[]` and `:8200` is never host-published ->
    assertion (b) FAILS, confirming the bug (the DIRECT VLAN-20 Prometheus scrape
    of `10.0.20.10:8200` gets `connection refused` -> OpenBao `up == 0`).

    Live corroboration (captured in `fix-obs-ac3-targets-up`):
    `docker inspect openbao-openbao-1` showed `PortBindings: {}` /
    `"8200/tcp": null`; `ss -ltnp` showed nothing on `:8200`; the direct VLAN-20
    scrape of `10.0.20.10:8200` gave `connection refused` -> OpenBao `up == 0`
    (with scrape TLS trust already fixed — the error had moved off `x509`).

    Validates: Requirements 1.1, 1.2, 2.1, 2.2
    """
    primary = _primary_service_entry(_load_tasks(_OPENBAO_TASKS))
    ports_text = _ports_as_text(primary)

    assert ports_text, (
        "the PRIMARY OpenBao `dca_services` entry (name: openbao) must declare a "
        f"`ports:` value (found none/empty in {_OPENBAO_TASKS})."
    )

    # (b) The host-port publish must NOT be keyed on the edge-path flag — this is
    # the defect: the mock-edge branch yields [] and :8200 is never published.
    assert _MOCK_EDGE_FLAG not in ports_text, (
        "the PRIMARY OpenBao `ports` must NOT be keyed on "
        f"`{_MOCK_EDGE_FLAG}` — the `:8200` VLAN20_Port host-publish is the "
        "DIRECT VLAN-20 reach for the Prometheus metrics scrape + the primary "
        "API and is ORTHOGONAL to whether the edge/Traefik ingress exists. On "
        "the unfixed tree it is the path-selected conditional "
        "`[] if (openbao_mock_edge_network | bool) else [...]`, so the Day-0 "
        "mock-edge path resolves to `[]` (publish nothing), `:8200` is never "
        "host-published, and the direct VLAN-20 scrape of 10.0.20.10:8200 gets "
        "`connection refused` -> OpenBao up == 0. The fix makes it the "
        "UNCONDITIONAL static publish, mirroring the unsealer. Current ports "
        f"value:\n{ports_text}"
    )

    # (a) The publish must reference BOTH the primary IP var and the API port var.
    assert _PRIMARY_IP_VAR in ports_text, (
        "the PRIMARY OpenBao `ports` must publish the VLAN20_Port bound to the "
        f"primary's own VLAN-20 IP (reference `{_PRIMARY_IP_VAR}`), mirroring "
        f"the unsealer's `{{{{ {_PRIMARY_IP_VAR}... }}}}` publish. Current ports "
        f"value:\n{ports_text}"
    )
    assert _API_PORT_VAR in ports_text, (
        "the PRIMARY OpenBao `ports` must publish the API/metrics port "
        f"(reference `{_API_PORT_VAR}`). Current ports value:\n{ports_text}"
    )

    # (c) Never bind 0.0.0.0/LAN — it is an internal VLAN-20 bind only.
    assert "0.0.0.0" not in ports_text, (
        "the PRIMARY OpenBao `:8200` publish must be an INTERNAL VLAN-20 bind to "
        "the primary's own VLAN-20 IP, NEVER `0.0.0.0`/LAN (R2.2 / "
        "integration-boundaries §1 — Traefik stays the only LAN HTTP/HTTPS "
        f"ingress). Current ports value:\n{ports_text}"
    )


# ---------------------------------------------------------------------------
# Property 2 — Preservation (these already hold on the UNFIXED tree and must
# keep holding after the fix: ONLY `ports` decouples; edge path-selection,
# unsealer publish, and the no-0.0.0.0 boundary are unchanged)
# ---------------------------------------------------------------------------

def _primary_dca_include_task(tasks: list[dict]) -> dict:
    """The docker-compose-app include task whose `vars` carry the PRIMARY
    `dca_services` entry (name: openbao) — the same task also carries the
    stack's `dca_networks`."""
    for t in tasks:
        task_vars = t.get("vars") or {}
        if not isinstance(task_vars, dict):
            continue
        svcs = task_vars.get("dca_services")
        if not isinstance(svcs, list):
            continue
        if any(isinstance(s, dict) and s.get("name") == "openbao" for s in svcs):
            return t
    raise AssertionError(
        "no docker-compose-app include task carrying the primary `dca_services` "
        f"entry (name: openbao) found in {_OPENBAO_TASKS}."
    )


def _unsealer_service_entry(tasks: list[dict]) -> dict:
    """The UNSEALER `dca_services` entry — `name == 'openbao-unsealer'`."""
    for svc in _dca_services_vars(tasks):
        if svc.get("name") == "openbao-unsealer":
            return svc
    raise AssertionError(
        "no unsealer `dca_services` entry with `name: openbao-unsealer` found in "
        f"{_OPENBAO_TASKS}."
    )


def _as_text(value) -> str:
    """Stringify any `ports`/`networks`/`labels`/`dca_networks` value regardless
    of shape (folded-Jinja string, plain list, or mapping)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}={_as_text(v)}" for k, v in value.items())
    return str(value)


def test_primary_edge_networks_labels_dca_networks_still_keyed_on_mock_edge():
    """PRESERVATION (a): the PRIMARY `- name: openbao` entry's `networks` AND
    `labels`, AND the stack's `dca_networks` (in the SAME docker-compose-app
    include task's `vars`), MUST STILL be keyed on `openbao_mock_edge_network`.

    Only the host-port publish (`ports`) decouples in this fix — the edge
    attachment (`networks`/`dca_networks`) and the Docker-label discovery
    (`labels`) stay correctly path-selected on the mock-edge flag so the Day-0
    Traefik-less bring-up keeps the placeholder `edge` + emits labels, and the
    real cross-guest path renders neither (the file-provider Route_Fragment
    supersedes them).

    These already hold on the UNFIXED tree -> PASS now; they must keep holding
    after the fix.

    Validates: Requirements 3.1, 3.2
    """
    tasks = _load_tasks(_OPENBAO_TASKS)
    primary = _primary_service_entry(tasks)
    include_task = _primary_dca_include_task(tasks)
    include_vars = include_task.get("vars") or {}

    networks_text = _as_text(primary.get("networks"))
    labels_text = _as_text(primary.get("labels"))
    dca_networks_text = _as_text(include_vars.get("dca_networks"))

    assert _MOCK_EDGE_FLAG in networks_text, (
        "the PRIMARY OpenBao `networks` must STAY keyed on "
        f"`{_MOCK_EDGE_FLAG}` (edge attachment stays path-selected; only `ports` "
        f"decouples). Current networks value:\n{networks_text}"
    )
    assert _MOCK_EDGE_FLAG in labels_text, (
        "the PRIMARY OpenBao `labels` must STAY keyed on "
        f"`{_MOCK_EDGE_FLAG}` (Traefik label discovery stays path-selected; only "
        f"`ports` decouples). Current labels value:\n{labels_text}"
    )
    assert _MOCK_EDGE_FLAG in dca_networks_text, (
        "the stack's `dca_networks` (same docker-compose-app include as the "
        f"primary service) must STAY keyed on `{_MOCK_EDGE_FLAG}` (the external "
        "`edge` dependency stays path-selected so the Day-0 mock-edge bring-up "
        "keeps the placeholder `edge: external: true`). Current dca_networks "
        f"value:\n{dca_networks_text}"
    )
    # The mock-edge branch must still carry the external `edge` network so the
    # Day-0 Traefik-less bring-up does not fail "network edge … could not be
    # found" (R3.2, structural proxy).
    assert "external" in dca_networks_text, (
        "the stack's `dca_networks` must still declare the external `edge` "
        "dependency on the mock-edge branch (external: true) so the Day-0 "
        f"bring-up keeps the placeholder edge. Current value:\n{dca_networks_text}"
    )


def test_unsealer_ports_still_unconditional_vlan20_publish():
    """PRESERVATION (b): the `- name: openbao-unsealer` entry's `ports` MUST
    STILL unconditionally publish `openbao_unsealer_ip` + `openbao_api_port` and
    MUST NOT be keyed on `openbao_mock_edge_network`.

    The unsealer is the pattern the primary fix mirrors; it is unchanged by this
    fix. This already holds on the UNFIXED tree -> PASS now.

    Validates: Requirements 3.3
    """
    unsealer = _unsealer_service_entry(_load_tasks(_OPENBAO_TASKS))
    ports_text = _ports_as_text(unsealer)

    assert ports_text, (
        "the UNSEALER `dca_services` entry (name: openbao-unsealer) must declare "
        f"a `ports:` value (found none/empty in {_OPENBAO_TASKS})."
    )
    assert _MOCK_EDGE_FLAG not in ports_text, (
        "the UNSEALER `ports` must NOT be keyed on "
        f"`{_MOCK_EDGE_FLAG}` — it publishes its seal-API `:8200` unconditionally "
        f"and statically. Current ports value:\n{ports_text}"
    )
    assert "openbao_unsealer_ip" in ports_text, (
        "the UNSEALER `ports` must publish the seal-API port bound to the "
        "unsealer's own VLAN-20 IP (reference `openbao_unsealer_ip`). Current "
        f"ports value:\n{ports_text}"
    )
    assert _API_PORT_VAR in ports_text, (
        "the UNSEALER `ports` must publish the seal-API port "
        f"(reference `{_API_PORT_VAR}`). Current ports value:\n{ports_text}"
    )


def test_no_ports_entry_in_role_binds_wildcard():
    """PRESERVATION (c): NO `ports` entry anywhere in the role (primary or
    unsealer) binds `0.0.0.0` — every published port is an INTERNAL VLAN-20 bind
    to the guest's own VLAN-20 IP (secret-hygiene + the Traefik-only-LAN-ingress
    boundary, integration-boundaries §1).

    This already holds on the UNFIXED tree -> PASS now; it must keep holding
    after the fix.

    Validates: Requirements 3.4
    """
    tasks = _load_tasks(_OPENBAO_TASKS)
    for svc in _dca_services_vars(tasks):
        ports_text = _ports_as_text(svc)
        assert "0.0.0.0" not in ports_text, (
            f"the `{svc.get('name')}` service's `ports` binds `0.0.0.0`/LAN — "
            "every published port must be an INTERNAL VLAN-20 bind to the guest's "
            "own VLAN-20 IP (R3.4 / integration-boundaries §1 — Traefik stays the "
            f"only LAN HTTP/HTTPS ingress). Current ports value:\n{ports_text}"
        )
