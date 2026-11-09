"""Offline cross-service drift-guard: teardown-parity contract (WS-M2).

Spec: .kiro/specs/teardown-parity-contract/ (WS-M2; PLANNING.md Part B).

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
Teardown is the largest recurring bug cluster, and the two most recent live-caught
teardown defects were PARITY gaps, not destroy-logic gaps:
  * WS-C2 — the platform clean-slate discovery catalog listed only svc07, so
    svc06/svc09 (implemented shared services with full clean-slate contracts) were
    silently left standing; and svc17 (SVC-17/18 observability) was STILL missing
    even after WS-C2.
  * WS-C3 — the consumer clean-slate provisioned a workload guest it never tore
    down.

Both are the same class: a service's teardown exists but is not wired into the
parity surface that makes it discoverable/invoked, and nothing mechanical caught
it. This guard makes teardown-parity a mechanical OFFLINE contract so the next
such omission fails here at author time, not in a live teardown.

WHAT IT ENFORCES
----------------
For every service that ships a bootstrap orchestrator (ansible/playbooks/
*-bootstrap.yml):
  * FR-1 — it has a matching ``<code>_clean_slate`` role AND routes to it (a
    top-level block gated ``when: clean_slate | bool`` that ``import_role``s the
    role, with the normal phases gated ``when: not (clean_slate ...)``).
  * FR-2 — IF it is a SHARED-tenancy (VLAN 20) service, it has a
    ``platform_clean_slate_services`` catalog row whose ``bootstrap_playbook``
    matches — so the platform teardown discovers + tears it down. Per-project
    services (svc01, consumer) are torn down by their own path and are exempt.
Plus non-vacuity (svc07 is checked) and no-rot (every classified code has a
bootstrap; every catalog row points at a real playbook + ``*_clean_slate`` role).

Service codes are NOT uniformly ``svcNN`` — SVC-17/18 uses the ``obs`` role
prefix, the per-project path uses ``consumer``. So the guard maps playbook -> code
by reading the clean_slate branch's ``import_role.name`` (strip ``_clean_slate``),
never by assuming ``svcNN``.

Helpers are COPIED locally (not imported across test trees — the conftest
collision rule), mirroring ``test_agent_live_wiring_coverage.py``. Pure PyYAML;
offline; collected under the default offline gate (``tests/installer`` root).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_teardown_parity.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

# tests/installer/test_teardown_parity.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP_GLOB = "ansible/playbooks/*-bootstrap.yml"
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_PLATFORM_CS_DEFAULTS = (
    _ROLES_DIR / "platform_clean_slate" / "defaults" / "main.yml"
)

# --------------------------------------------------------------------------- #
# Tenancy classification — the shared(VLAN-20)-vs-per-project map.
# AUTHORITATIVE SOURCE: 00-developer-services-catalog.md §3 "Tenancy" column +
# domain-rules.md §1/§3 (VLAN 20 hosts shared platform services only;
# per-project services live on VLAN 100+). Committed here (rather than parsed
# from the catalog markdown, which is fragile) and protected by the no-rot
# assertion below — every entry MUST correspond to a discovered bootstrap, so a
# renamed/removed service forces this map to be updated.
#
# Codes are keyed on the *_clean_slate ROLE name (what the bootstrap imports),
# NOT the catalog SVC code — SVC-17/18's role prefix is `obs`, the per-project
# path is `consumer`. (The platform catalog row for obs is labelled `svc17`, but
# that label is matched functionally by bootstrap_playbook in FR-2, not by code.)
#
# SHARED (VLAN 20) — MUST be registered in platform_clean_slate_services so the
# platform teardown discovers + tears them down:
_SHARED_CODES = frozenset({"svc06", "svc07", "svc09", "obs", "mailcatcher"})
# PER-PROJECT — torn down by their OWN / the consumer clean-slate path (they live
# on project VLANs, not VLAN 20), so they are EXEMPT from the platform-catalog
# check. They MUST still have a clean-slate role + routing (FR-1).
_PER_PROJECT_CODES = frozenset({"svc01", "consumer"})
# The platform teardown ORCHESTRATOR itself is not a catalog member and its
# teardown is a different shape (clean-slate.yml + platform_clean_slate); exempt
# from both FR-1 routing and FR-2. Keyed by bootstrap playbook basename.
_PLATFORM_BOOTSTRAP = "platform-bootstrap.yml"

# Non-vacuity anchor: svc07 MUST be among the iterated bootstrap services.
_NONVACUITY_CODE = "svc07"


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally per the conftest-collision rule (no cross-tree
# import), mirroring test_agent_live_wiring_coverage.py.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_play(path: Path) -> dict:
    docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    plays = [d for d in docs if isinstance(d, list) for p in d]
    # A bootstrap playbook is a single-play list document.
    for d in docs:
        if isinstance(d, list) and d and isinstance(d[0], dict):
            return d[0]
    raise AssertionError(f"{path} is not a single-play playbook")


def _play_tasks(play: dict) -> list:
    return list(_iter_tasks(play.get("tasks") or []))


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, (list, tuple)):
        return " ".join(str(w) for w in when)
    return str(when)


def _import_role_name(task: dict) -> str | None:
    imp = task.get("import_role") or task.get("ansible.builtin.import_role")
    if isinstance(imp, dict):
        return imp.get("name")
    return None


def _clean_slate_block(play: dict) -> dict | None:
    """Return the top-level task whose `when` gates on `clean_slate` truthy and
    whose block import_roles a `*_clean_slate` role — the clean-slate branch."""
    for task in play.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        w = _when_text(task)
        if "clean_slate" not in w or "not " in w:
            continue
        block = task.get("block")
        if not isinstance(block, list):
            continue
        for inner in _iter_tasks(block):
            name = _import_role_name(inner)
            if name and name.endswith("_clean_slate"):
                return task
    return None


def _code_from_clean_slate_block(block: dict) -> str | None:
    for inner in _iter_tasks(block.get("block") or []):
        name = _import_role_name(inner)
        if name and name.endswith("_clean_slate"):
            return name[: -len("_clean_slate")]
    return None


def _bootstrap_playbooks() -> list[Path]:
    return sorted(_REPO_ROOT.glob(_BOOTSTRAP_GLOB))


def _catalog_rows() -> list[dict]:
    defaults = yaml.safe_load(_PLATFORM_CS_DEFAULTS.read_text(encoding="utf-8")) or {}
    return [
        r for r in (defaults.get("platform_clean_slate_services") or [])
        if isinstance(r, dict)
    ]


def _service_map() -> dict[str, dict]:
    """Map code -> {playbook, play, clean_slate_block} for every bootstrap
    orchestrator EXCEPT the platform teardown orchestrator (different shape)."""
    out: dict[str, dict] = {}
    for pb in _bootstrap_playbooks():
        if pb.name == _PLATFORM_BOOTSTRAP:
            continue
        play = _load_play(pb)
        cs = _clean_slate_block(play)
        code = _code_from_clean_slate_block(cs) if cs else None
        out[pb.name] = {"playbook": pb, "play": play, "cs_block": cs, "code": code}
    return out


# =============================================================================
# FR-1 — every bootstrap service has a clean-slate role + routing.
# =============================================================================
def test_every_bootstrap_service_has_clean_slate_role_and_routing():
    svc_map = _service_map()
    assert svc_map, "No *-bootstrap.yml orchestrators discovered (vacuous scan)."

    offenders: list[str] = []
    for name, info in svc_map.items():
        cs = info["cs_block"]
        code = info["code"]
        if cs is None or not code:
            offenders.append(
                f"{name}: no clean_slate-gated block importing a *_clean_slate role"
            )
            continue
        role_dir = _ROLES_DIR / f"{code}_clean_slate"
        if not role_dir.is_dir():
            offenders.append(
                f"{name}: routes to '{code}_clean_slate' but role dir {role_dir} "
                "does not exist"
            )
            continue
        # The normal phases must be gated `when: not (clean_slate ...)` so a
        # clean-slate run skips provisioning. At least one such gated task.
        phases = [
            t for t in info["play"].get("tasks") or []
            if isinstance(t, dict) and "clean_slate" in _when_text(t)
            and "not " in _when_text(t)
        ]
        if not phases:
            offenders.append(
                f"{name}: no normal-phase task gated `when: not (clean_slate ...)` "
                "— the clean-slate branch may not be mutually exclusive with "
                "provisioning"
            )

    assert not offenders, (
        "WS-M2 FR-1 (teardown-parity): every service with a bootstrap "
        "orchestrator must have a matching *_clean_slate role AND route to it "
        "(a `when: clean_slate | bool` block importing it, with the normal phases "
        "gated `when: not (clean_slate ...)`). Offenders:\n  - "
        + "\n  - ".join(offenders)
    )


# =============================================================================
# FR-2 — every SHARED-tenancy service is in the platform teardown catalog.
# =============================================================================
def test_every_shared_service_is_in_the_platform_catalog():
    svc_map = _service_map()
    rows = _catalog_rows()
    catalog_bootstraps = {
        str(r.get("bootstrap_playbook", "")).rsplit("/", 1)[-1] for r in rows
    }

    missing: list[str] = []
    for name, info in svc_map.items():
        code = info["code"]
        if code not in _SHARED_CODES:
            continue  # per-project services are torn down by their own path
        if name not in catalog_bootstraps:
            missing.append(
                f"{code} ({name}) is a SHARED (VLAN-20) service but has no "
                "platform_clean_slate_services row whose bootstrap_playbook matches"
            )

    assert not missing, (
        "WS-M2 FR-2 (teardown-parity): every SHARED-tenancy (VLAN 20) service "
        "with a bootstrap MUST be registered in platform_clean_slate_services so "
        "the platform teardown discovers + tears it down (the WS-C2 / svc17 gap "
        "class). Missing:\n  - " + "\n  - ".join(missing)
    )


# =============================================================================
# FR-4 — non-vacuity + no-rot.
# =============================================================================
def test_guard_is_non_vacuous_and_classification_has_not_rotted():
    svc_map = _service_map()
    codes = {info["code"] for info in svc_map.values() if info["code"]}

    # Non-vacuity: svc07 must be among the iterated bootstrap services.
    assert _NONVACUITY_CODE in codes, (
        f"WS-M2 non-vacuity: expected {_NONVACUITY_CODE!r} among the discovered "
        f"bootstrap services; found {sorted(codes)}. The scan may be broken."
    )

    # No-rot (a): every code in the committed SHARED/PER-PROJECT maps MUST
    # correspond to a discovered bootstrap — so a renamed/removed service forces
    # the map to be updated rather than silently going unchecked.
    classified = _SHARED_CODES | _PER_PROJECT_CODES
    stale = sorted(classified - codes)
    assert not stale, (
        "WS-M2 no-rot: the committed tenancy map classifies code(s) that no "
        f"longer have a bootstrap orchestrator: {stale}. Update the SHARED / "
        "PER_PROJECT maps in this test (and re-confirm their tenancy against "
        "00-developer-services-catalog.md §3)."
    )

    # No-rot (b): every discovered code MUST be classified (SHARED or
    # PER_PROJECT) — a new service forces a conscious classification here.
    unclassified = sorted(codes - classified)
    assert not unclassified, (
        "WS-M2 no-rot: discovered bootstrap service code(s) are not classified "
        f"in this test's tenancy map: {unclassified}. Add each to _SHARED_CODES "
        "(VLAN-20 shared -> must be in the platform catalog) or "
        "_PER_PROJECT_CODES (own teardown path), per "
        "00-developer-services-catalog.md §3 + domain-rules.md §1/§3."
    )

    # No-rot (c): every platform_clean_slate_services row must point at a real
    # bootstrap playbook AND a real *_clean_slate role (a catalog row can't
    # reference a vanished service).
    row_offenders: list[str] = []
    for r in _catalog_rows():
        bp = str(r.get("bootstrap_playbook", ""))
        if not (_REPO_ROOT / bp).is_file():
            row_offenders.append(f"row {r.get('service')!r}: bootstrap_playbook {bp!r} missing")
    assert not row_offenders, (
        "WS-M2 no-rot: platform_clean_slate_services row(s) reference a missing "
        "bootstrap playbook:\n  - " + "\n  - ".join(row_offenders)
    )
