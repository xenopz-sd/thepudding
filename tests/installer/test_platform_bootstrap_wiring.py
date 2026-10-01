"""Offline structural drift-guard for the platform-bootstrap orchestrator play.

Spec: .kiro/specs/platform-prerequisites-bootstrap/ (requirements.md, design.md,
tasks.md). Implements SPEC TASK 9.3 (Req 1.1, 1.2, 7.1, 7.2 / Properties 1, 6).
Guards the play wired in Tasks 9.1 (buckets) and 9.2 (throwaway-guard consult):
``ansible/playbooks/platform-bootstrap.yml``.

WHAT THIS GUARDS — the config-driven prerequisites orchestrator selects three
buckets (host, api, devmachine) off ``platform_selected_buckets`` and, on the two
destructive buckets (host + api), consults the throwaway-cluster authorization
guard BEFORE any pveum/pveam/terraform mutation. This pins that wiring as an
offline text/structure drift-guard, per the design's Testing Strategy (structural
assertions, not generative PBT).

  * P1 (bucket/phase selection) — the play carries FOUR gated block/rescue
    wrappers: the host bucket (identity + template), the api bucket (SDN fabric),
    the GATEWAY phase (host L3 gateway), and the devmachine bucket. host, gateway
    and devmachine are gated on ``platform_selected_buckets`` membership; the
    ``platform_selected_buckets`` parse of ``bootstrap_scope`` exists in the play
    vars; each wrapper's rescue names its phase; and ``platform_preflight`` runs
    first (before any bucket, ungated by bucket membership).
  * PHASE ORDER (Fix A / ADR-0010) — the play sequences
    identity -> template -> api -> gateway: PVE-identity (host bucket) is ordered
    BEFORE the api ``terraform apply``, and the host L3 gateway mutation is
    ordered AFTER the api ``terraform apply``. This guard BITES on the old
    host-then-gateway-before-api arrangement (gateway inline in the host bucket,
    before the api bucket ran) — that would fail the "gateway after api" check.
  * P6 (guard wiring) — the host bucket, the api bucket AND the gateway phase
    each carry the throwaway-guard consult (command -> INFO -> rc-assert) gated
    ``when: agent_live_run``, positioned BEFORE any pveum/pveam/terraform/gateway
    mutation (i.e. before their ``import_role`` calls); the devmachine bucket
    does NOT carry the consult.

HOW IT STAYS OFFLINE — pure PyYAML + structure parse of the committed play; no
``ansible-playbook`` binary, no live Proxmox. Always runs; needs no
``requires_infra`` gate. It PARSES YAML only and never embeds/prints a secret.
Mirrors the repo's drift-guard style (see ``test_thin_orchestrator_play.py`` and
``test_platform_api_guard.py``): helpers are copied locally, no cross-test-tree
imports, repo root via ``parents[2]``.

Validates: Requirements 1.1, 1.2, 7.1, 7.2 / Properties 1, 6
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_platform_bootstrap_wiring.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAY = _REPO_ROOT / "ansible" / "playbooks" / "platform-bootstrap.yml"

# The four selectable phases, in dependency order (Fix A / ADR-0010):
#   host bucket (identity + template) -> api bucket (SDN fabric)
#   -> gateway phase (host L3 gateway) -> devmachine bucket.
# The `host` bucket and the `gateway` phase are BOTH gated on
# `'host' in platform_selected_buckets` (the gateway is host-layer config), so
# phases are keyed by a stable marker in their wrapper `name:`, not by the gate
# alone. Each entry: phase-key -> (name-marker, membership-token-or-None).
_PHASES = ("host", "api", "gateway", "devmachine")
# The three destructive phases that must carry the throwaway-guard consult
# (host = pveum/pveam, api = terraform, gateway = sdn_gateway host mutation).
_GUARDED_PHASES = ("host", "api", "gateway")
# The selectable buckets whose wrapper is gated on membership (all but api,
# which is gated on 'api'; host + gateway on 'host'; devmachine on 'devmachine').
_MEMBERSHIP_TOKEN = {
    "host": "host",
    "api": "api",
    "gateway": "host",       # gateway phase is host-layer, gated on 'host'
    "devmachine": "devmachine",
}
# A stable lowercase marker that identifies each phase wrapper by its `name:`,
# distinguishing the two `'host'`-gated wrappers (host bucket vs gateway phase).
_NAME_MARKER = {
    "host": "bucket — host",
    "api": "bucket — api",
    "gateway": "phase — gateway",
    "devmachine": "bucket — devmachine",
}

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
# Helpers — copied locally per the repo drift-guard convention.
# --------------------------------------------------------------------------- #
def _load_plays(path: Path) -> list[dict]:
    """Return the list of play dicts (a playbook is a top-level list of plays)."""
    plays: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            plays.extend(p for p in doc if isinstance(p, dict))
        elif isinstance(doc, dict):
            plays.append(doc)
    return plays


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _module_names(task: dict) -> set[str]:
    return {k for k in task if k not in _NON_MODULE_KEYS}


def _the_play() -> dict:
    plays = _load_plays(_PLAY)
    assert len(plays) == 1, (
        f"platform-bootstrap.yml must be a single-play orchestrator; found {len(plays)}."
    )
    return plays[0]


def _top_tasks() -> list[dict]:
    return list(_the_play().get("tasks") or [])


def _when_text(task: dict) -> str:
    """Flatten a task's ``when`` (str or list) into one string."""
    w = task.get("when")
    if w is None:
        return ""
    if isinstance(w, (list, tuple)):
        return " ".join(str(x) for x in w)
    return str(w)


