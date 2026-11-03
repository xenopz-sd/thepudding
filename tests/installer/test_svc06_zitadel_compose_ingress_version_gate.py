"""Offline bug-condition + preservation tests for the SVC-06 ZITADEL
compose-ingress version-gate decoupling bugfix.

Spec: .kiro/specs/fix-svc06-zitadel-compose-ingress-version-gate-decoupling/
(bugfix.md / design.md / tasks.md). OFFLINE structural tests — a PyYAML parse of
the `zitadel-server` role task file. No infra, no terraform binary — always runs.

=== THE DEFECT (bug-condition methodology) ===
In ``ansible/roles/zitadel-server/tasks/main.yml`` the docker-compose-app deploy
task (``"ZITADEL | Deploy the Compose stack (docker-compose-app)"``) — which ALSO
path-selects the cross-guest ingress wiring (ports/networks/labels/dca_networks)
on the ``zitadel_mock_edge_network`` swap-gate — was gated:

    when: not zitadel_already_at_pinned_version

So once ZITADEL runs at the pinned image the whole deploy is SKIPPED on every
re-run, and flipping ``zitadel_mock_edge_network`` true->false (the sanctioned
mock->real swap-gate once SVC-09 Traefik owns the real ``edge``, ADR-0013/0014)
never re-templates ``compose.yaml`` to publish the VLAN-20 port — Traefik returns
502 for ``auth.<domain>``. Same CLASS as
fix-svc01-openbao-credential-write-version-gate-decoupling: a necessary reconcile
wrongly gated behind the image-version check.

    isBugCondition(X) = already_at_pinned_image AND compose_input_changed

=== THE FIX ===
Change 1: remove ``when: not zitadel_already_at_pinned_version`` from the deploy
          task so the stack reconciles declaratively every run (docker-compose-app
          is idempotent — a converged stack is changed=0). Mirrors the Grafana /
          OpenBao deploys, which carry NO version gate.
Change 2: remove the now-orphaned ``zitadel_already_at_pinned_version`` set_fact +
          its ``docker inspect`` guard task (the fact had a single consumer — the
          removed gate; grep-confirmed).

=== HOW THIS TEST ENCODES IT ===
Property 1 (Bug Condition — FAILS unfixed, PASSES after fix):
  * the deploy task's ``when:`` does NOT reference zitadel_already_at_pinned_version
  * the role text contains ``zitadel_already_at_pinned_version`` zero times
Property 2 (Preservation — PASSES unfixed, stays green post-fix):
  * the deploy ports/networks/labels/dca_networks stay path-selected on
    zitadel_mock_edge_network (ingress swap-gate preserved)
  * the OpenBao masterkey/DB-password/provision-token tasks stay gated on
    zitadel_openbao_addr with the never-rotate provision-token guard
  * secret-bearing tasks keep no_log + BAO_TOKEN/OPENBAO_ADMIN_TOKEN in env

The task-dict helpers are LOCAL copies mirroring the sibling
tests/installer/test_svc06_cross_guest.py — the conftest-collision rule forbids a
cross-tree import.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_svc06_zitadel_compose_ingress_version_gate.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# --------------------------------------------------------------------------- #
# Paths. tests/installer/ -> repo root is parents[2].
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ZITADEL_ROLE = _REPO_ROOT / "ansible" / "roles" / "zitadel-server"
_ZITADEL_TASKS = _ZITADEL_ROLE / "tasks" / "main.yml"

# The install/version gate fact that is the defect when it gates the deploy.
_VERSION_FACT = "zitadel_already_at_pinned_version"


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


def _tasks() -> list[dict]:
    return _load_task_file(_ZITADEL_TASKS)


def _role_text() -> str:
    return _ZITADEL_TASKS.read_text(encoding="utf-8")


def _zitadel_dca_deploy_task() -> dict:
    """Return the ``include_role: docker-compose-app`` task that deploys the
    ZITADEL Compose stack (dca_app_name == zitadel_stack_name). Mirrors the
    finder in tests/installer/test_svc06_cross_guest.py."""
    deploys = []
    for t in _tasks():
        spec = t.get("ansible.builtin.include_role", t.get("include_role"))
        if not (isinstance(spec, dict) and spec.get("name") == "docker-compose-app"):
            continue
        tvars = t.get("vars") or {}
        if str(tvars.get("dca_app_name", "")).strip() == "{{ zitadel_stack_name }}":
            deploys.append(t)
    assert len(deploys) == 1, (
        f"expected exactly one ZITADEL docker-compose-app deploy task in "
        f"zitadel-server main.yml, got {len(deploys)}"
    )
    return deploys[0]


def _zitadel_service(deploy_task: dict) -> dict:
    tvars = deploy_task.get("vars") or {}
    services = tvars.get("dca_services") or []
    assert services and isinstance(services, list), "dca_services must be a non-empty list"
    svc = services[0]
    assert isinstance(svc, dict) and svc.get("name") == "zitadel", (
        f"dca_services[0] must be the zitadel service; got {svc!r}"
    )
    return svc


# =========================================================================== #
# PROPERTY 1 — BUG CONDITION (FAILS on the unfixed role, PASSES after the fix).
#   The deploy reconcile must NOT be gated on the image-version fact, and the
#   orphaned fact must be gone. These two assertions are the confirming
#   counterexamples: isBugCondition(X) = already_at_pinned_image AND
#   compose_input_changed.
# =========================================================================== #
def test_deploy_task_is_not_version_gated():
    """P1 (Validates: Req 2.1, 2.2, 2.4).

    BEFORE FIX: FAILS — the deploy carries `when: not
    zitadel_already_at_pinned_version`, so a re-run against an already-pinned
    container skips the whole Compose reconcile and the mock->real ingress flip is
    never applied (isBugCondition). AFTER FIX: PASSES — the deploy has no version
    gate and reconciles every run.
    """
    deploy = _zitadel_dca_deploy_task()
    when = deploy.get("when")
    # Normalise the `when:` to a single comparable string (it may be a scalar
    # string or a list of conditions).
    if when is None:
        when_items: list[str] = []
    elif isinstance(when, list):
        when_items = [str(c) for c in when]
    else:
        when_items = [str(when)]
    joined = " ".join(when_items).replace(" ", "")

    assert _VERSION_FACT not in joined, (
        "the ZITADEL docker-compose-app deploy task must NOT be gated on the "
        f"image-version fact `{_VERSION_FACT}` — a necessary declarative reconcile "
        "(the mock->real ingress flip) wrongly gated behind the image check is the "
        f"defect (same class as the SVC-01 precedent). got when={when!r}"
    )


def test_orphaned_version_fact_removed():
    """P1 (Validates: Req 2.1).

    BEFORE FIX: FAILS — the role still sets `zitadel_already_at_pinned_version`
    (line 34) from a `docker inspect` guard whose ONLY consumer is the removed
    deploy gate. AFTER FIX: PASSES — the orphaned fact + its guard task are gone
    (the role's declarative docker-compose-app idempotency is the real mechanism,
    matching the Grafana/OpenBao deploys which carry no such guard).
    """
    text = _role_text()
    assert _VERSION_FACT not in text, (
        f"the orphaned idempotency fact `{_VERSION_FACT}` (and its `docker inspect` "
        "guard task) must be removed — after decoupling the deploy it has no "
        f"consumer (grep-confirmed single use). Found {text.count(_VERSION_FACT)} "
        "occurrence(s)."
    )


# =========================================================================== #
# PROPERTY 2 — PRESERVATION (PASSES on the unfixed role, stays green post-fix).
#   Everything that must NOT change: the ingress path-selection keyed on
#   zitadel_mock_edge_network, the OpenBao sections gated only on
#   zitadel_openbao_addr (+ never-rotate), and secret handling.
# =========================================================================== #
def test_ingress_path_selection_preserved():
    """P2 (Validates: Req 3.1, 3.2).

    The deploy's ports/networks/labels/dca_networks stay path-selected Jinja
    conditionals keyed on zitadel_mock_edge_network, carrying both branches — the
    REAL branch (zitadel_bind_ip + zitadel_http_port, no edge, no labels) and the
    MOCK-EDGE branch ([] / zitadel_traefik_network / labels). The swap-gate is
    untouched by this bugfix.
    """
    deploy = _zitadel_dca_deploy_task()
    svc = _zitadel_service(deploy)
    tvars = deploy.get("vars") or {}

    ports_c = str(svc.get("ports", "")).replace(" ", "")
    assert "{{" in ports_c and "zitadel_mock_edge_network" in ports_c, (
        "ports must stay a Jinja conditional keyed on zitadel_mock_edge_network"
    )
    assert "zitadel_bind_ip" in ports_c and "zitadel_http_port" in ports_c, (
        "the real-path port mapping must still bind zitadel_bind_ip:zitadel_http_port"
    )
    assert "0.0.0.0" not in ports_c, "the VLAN20_Port must never bind 0.0.0.0 (R2.2)"
    assert "[]" in ports_c, "ports must keep the empty-list mock branch"

    nets_c = str(svc.get("networks", "")).replace(" ", "")
    assert "zitadel_mock_edge_network" in nets_c and "zitadel_traefik_network" in nets_c, (
        "networks must stay keyed on zitadel_mock_edge_network and reference "
        "zitadel_traefik_network on the mock branch"
    )
    assert "[]" in nets_c, "networks must resolve to [] on the real path"

    labels_c = str(svc.get("labels", "")).replace(" ", "")
    assert "zitadel_mock_edge_network" in labels_c and "zitadel_traefik_labels" in labels_c, (
        "labels must stay keyed on zitadel_mock_edge_network / reference "
        "zitadel_traefik_labels on the mock branch"
    )
    assert "[]" in labels_c, "labels must resolve to [] on the real path"

    dca_nets_c = str(tvars.get("dca_networks", "")).replace(" ", "")
    assert "zitadel_mock_edge_network" in dca_nets_c and "zitadel_traefik_network" in dca_nets_c, (
        "dca_networks must stay keyed on zitadel_mock_edge_network and declare the "
        "external edge dependency on the mock branch"
    )
    assert "[]" in dca_nets_c, "dca_networks must resolve to [] on the real path"


def test_openbao_sections_gated_on_address_not_version():
    """P2 (Validates: Req 3.3).

    The OpenBao masterkey/DB-password/provision-token tasks stay gated on
    zitadel_openbao_addr (never on a version gate) and keep the never-rotate
    provision-token guard. The bugfix touches none of them.
    """
    text = _role_text()
    # The OpenBao sections are address-gated (present and unchanged).
    assert "zitadel_openbao_addr | length > 0" in text or "zitadel_openbao_addr|length>0" in text.replace(" ", ""), (
        "the OpenBao read/write tasks must stay gated on zitadel_openbao_addr length"
    )
    # Never-rotate provision-token idempotency guard preserved.
    assert "not (zitadel_provision_token_stored | bool)" in text, (
        "the provision-token persist must keep its never-rotate guard "
        "`not (zitadel_provision_token_stored | bool)` (Req 3.3)"
    )
    # The masterkey is read-back / gen-once (never regenerated on a converged run).
    assert "Read any existing masterkey from OpenBao" in text, (
        "the masterkey gen-once/read-back section must be preserved"
    )


def test_secret_handling_preserved():
    """P2 (Validates: Req 3.4).

    Secret-bearing tasks keep no_log; the OpenBao admin token travels via
    BAO_TOKEN sourced from OPENBAO_ADMIN_TOKEN in the command environment, never
    on a bare `bao` positional argv.
    """
    text = _role_text()
    assert "no_log: true" in text, "secret-bearing tasks must keep no_log: true (Req 3.4)"
    assert "BAO_TOKEN=" in text and "OPENBAO_ADMIN_TOKEN" in text, (
        "the OpenBao admin token must travel via BAO_TOKEN sourced from "
        "OPENBAO_ADMIN_TOKEN in the command environment (never argv) (Req 3.4)"
    )
