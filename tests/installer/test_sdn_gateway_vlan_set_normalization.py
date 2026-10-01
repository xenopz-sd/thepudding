"""Offline structural drift-guard for the sdn_gateway VLAN-set normalization.

Spec context: this guards the fix for the live Task-15.1 Issue-#3 defect in the
platform clean-slate — after `terraform destroy` + gateway teardown + reboot the
SDN config was empty, yet `p20` came BACK on the next boot because
`/etc/network/interfaces.d/sdn-gw-20.cfg` (its `auto p20` stanza) was never
removed by the decommission.

ROOT CAUSE (diagnosed live): both callers of `sdn-gateway.yml` (the bring-up
`platform_gateway` role and the tear-down `platform_clean_slate` role) pass the
VLAN-ID sets through the child-run idiom as a single
`--extra-vars "sdn_gateway_decommissioned_vlan_ids=[20]"` token. Ansible parses a
bare `key=value` extra-var as the STRING "[20]", NOT a list. The role's teardown
loop then did `sdn_gateway_decommissioned_vlan_ids | map('int')`, which iterates
the STRING'S CHARACTERS ('[', '2', '0', ']') and yields phantom VLANs [0, 2]
instead of [20]. So teardown ran for VLANs 0 and 2 and NEVER for VLAN 20 — the
`file: state=absent` removal of `sdn-gw-20.cfg` was never reached, and the
`auto p20` stanza survived to recreate the bridge on the next boot.

THE FIX (in the SHARED role `ansible/roles/sdn_gateway/tasks/main.yml`): a
`set_fact` normalizes each caller-supplied set into a DEDICATED `_norm` fact —
a real integer list whether the value arrived as a genuine list (bring-up in-
process default / JSON extra-var — passes through unchanged) or a string like
"[20]"/"20,21"/"20" (reduced via `regex_findall('[0-9]+')`). A NEW `_norm` name
is required because a `key=value` extra-var has the HIGHEST variable precedence
and would silently override a `set_fact` that reused the same name — so the
served-set computation and the teardown loop must consume the `_norm` facts.

WHAT THIS FILE GUARDS (pure PyYAML structure parse of the committed role — no
`ansible-playbook`, no live Proxmox; always runs, no requires-infra gate):
  * the normalization `set_fact` exists and produces BOTH `_norm` facts using the
    string-safe `regex_findall('[0-9]+')` coercion;
  * the teardown loop iterates the NORMALIZED decommissioned fact (not the raw,
    string-coercible caller var) — the exact line whose regression reintroduces
    the phantom-VLAN bug;
  * the served-VLAN-set computation consumes the NORMALIZED active fact;
  * the normalization task PRECEDES both consumers in document order.

It never embeds or prints a secret (there is none in this role). Mirrors the
repo drift-guard style (helpers copied locally, repo root via `parents[2]`).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_sdn_gateway_vlan_set_normalization.py -> repo root = parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_MAIN = _REPO_ROOT / "ansible" / "roles" / "sdn_gateway" / "tasks" / "main.yml"

_ACTIVE_RAW = "sdn_gateway_active_vlan_ids"
_DECOMM_RAW = "sdn_gateway_decommissioned_vlan_ids"
_ACTIVE_NORM = "sdn_gateway_active_vlan_ids_norm"
_DECOMM_NORM = "sdn_gateway_decommissioned_vlan_ids_norm"


def _load_tasks(path: Path) -> list[dict]:
    """Flat, document-ordered task list of the role's tasks/main.yml."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(t for t in doc if isinstance(t, dict))
        elif isinstance(doc, dict):
            tasks.append(doc)
    return tasks


def _set_fact_body(task: dict, fact_name: str) -> str | None:
    spec = task.get("ansible.builtin.set_fact") or task.get("set_fact")
    if not isinstance(spec, dict) or fact_name not in spec:
        return None
    return str(spec[fact_name])


def _index_of(tasks: list[dict], pred) -> int | None:
    return next((i for i, t in enumerate(tasks) if pred(t)), None)