def _import_role_names(tasks) -> list[str]:
    """Ordered role names from every import_role/include_role task in the tree.

    Matches BOTH ``import_role`` (static) and ``include_role`` (dynamic). The
    bring-up phases load their roles with ``include_role`` so the enclosing
    block's ``when: not clean_slate`` gate skips the whole phase on a clean-slate
    run (a static import would push the gate down onto every task and evaluate
    them anyway); the clean-slate branch uses ``import_role``. This detector
    recognises either so the P1/ordering/mutation-marker checks still find every
    bring-up + clean-slate role load regardless of which loader is used.
    """
    names: list[str] = []
    for task in _iter_tasks(tasks):
        for key in (
            "ansible.builtin.import_role", "import_role",
            "ansible.builtin.include_role", "include_role",
        ):
            spec = task.get(key)
            if isinstance(spec, dict) and "name" in spec:
                names.append(str(spec["name"]))
    return names


def _fail_msg(task: dict) -> str:
    spec = task.get("ansible.builtin.fail") or task.get("fail") or {}
    if isinstance(spec, dict):
        return str(spec.get("msg", ""))
    return str(spec)


def _bucket_wrappers() -> dict[str, dict]:
    """Map phase-key -> its top-level block/rescue wrapper task.

    A phase wrapper is a top-level task that has both ``block`` and ``rescue``,
    is gated on ``platform_selected_buckets`` membership, and whose ``name:``
    carries the phase's stable marker (``_NAME_MARKER``). Keying on the name
    marker — not the gate alone — is required because the host bucket and the
    gateway phase are BOTH gated on ``'host' in platform_selected_buckets`` (the
    gateway is host-layer config sequenced after the api block, Fix A/ADR-0010).
    The preflight wrapper (no bucket-membership gate) is intentionally excluded.
    """
    wrappers: dict[str, dict] = {}
    for task in _top_tasks():
        if not (isinstance(task, dict) and "block" in task and "rescue" in task):
            continue
        gate = _when_text(task)
        if "platform_selected_buckets" not in gate:
            continue
        name = str(task.get("name", "")).lower()
        for phase in _PHASES:
            if _NAME_MARKER[phase] in name:
                # Sanity: the wrapper's gate must match the phase's expected
                # membership token (host+gateway on 'host', api on 'api', ...).
                token = _MEMBERSHIP_TOKEN[phase]
                if f"'{token}'" in gate or f'"{token}"' in gate:
                    wrappers[phase] = task
                break
    return wrappers


