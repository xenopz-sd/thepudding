"""Offline structural guard for the obs_clean_slate Grafana Route_Fragment removal.

Defect fix (spec: traefik-cross-guest-ingress) — a LIVE teardown surfaced that
the clean-slate route-removal task SILENTLY no-op'd: it ran in the
``hosts: localhost`` orchestrator play (``ansible/inventory/localhost.yml``,
``ansible_connection: local``) with a bare ``delegate_to: 10.0.20.12`` that had
NO working SSH connection (no ``add_host``, no ``known_hosts`` entry in that
context — unlike the configurator play), and ``failed_when: false`` swallowed
the connection failure as a FALSE "already absent", leaving a dangling Traefik
route pointing at the freed Grafana VLAN-20 IP.

This guard pins the FIX, by parsing
``ansible/roles/obs_clean_slate/tasks/main.yml`` only (no ``ansible-playbook``,
no live Proxmox — always runs, no ``requires_infra`` gate):

  * the route-removal task delegates to the Traefik guest AND a known_hosts /
    ssh-keyscan reconciliation for ``obs_traefik_guest_host`` PRECEDES it (the
    orchestrator-context connection setup — the removal is NOT connectionless);
  * the removal is GATED on a reachability fact and does NOT use a blanket
    ``failed_when: false`` that could mask a real removal failure (the masking
    pattern is gone / replaced by a conditional ``failed_when``);
  * the INFO outcome task distinguishes "unreachable on full teardown" from
    "already absent" — the exact conflation that hid the bug.

Mirrors the repo drift-guard style (``test_obs_clean_slate_structural.py``):
helpers COPIED locally (no cross-test-tree import — the tests/installer vs
infra/tests conftest-collision rule), repo root via ``parents[2]``, PyYAML parse
only, never embeds/prints a secret.

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_obs_clean_slate_route_removal.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_obs_clean_slate_route_removal.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_TASKS = _REPO_ROOT / "ansible" / "roles" / "obs_clean_slate" / "tasks" / "main.yml"

# Keys on a task dict that are directives/containers, NOT the invoked module.
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "become", "vars", "tags", "args",
        "block", "rescue", "always", "until", "retries", "delay", "environment",
        "check_mode", "notify", "listen", "ignore_errors",
    }
)


# --------------------------------------------------------------------------- #
# Helpers — COPIED locally per the repo drift-guard convention (no cross-tree
# import; the conftest-collision rule). Mirrors test_obs_clean_slate_structural.
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path) -> list[dict]:
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _is_module(task: dict, *names: str) -> bool:
    mods = _module_names(task)
    return any(n in mods for n in names)


def _task_argv_text(task: dict) -> str:
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            parts.append(spec)
        elif isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                parts.append(" ".join(str(a) for a in argv))
            elif isinstance(argv, str):
                parts.append(argv)
            for key in ("cmd", "stdin"):
                if isinstance(spec.get(key), str):
                    parts.append(spec[key])
    return " ".join(parts)


def _when_text(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, (list, tuple)):
        return " ".join(str(w) for w in when)
    return str(when)


def _failed_when_text(task: dict) -> str:
    fw = task.get("failed_when")
    if fw is None:
        return "__ABSENT__"
    if isinstance(fw, (list, tuple)):
        return " ".join(str(x) for x in fw)
    return str(fw)


def _delegate_text(task: dict) -> str:
    return str(task.get("delegate_to", ""))


def _debug_msg_text(task: dict) -> str:
    spec = task.get("ansible.builtin.debug") or task.get("debug")
    if not isinstance(spec, dict):
        return ""
    return str(spec.get("msg", ""))


def _first_index(tasks: list[dict], pred) -> int | None:
    return next((i for i, t in enumerate(tasks) if pred(t)), None)


# --------------------------------------------------------------------------- #
# Marker detectors specific to the route-removal fix.
# --------------------------------------------------------------------------- #
def _is_route_removal(task: dict) -> bool:
    """The file-state-absent removal of the Grafana Route_Fragment."""
    spec = task.get("ansible.builtin.file") or task.get("file")
    if not isinstance(spec, dict):
        return False
    return (
        str(spec.get("path", "")).find("obs_grafana_route_fragment") != -1
        and str(spec.get("state", "")) == "absent"
    )


def _is_traefik_keyscan(task: dict) -> bool:
    argv = _task_argv_text(task)
    return "ssh-keyscan" in argv and "obs_traefik_guest_host" in argv


def _is_traefik_keygen_purge(task: dict) -> bool:
    argv = _task_argv_text(task)
    return "ssh-keygen" in argv and "obs_traefik_guest_host" in argv


def _reachability_set_fact_body(task: dict) -> str | None:
    spec = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    if not isinstance(spec, dict) or "obs_traefik_reachable" not in spec:
        return None
    return str(spec["obs_traefik_reachable"])


# --------------------------------------------------------------------------- #
# Sanity — non-vacuous parse.
# --------------------------------------------------------------------------- #
def test_role_parses_and_has_a_route_removal_task():
    assert _ROLE_TASKS.is_file(), f"obs_clean_slate role tasks not found at {_ROLE_TASKS}."
    tasks = _load_role_tasks(_ROLE_TASKS)
    assert len(tasks) >= 12, (
        f"vacuous-parse guard: parsed only {len(tasks)} tasks; expected >=12."
    )
    assert _first_index(tasks, _is_route_removal) is not None, (
        "DRIFT: no Grafana Route_Fragment removal task (file state=absent on "
        "obs_grafana_route_fragment) found in the role."
    )


# =============================================================================
# The removal delegates to the Traefik guest AND a known_hosts/ssh-keyscan
# reconciliation PRECEDES it — the orchestrator-context connection setup.
# =============================================================================
def test_route_removal_delegates_to_traefik_guest_with_preceding_knownhosts_reconcile():
    """The removal is NOT left connectionless in the localhost orchestrator play.

    BITES if the removal is restored to a bare delegate with no preceding SSH
    connection setup (the exact shape that silently no-op'd live).
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    removal_idx = _first_index(tasks, _is_route_removal)
    assert removal_idx is not None, "DRIFT: no route-removal task found."

    # It delegates to the Traefik guest — either the add_host alias or the IP var.
    delegate = _delegate_text(tasks[removal_idx])
    assert delegate, (
        "DRIFT: the route-removal task must delegate to the Traefik guest "
        "(delegate_to); none found."
    )
    assert ("traefik-cross-guest-cleanup" in delegate) or ("obs_traefik_guest_host" in delegate), (
        "DRIFT: the removal must delegate to the Traefik guest (the add_host "
        f"alias or obs_traefik_guest_host); delegate_to={delegate!r}."
    )

    # A ssh-keyscan reconciliation for the Traefik guest PRECEDES the removal.
    keyscan_idx = _first_index(tasks, _is_traefik_keyscan)
    assert keyscan_idx is not None, (
        "DRIFT: no ssh-keyscan of obs_traefik_guest_host found — the "
        "orchestrator-context known_hosts setup is missing, so the delegated "
        "removal would have no working SSH connection (the live-silent-no-op bug)."
    )
    assert keyscan_idx < removal_idx, (
        f"DRIFT: the ssh-keyscan reconciliation (index={keyscan_idx}) must PRECEDE "
        f"the route removal (index={removal_idx}); the connection must be set up "
        "before delegating."
    )

    # A stale-fingerprint purge (ssh-keygen -R) for the guest is also present,
    # mirroring obs_provision's known_hosts block byte-for-byte in style.
    assert _first_index(tasks, _is_traefik_keygen_purge) is not None, (
        "DRIFT: no `ssh-keygen -R obs_traefik_guest_host` stale-fingerprint purge "
        "found — the obs_provision-style known_hosts reconcile is incomplete."
    )


# =============================================================================
# The removal is gated on a reachability fact and does NOT use a blanket
# failed_when: false that could mask a real removal failure.
# =============================================================================
def test_removal_gated_on_reachability_fact_and_not_blanket_failed_when_false():
    """The silent-false-success masking pattern is gone / replaced by a conditional.

    BITES if the removal regresses to `failed_when: false` (blanket mask) or
    loses its reachability gate.
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    # A reachability fact is computed from the keyscan (rc + non-empty stdout).
    reach_bodies = [
        b for t in tasks if (b := _reachability_set_fact_body(t)) is not None
    ]
    assert reach_bodies, (
        "DRIFT: no set_fact computing `obs_traefik_reachable` found — the "
        "reachability gate the removal depends on is missing."
    )
    for body in reach_bodies:
        assert "obs_traefik_keyscan" in body and "rc" in body and "stdout" in body, (
            "DRIFT: `obs_traefik_reachable` must be derived from the keyscan rc "
            f"AND non-empty stdout; body: {body!r}"
        )

    removal_idx = _first_index(tasks, _is_route_removal)
    removal = tasks[removal_idx]

    # Gated on the reachability fact.
    assert "obs_traefik_reachable" in _when_text(removal), (
        "DRIFT: the route removal must be gated `when: obs_traefik_reachable ...` "
        f"so a full teardown (Traefik gone) skips it cleanly; when={_when_text(removal)!r}."
    )

    # The blanket failed_when:false mask must be GONE. Either failed_when is a
    # conditional list/expr that is NOT the literal `false`, or it is absent.
    fw = _failed_when_text(removal)
    masking = fw.strip().lower() in {"false", "no"}
    assert not masking, (
        "DRIFT (silent-false-success): the route removal must NOT use a blanket "
        f"`failed_when: false` that masks a real removal failure; failed_when={fw!r}."
    )
    # If present, the conditional must be tolerant of 'unreachable' only (the
    # full-teardown case), not a blanket mask.
    if fw != "__ABSENT__":
        assert "unreachable" in fw.lower() or "obs_grafana_route_removed" in fw, (
            "DRIFT: the removal's failed_when must be a REAL condition keyed on "
            "the removal result (tolerant of 'unreachable' only), not a blanket "
            f"mask; failed_when={fw!r}."
        )


# =============================================================================
# The INFO task distinguishes unreachable-on-full-teardown from already-absent.
# =============================================================================
def test_info_task_distinguishes_unreachable_from_already_absent():
    """The exact conflation that hid the bug must be gone.

    The INFO outcome must have THREE distinguishable states in its message:
    removed / already-absent / unreachable-on-full-teardown — so "unreachable"
    is never reported as "already absent".
    """
    tasks = _load_role_tasks(_ROLE_TASKS)

    info_tasks = [
        t for t in tasks
        if _is_module(t, "ansible.builtin.debug", "debug")
        and "obs_grafana_route_fragment" in _debug_msg_text(t)
    ]
    assert info_tasks, (
        "DRIFT: no INFO debug task about the Grafana Route_Fragment removal "
        "outcome found."
    )

    found = False
    for t in info_tasks:
        msg = _debug_msg_text(t).lower()
        has_unreachable = "unreachable" in msg
        has_already_absent = "already absent" in msg or "already-absent" in msg
        has_removed = "removed" in msg
        # The outcome branch must be keyed on the reachability fact so the
        # unreachable path is a DISTINCT message, not folded into already-absent.
        keyed_on_reachability = "obs_traefik_reachable" in _debug_msg_text(t)
        if has_unreachable and has_already_absent and has_removed and keyed_on_reachability:
            found = True
            break

    assert found, (
        "DRIFT (bug-hiding conflation): the INFO task must distinguish THREE "
        "states — removed / already-absent / unreachable-on-full-teardown — "
        "branching on `obs_traefik_reachable`, so 'unreachable' is never reported "
        "as 'already absent' (the conflation that hid the silent no-op)."
    )