def _normalization_task_index(tasks: list[dict]) -> int | None:
    """Index of the set_fact that assigns BOTH `_norm` facts."""
    return _index_of(
        tasks,
        lambda t: (
            _set_fact_body(t, _ACTIVE_NORM) is not None
            and _set_fact_body(t, _DECOMM_NORM) is not None
        ),
    )


def test_normalization_setfact_produces_both_norm_facts_string_safe():
    """The role must normalize BOTH VLAN sets into `_norm` facts, string-safe.

    Regression bite: if the normalization is removed, or stops using a
    string-safe coercion (e.g. reverts to a bare `| map('int')` that would
    iterate a "[20]" string's characters), this fails.
    """
    tasks = _load_tasks(_ROLE_MAIN)
    idx = _normalization_task_index(tasks)
    assert idx is not None, (
        "DRIFT: no `set_fact` assigns BOTH normalized VLAN-set facts "
        f"({_ACTIVE_NORM} and {_DECOMM_NORM}). The role must coerce the caller-"
        "supplied active/decommissioned sets into real integer lists so a "
        "`--extra-vars key=[20]` string does not get iterated character-by-"
        "character into phantom VLANs [0, 2]."
    )
    norm_task = tasks[idx]
    for raw, norm in ((_ACTIVE_RAW, _ACTIVE_NORM), (_DECOMM_RAW, _DECOMM_NORM)):
        body = _set_fact_body(norm_task, norm) or ""
        # String-safe coercion: extract digit runs from a string, else pass a
        # real list through. `regex_findall('[0-9]+')` is the string-safe path.
        assert "regex_findall" in body, (
            f"DRIFT: the {norm} normalization must use a string-safe coercion "
            f"(`regex_findall('[0-9]+')`) so a '{raw}' arriving as the string "
            f'"[20]" yields [20], not the character iteration [0, 2]; body={body!r}.'
        )
        assert "is not string" in body or "is string" in body, (
            f"DRIFT: the {norm} normalization must branch on whether {raw} is a "
            "string (a genuine list must pass through unchanged so bring-up "
            f"behaviour is preserved); body={body!r}."
        )


def test_teardown_loop_iterates_normalized_decommissioned_fact():
    """The teardown include-loop must iterate the NORMALIZED decommissioned fact.

    This is the exact line whose regression reintroduced the live bug: a loop
    over the RAW `sdn_gateway_decommissioned_vlan_ids` (which a `key=value`
    extra-var makes a string) tears down phantom VLANs 0/2 and never the real
    VLAN, so `sdn-gw-<vlan>.cfg` is never removed.
    """
    tasks = _load_tasks(_ROLE_MAIN)

    def _is_teardown_include(task: dict) -> bool:
        inc = task.get("ansible.builtin.include_tasks") or task.get("include_tasks")
        return isinstance(inc, str) and inc.strip() == "teardown.yml"

    teardown_tasks = [t for t in tasks if _is_teardown_include(t)]
    assert teardown_tasks, "DRIFT: no `include_tasks: teardown.yml` loop found."
    loop_expr = str(teardown_tasks[0].get("loop", ""))
    assert _DECOMM_NORM in loop_expr, (
        "DRIFT: the teardown loop must iterate the NORMALIZED decommissioned "
        f"fact `{_DECOMM_NORM}`, not the raw `{_DECOMM_RAW}` (a `--extra-vars "
        'key=[20]` makes the raw var the string "[20]", iterated as chars into '
        f"phantom VLANs [0, 2]); loop is {loop_expr!r}."
    )
    # And it must NOT re-derive from the raw var (which would re-open the bug).
    assert f"{_DECOMM_RAW} " not in f"{loop_expr} " or _DECOMM_NORM in loop_expr, (
        "DRIFT: the teardown loop must consume ONLY the normalized fact."
    )