# --------------------------------------------------------------------------- #
# Guard-consult detection (for P6).
# --------------------------------------------------------------------------- #
def _task_argv_text(task: dict) -> str:
    """Flat string of a command/shell task's argv, else ''."""
    parts: list[str] = []
    for mod in ("ansible.builtin.command", "command",
                "ansible.builtin.shell", "shell"):
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
    return " ".join(parts)


def _is_guard_consult_command(task: dict) -> bool:
    """True if the task is the throwaway_guard command consult."""
    return "throwaway_guard" in _task_argv_text(task)


def _is_mutation_task(task: dict) -> bool:
    """True if the task is a pveum/pveam/terraform mutation, or imports a role
    that performs one.

    The bucket bodies drive their mutations through a dynamic ``include_role``
    (platform_pve_identity / platform_host_prep / platform_api / platform_gateway;
    ``_import_role_names`` matches both include_role and import_role), so an
    include/import of any of those roles counts as the point past which no
    unauthorized guard-less mutation may occur. A direct pveum/pveam/terraform
    argv also counts, in case a future edit inlines one.
    """
    role_names = _import_role_names([task])
    mutating_roles = {
        "platform_pve_identity", "platform_host_prep", "platform_api",
        "platform_gateway",
    }
    if set(role_names) & mutating_roles:
        return True
    argv = _task_argv_text(task).lower()
    return any(tok in argv for tok in ("pveum", "pveam", "terraform"))


# =============================================================================
# P1 — bucket selection.
# Validates: Requirements 1.1, 1.2 / Property 1.
# =============================================================================
def test_platform_selected_buckets_parse_exists_in_play_vars():
    """Validates: Requirements 1.2 / Property 1.

    The play vars must define ``platform_selected_buckets`` as the parse of
    ``bootstrap_scope`` — the single place bucket selection is computed. Each
    bucket block is gated on membership in this list.
    """
    play_vars = _the_play().get("vars") or {}
    assert "platform_selected_buckets" in play_vars, (
        "DRIFT: play vars must define `platform_selected_buckets` (the parse of "
        "bootstrap_scope that every bucket block is gated on) — not found."
    )
    parse = str(play_vars["platform_selected_buckets"])
    assert "bootstrap_scope" in parse, (
        "DRIFT: `platform_selected_buckets` must be derived from `bootstrap_scope`; "
        f"its expression does not reference bootstrap_scope: {parse!r}."
    )


def test_four_gated_phase_block_rescue_wrappers():
    """Validates: Requirements 1.1, 1.2 / Property 1.

    The play must carry exactly four phase block/rescue wrappers — host bucket,
    api bucket, gateway phase, devmachine bucket — each gated on membership in
    ``platform_selected_buckets`` (host + gateway on ``'host'``, api on
    ``'api'``, devmachine on ``'devmachine'``).
    """
    wrappers = _bucket_wrappers()

    for phase in _PHASES:
        assert phase in wrappers, (
            f"DRIFT: no top-level block/rescue wrapper for the {phase!r} phase "
            f"(name marker {_NAME_MARKER[phase]!r}, gated on "
            f"'{_MEMBERSHIP_TOKEN[phase]}' in platform_selected_buckets) was "
            f"found. All four phases (host, api, gateway, devmachine) must be "
            f"gated wrappers; found {sorted(wrappers)}."
        )

    assert len(wrappers) == 4, (
        "There must be exactly four gated phase wrappers (host, api, gateway, "
        f"devmachine); found {sorted(wrappers)}."
    )


def test_each_phase_rescue_names_its_phase():
    """Validates: Requirements 1.2 / Property 1.

    Each phase's ``rescue`` must contain a ``fail`` diagnostic naming its phase,
    so a failure reports which phase aborted (mirrors the svc07 phase rescues).
    """
    wrappers = _bucket_wrappers()

    for phase, wrapper in wrappers.items():
        rescue_tasks = list(_iter_tasks(wrapper.get("rescue")))
        fail_msgs = [
            _fail_msg(t) for t in rescue_tasks
            if "ansible.builtin.fail" in t or "fail" in t
        ]
        assert fail_msgs, (
            f"DRIFT: the {phase!r} phase rescue must contain a `fail` diagnostic "
            "naming the phase; none found."
        )
        joined = "\n".join(fail_msgs).lower()
        assert phase in joined, (
            f"DRIFT: the {phase!r} phase rescue `fail` message must name the "
            f"{phase!r} phase; messages seen:\n{joined}"
        )


def test_pve_identity_before_api_and_gateway_after_api():
    """Validates: Requirements 1.3 / Property 1 (Fix A / ADR-0010 phase order).

    The play must sequence identity -> template -> api -> gateway:

      * the PVE-identity mutation (host bucket) is ordered BEFORE the api
        bucket's ``terraform apply`` (the api consumes the minted token), AND
      * the host L3 gateway mutation (gateway phase) is ordered AFTER the api
        bucket's ``terraform apply`` (the gateway binds .1 to the SDN VNet bridge
        p<vlan> that the api ``terraform apply`` creates).

    This BITES on the old arrangement where the gateway ran inline in the host
    bucket BEFORE the api bucket — there the gateway wrapper index would precede
    the api wrapper index and this assertion would fail.
    """
    wrappers = _bucket_wrappers()
    top = _top_tasks()

    host_idx = top.index(wrappers["host"])
    api_idx = top.index(wrappers["api"])
    gateway_idx = top.index(wrappers["gateway"])

    # identity (host bucket) before api.
    assert host_idx < api_idx, (
        "DRIFT (Fix A / ADR-0010): the host bucket (PVE-identity mint, "
        f"index={host_idx}) must be ordered BEFORE the api bucket "
        f"(index={api_idx}) — the api `terraform apply` consumes the minted "
        "terraform@pve token."
    )

    # gateway AFTER api (the bug this reorder fixes).
    assert api_idx < gateway_idx, (
        "DRIFT (Fix A / ADR-0010 — the bug this guard bites on): the gateway "
        f"phase (index={gateway_idx}) must be ordered AFTER the api bucket "
        f"(index={api_idx}). The host L3 gateway binds .1 to the SDN VNet bridge "
        "p<vlan> that the api `terraform apply` creates; on a clean slate it "
        "CANNOT run before the api bucket. The old host-then-gateway-before-api "
        "arrangement is exactly what this assertion forbids."
    )


def test_api_bucket_runs_terraform_apply_mutation():
    """Validates: Requirements 1.3, 4.1 / Property 1 (ordering anchor).

    Anchors the ordering guard above against a rename: the api bucket must
    actually drive a terraform mutation (its ``platform_api`` import_role, whose
    role child-runs ``terraform apply``), so "gateway after api" is anchored to a
    real fabric-creating step, not a vacuous wrapper.
    """
    wrappers = _bucket_wrappers()
    api_body = list(_iter_tasks(wrappers["api"].get("block")))
    assert any(_is_mutation_task(t) for t in api_body), (
        "DRIFT: the api bucket must carry a mutating step (the platform_api "
        "import_role / a terraform apply) so the gateway-after-api ordering "
        "check anchors on a real fabric-creating mutation."
    )


def test_preflight_runs_first_before_any_bucket():
    """Validates: Requirements 1.1 / Property 1.

    ``platform_preflight`` must run FIRST for every invocation — its import must
    be ordered before every bucket wrapper, and it must NOT be gated on bucket
    membership (it is the shared gate that runs regardless of selection).
    """
    top = _top_tasks()

    # Index of the first task that imports platform_preflight.
    preflight_idx = next(
        (i for i, t in enumerate(top)
         if "platform_preflight" in _import_role_names([t])),
        None,
    )
    assert preflight_idx is not None, (
        "DRIFT: no import of the `platform_preflight` role found at the play task "
        "level; it must run first for every invocation."
    )

    # The preflight wrapper (the top-level task containing that import) must not
    # be gated on bucket membership.
    preflight_wrapper = top[preflight_idx]
    assert "platform_selected_buckets" not in _when_text(preflight_wrapper), (
        "DRIFT: `platform_preflight` must be ungated by bucket membership — it is "
        "the shared gate that runs for every invocation, not a selectable bucket."
    )

    # Every phase wrapper must be ordered AFTER the preflight import.
    wrappers = _bucket_wrappers()
    for phase, wrapper in wrappers.items():
        phase_idx = top.index(wrapper)
        assert preflight_idx < phase_idx, (
            f"DRIFT (Property 1): `platform_preflight` (index={preflight_idx}) must "
            f"run before the {phase!r} phase (index={phase_idx})."
        )