def test_served_set_computation_consumes_normalized_active_fact():
    """The served-VLAN-set computation must consume the NORMALIZED active fact."""
    tasks = _load_tasks(_ROLE_MAIN)
    served_tasks = [
        t for t in tasks if _set_fact_body(t, "sdn_gateway_served_vlan_ids") is not None
    ]
    assert served_tasks, (
        "DRIFT: no `set_fact` computing `sdn_gateway_served_vlan_ids` found."
    )
    body = _set_fact_body(served_tasks[0], "sdn_gateway_served_vlan_ids") or ""
    assert _ACTIVE_NORM in body, (
        "DRIFT: the served-VLAN-set computation must intersect against the "
        f"NORMALIZED active fact `{_ACTIVE_NORM}`, not the raw string-coercible "
        f"`{_ACTIVE_RAW}`; body={body!r}."
    )


def test_served_set_subtracts_decommissioned_vlans():
    """The served (configure) set must SUBTRACT the decommissioned set.

    The shared VLAN (20) is otherwise served UNCONDITIONALLY, so a clean-slate
    that decommissions it (active=[], decommissioned=[20]) would land VLAN 20 in
    BOTH the configure set AND the teardown set. configure.yml runs first and
    hard-fails at `ip addr show dev p<vlan>` once the SDN bridge is already gone
    (exactly the clean-slate ordering, where `terraform destroy` precedes the
    gateway teardown) — aborting before teardown.yml removes `sdn-gw-<vlan>.cfg`.
    Subtracting the decommissioned set means a VLAN being torn down is never
    re-configured in the same run. Regression bite: drop the `difference(...)`
    and this fails.
    """
    tasks = _load_tasks(_ROLE_MAIN)
    served_tasks = [
        t for t in tasks if _set_fact_body(t, "sdn_gateway_served_vlan_ids") is not None
    ]
    assert served_tasks, (
        "DRIFT: no `set_fact` computing `sdn_gateway_served_vlan_ids` found."
    )
    body = _set_fact_body(served_tasks[0], "sdn_gateway_served_vlan_ids") or ""
    assert "difference" in body and _DECOMM_NORM in body, (
        "DRIFT: the served-VLAN-set computation must subtract the NORMALIZED "
        f"decommissioned set (`| difference({_DECOMM_NORM})`) so a VLAN being "
        "decommissioned (even the always-served shared VLAN 20) is NOT "
        "re-configured in the same run — otherwise configure.yml hard-fails on "
        f"the already-gone bridge before teardown runs; body={body!r}."
    )


def test_normalization_precedes_both_consumers():
    """The normalization set_fact must PRECEDE the served-set + teardown consumers.

    Ordering matters: a `_norm` fact referenced before it is set would be
    undefined. This pins the normalization ahead of both consumers in the
    role's document (execution) order.
    """
    tasks = _load_tasks(_ROLE_MAIN)
    norm_idx = _normalization_task_index(tasks)
    assert norm_idx is not None, "DRIFT: normalization set_fact missing (see other test)."

    served_idx = _index_of(
        tasks, lambda t: _set_fact_body(t, "sdn_gateway_served_vlan_ids") is not None
    )
    teardown_idx = _index_of(
        tasks,
        lambda t: (
            isinstance(
                (t.get("ansible.builtin.include_tasks") or t.get("include_tasks")), str
            )
            and (t.get("ansible.builtin.include_tasks") or t.get("include_tasks")).strip()
            == "teardown.yml"
        ),
    )
    assert served_idx is not None and teardown_idx is not None, (
        "DRIFT: could not locate the served-set computation and/or the teardown "
        "loop to verify normalization ordering."
    )
    assert norm_idx < served_idx, (
        "DRIFT: the VLAN-set normalization must precede the served-set "
        f"computation (norm at {norm_idx}, served at {served_idx})."
    )
    assert norm_idx < teardown_idx, (
        "DRIFT: the VLAN-set normalization must precede the teardown loop "
        f"(norm at {norm_idx}, teardown at {teardown_idx})."
    )