# =============================================================================
# P6 — guard wiring.
# Validates: Requirements 7.1, 7.2 / Property 6.
# =============================================================================
def test_destructive_phases_carry_gated_guard_consult():
    """Validates: Requirements 7.1, 7.2 / Property 6.

    The host bucket, api bucket AND gateway phase must each carry the
    throwaway-guard consult command, gated ``when: agent_live_run`` — an ordinary
    operator run (interlock unset) skips it entirely. All three drive a
    destructive host/API mutation (pveum/pveam, terraform, sdn_gateway).
    """
    wrappers = _bucket_wrappers()

    for phase in _GUARDED_PHASES:
        body = list(_iter_tasks(wrappers[phase].get("block")))
        consults = [t for t in body if _is_guard_consult_command(t)]
        assert consults, (
            f"DRIFT (Property 6): the {phase!r} phase must carry the "
            "throwaway-guard consult (a command invoking throwaway_guard); none "
            "found."
        )
        for consult in consults:
            assert "agent_live_run" in _when_text(consult), (
                f"DRIFT (Property 6): the {phase!r} phase's throwaway-guard "
                "consult must be gated `when: agent_live_run` so an ordinary "
                f"operator run skips it; its `when` is {_when_text(consult)!r}."
            )


def test_guard_consult_precedes_any_mutation_in_destructive_phases():
    """Validates: Requirements 7.1, 7.2 / Property 6.

    In the host bucket, api bucket and gateway phase, the throwaway-guard consult
    must be positioned BEFORE any pveum/pveam/terraform/gateway mutation (i.e.
    before the mutating ``import_role`` calls) — so a non-throwaway target is
    refused before anything is touched.
    """
    wrappers = _bucket_wrappers()

    for phase in _GUARDED_PHASES:
        # Direct children of the phase block, in document order.
        body = list(wrappers[phase].get("block") or [])

        consult_idx = next(
            (i for i, t in enumerate(body) if _is_guard_consult_command(t)),
            None,
        )
        assert consult_idx is not None, (
            f"DRIFT (Property 6): no throwaway-guard consult found at the head of "
            f"the {phase!r} phase block."
        )

        mutation_idx = next(
            (i for i, t in enumerate(body) if _is_mutation_task(t)),
            None,
        )
        assert mutation_idx is not None, (
            f"DRIFT: no pveum/pveam/terraform/gateway mutation (or mutating "
            f"import_role) found in the {phase!r} phase — cannot anchor the "
            "ordering check."
        )

        assert consult_idx < mutation_idx, (
            f"DRIFT (Property 6): in the {phase!r} phase the throwaway-guard "
            f"consult (index={consult_idx}) must precede any mutation "
            f"(index={mutation_idx}) so a non-throwaway target is refused BEFORE "
            "any Proxmox resource is touched."
        )


def test_devmachine_bucket_has_no_guard_consult():
    """Validates: Requirements 7.4 / Property 6.

    The devmachine bucket mutates only the developer's own workstation, never the
    shared cluster, so it must NOT carry a throwaway-guard consult.
    """
    wrappers = _bucket_wrappers()

    devmachine_body = list(_iter_tasks(wrappers["devmachine"].get("block")))
    consults = [t for t in devmachine_body if _is_guard_consult_command(t)]
    assert not consults, (
        "DRIFT (Property 6): the devmachine bucket must NOT carry a "
        "throwaway-guard consult (it only touches the developer's own "
        f"workstation); found {len(consults)} consult task(s)."
    )
