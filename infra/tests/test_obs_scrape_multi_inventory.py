"""Offline bug-condition exploration + configurator-wiring-contract test for the
SVC-17/18 observability scrape-target multi-inventory discovery bugfix.

Spec: .kiro/specs/fix-obs-scrape-targets-multi-inventory/
      (bugfix.md, design.md, tasks.md).

Root cause (captured live on the throwaway cluster): the orchestrated one-command
SVC-17/18 bring-up converges `failed=0` with Prometheus healthy, yet Prometheus
scrapes ONLY itself and `/etc/prometheus/targets/` is empty. The scrape-target
index (observability_stack) discovers the V1_Scrape_Set (SVC-01 postgres / SVC-06
zitadel / SVC-07 openbao / SVC-09 traefik) by reading controller-side
`groups['postgres'|'zitadel'|'openbao'|'traefik']`, but the obs configurator child
run (obs_bootstrap/tasks/main.yml Task 2 -> observability.yml) is invoked with a
SINGLE `-i "{{ obs_generated_inventory }}"` (only
svc-17-18-observability.generated.yml). The four service_role groups live only in
the OTHER services' generated inventories, so they are absent, the index resolves
EMPTY, the degrade filter drops every row, and zero target files render.

This module encodes the expected configurator-wiring contract so the regression
cannot silently return:

  Property 1 (Bug Condition): the Task-2 configurator child run (the command task
    whose argv invokes ansible/playbooks/observability.yml) carries MORE than one
    inventory source -- it loads ALL `ansible/inventory/*.generated.yml` by a
    run-time glob/loop over the inventory dir, not a single
    `-i "{{ obs_generated_inventory }}"`; AND both `observability.yml` plays are
    scoped to `prometheus:grafana:loki` (not bare `hosts: all`). FAILS on the
    unfixed tree (Task 2 has exactly one inventory source `obs_generated_inventory`;
    the plays are `hosts: all`) -- that failure CONFIRMS the bug.

Pure PyYAML + text parse; no ansible-playbook binary, no live infra. Mirrors the
task-walking helpers of the sibling test
infra/tests/test_obs_bootstrap_token_bridging.py.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_obs_scrape_multi_inventory.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_PLAYBOOKS = _REPO_ROOT / "ansible" / "playbooks"
_BOOTSTRAP_TASKS = _ROLES / "obs_bootstrap" / "tasks" / "main.yml"
_OBSERVABILITY_PLAY = _PLAYBOOKS / "observability.yml"
_SCRAPE_TARGETS = _ROLES / "observability_stack" / "tasks" / "scrape_targets.yml"

_OBS_PLAY_REF = "observability.yml"
_COMMON_PLAY_REF = "common.yml"
_OBS_INV_ONLY = "obs_generated_inventory"
_CONFIGURATOR_ARGV_FACT = "obs_configurator_argv"
_INVENTORY_FLAGS_FACT = "obs_inventory_flags"
_CONFIGURATOR_INVENTORIES_FACT = "obs_configurator_inventories"
_EXPECTED_HOST_SCOPE = "prometheus:grafana:loki"
_TOKEN_VAR = "OPENBAO_ADMIN_TOKEN"
_DEGRADE_WHEN = "(_obs_row_index | length) > 0"

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop",
    "loop_control", "delegate_to", "become", "vars", "tags", "args", "block",
    "rescue", "always", "until", "retries", "delay", "environment", "check_mode",
    "notify", "listen", "ignore_errors",
}


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


def _argv_of(task: dict) -> list[str]:
    """The argv of a command/shell task, as a list of stringified elements.

    The argv may be a YAML list (literal argv elements) OR a single Jinja string
    (a computed argv fact like `{{ obs_configurator_argv }}`). A string argv is
    returned as a one-element list, NOT iterated character-by-character."""
    cmd = task.get("ansible.builtin.command") or task.get("command") or {}
    if isinstance(cmd, dict):
        argv = cmd.get("argv") or []
    else:
        argv = []
    if isinstance(argv, str):
        return [argv]
    return [str(a) for a in argv]


def _configurator_child_run_task(tasks: list[dict]) -> dict:
    """The Task-2 configurator child run: the command task whose argv invokes
    ansible/playbooks/observability.yml.

    The argv is now a computed fact `{{ obs_configurator_argv }}` (a single
    string argv value), so match on either the resolved play ref in a literal
    argv element OR an argv built from the `obs_configurator_argv` fact that the
    assemble step composes from `ansible/playbooks/observability.yml`."""
    for t in tasks:
        argv = _argv_of(t)
        if any(_OBS_PLAY_REF in a for a in argv) or any(
            _CONFIGURATOR_ARGV_FACT in a for a in argv
        ):
            return t
    raise AssertionError(
        "no obs_bootstrap command task invokes ansible/playbooks/observability.yml"
    )


def _set_fact_tasks(tasks: list[dict]) -> list[dict]:
    """All set_fact tasks (either module spelling)."""
    out: list[dict] = []
    for t in tasks:
        if "ansible.builtin.set_fact" in t or "set_fact" in t:
            out.append(t)
    return out


def _set_fact_body(task: dict) -> dict:
    body = task.get("ansible.builtin.set_fact") or task.get("set_fact") or {}
    return body if isinstance(body, dict) else {}


def _common_baseline_child_run_task(tasks: list[dict]) -> dict:
    """The Task-1 common baseline child run: the command task whose argv invokes
    ansible/playbooks/common.yml (and NOT observability.yml)."""
    for t in tasks:
        argv = _argv_of(t)
        if any(_COMMON_PLAY_REF in a for a in argv) and not any(
            _OBS_PLAY_REF in a for a in argv
        ):
            return t
    raise AssertionError(
        "no obs_bootstrap command task invokes ansible/playbooks/common.yml"
    )


def _no_log_true(t: dict) -> bool:
    return t.get("no_log") in (True, "true", "True", "yes")


def _inventory_sources(argv: list[str]) -> list[str]:
    """The inventory source arguments: each element that immediately follows a
    `-i` flag in the argv."""
    sources: list[str] = []
    for i, a in enumerate(argv):
        if a == "-i" and i + 1 < len(argv):
            sources.append(argv[i + 1])
    return sources


def _play_docs(path: Path) -> list[dict]:
    docs: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            docs.extend(p for p in doc if isinstance(p, dict) and "hosts" in p)
    return docs


# ---------------------------------------------------------------------------
# Property 1 — Bug Condition (these FAIL on the unfixed tree)
# ---------------------------------------------------------------------------

def test_configurator_child_run_fans_out_multiple_separate_inventory_flags():
    """The Task-2 configurator child run must supply MULTIPLE SEPARATE `-i
    <path>` flags, NOT a single comma-joined `-i` of inventory file paths.

    Verified LIVE: `ansible -i "a.yml,b.yml,c.yml" ...` merges NOTHING (only
    all/ungrouped appear) — Ansible does not merge comma-separated inventory FILE
    PATHS; only multiple separate `-i a.yml -i b.yml -i c.yml` flags merge the
    groups. So the previous comma-join was runtime-broken even though it looked
    like "more than one inventory". This test is tightened to CATCH the comma-join.

    The argv is now a computed fact `{{ obs_configurator_argv }}`, assembled from
    a per-inventory fan-out loop. This test therefore parses the set_fact tasks
    that build `obs_inventory_flags` (the `['-i', item]` fan-out loop over
    `obs_configurator_inventories`) and `obs_configurator_argv` (the complete
    argv concatenating those flags), and asserts:
      1. Task 2's argv IS the computed `obs_configurator_argv` fact.
      2. A loop over `obs_configurator_inventories` appends a `['-i', item]` pair
         per inventory (the fan-out — this is what produces MULTIPLE separate
         `-i` flags).
      3. `obs_configurator_argv` concatenates `obs_inventory_flags` into the argv.
      4. No comma-join of inventory paths survives anywhere in the role tasks
         (no `obs_configurator_inventories ... | join(',')` into an `-i` value).

    Unfixed tree: Task 2 pins a single comma-joined `-i` built with `| join(',')`
    (and no fan-out loop) -> FAILS on both the fan-out assertion and the
    no-comma-join assertion, confirming the bug.

    Validates: Requirements 2.1, 2.2
    """
    tasks = _load_tasks(_BOOTSTRAP_TASKS)
    task = _configurator_child_run_task(tasks)
    argv = _argv_of(task)

    # (1) Task 2's argv is the single computed argv fact.
    assert len(argv) == 1 and _CONFIGURATOR_ARGV_FACT in argv[0], (
        "the Task-2 observability.yml configurator child run must set its argv "
        f"to the single computed fact `{{{{ {_CONFIGURATOR_ARGV_FACT} }}}}` "
        "(binary + fanned-out separate `-i` flags + play + `-e` bridges), not a "
        f"literal argv list. Got argv: {argv!r}"
    )

    set_facts = _set_fact_tasks(tasks)

    # (2) A loop over obs_configurator_inventories appends ['-i', item] per entry.
    fanout = []
    for t in set_facts:
        body = _set_fact_body(t)
        if _INVENTORY_FLAGS_FACT not in body:
            continue
        value = str(body[_INVENTORY_FLAGS_FACT])
        loop_over = str(t.get("loop", ""))
        appends_i_item = ("'-i'" in value or '"-i"' in value) and "item" in value
        if (
            appends_i_item
            and _INVENTORY_FLAGS_FACT in value  # accumulating append
            and _CONFIGURATOR_INVENTORIES_FACT in loop_over
        ):
            fanout.append(t)
    assert fanout, (
        f"obs_bootstrap must build `{_INVENTORY_FLAGS_FACT}` by a loop over "
        f"`{_CONFIGURATOR_INVENTORIES_FACT}` that appends a `['-i', item]` pair "
        "per inventory — this fan-out is what yields MULTIPLE SEPARATE `-i` "
        "flags (the only form Ansible merges). A single comma-joined `-i` is "
        "runtime-broken."
    )

    # (3) obs_configurator_argv concatenates the fanned-out inventory flags.
    argv_builders = [
        t for t in set_facts if _CONFIGURATOR_ARGV_FACT in _set_fact_body(t)
    ]
    assert argv_builders, (
        f"obs_bootstrap must assemble the complete argv fact "
        f"`{_CONFIGURATOR_ARGV_FACT}` immediately before Task 2."
    )
    assert any(
        _INVENTORY_FLAGS_FACT in str(_set_fact_body(t)[_CONFIGURATOR_ARGV_FACT])
        for t in argv_builders
    ), (
        f"`{_CONFIGURATOR_ARGV_FACT}` must concatenate the fanned-out "
        f"`{_INVENTORY_FLAGS_FACT}` (the separate `-i` pairs) into the argv."
    )

    # (4) No comma-join of inventory paths survives anywhere in the role tasks.
    full_text = _BOOTSTRAP_TASKS.read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", full_text)
    assert "join(',')" not in normalized and 'join(",")' not in normalized, (
        "the runtime-broken comma-join of inventory file paths into a single "
        "`-i` (`obs_configurator_inventories ... | join(',')`) must NOT survive "
        "anywhere in obs_bootstrap/tasks/main.yml — Ansible does not merge "
        "comma-separated inventory FILE PATHS."
    )


def test_configurator_inventories_built_from_glob_with_obs_first():
    """`obs_configurator_inventories` must be built from a fileglob of
    `ansible/inventory/*.generated.yml` with the obs inventory FIRST (guaranteed
    present even if the glob misses it), de-duplicated. This is the ordered,
    merged set the fan-out loop then turns into separate `-i` flags.

    Unfixed tree already resolves this fact from the glob, so this pins the
    discovery contract the fan-out depends on.

    Validates: Requirements 2.1, 2.2
    """
    set_facts = _set_fact_tasks(_load_tasks(_BOOTSTRAP_TASKS))
    builders = [
        t for t in set_facts
        if _CONFIGURATOR_INVENTORIES_FACT in _set_fact_body(t)
    ]
    assert builders, (
        f"obs_bootstrap must resolve `{_CONFIGURATOR_INVENTORIES_FACT}` before "
        "fanning it out into `-i` flags."
    )
    value = str(_set_fact_body(builders[0])[_CONFIGURATOR_INVENTORIES_FACT])
    assert "fileglob" in value and "*.generated.yml" in value, (
        f"`{_CONFIGURATOR_INVENTORIES_FACT}` must be discovered by a run-time "
        "fileglob of `ansible/inventory/*.generated.yml` (scrape what exists), "
        f"not a hardcoded list. Got: {value!r}"
    )
    assert _OBS_INV_ONLY in value, (
        f"`{_CONFIGURATOR_INVENTORIES_FACT}` must place the obs inventory "
        f"(`{_OBS_INV_ONLY}`) FIRST so it is present even if the glob misses it."
    )


def test_observability_plays_scoped_to_prometheus_grafana_loki():
    """BOTH observability.yml plays must be scoped to
    `hosts: prometheus:grafana:loki`, so a merged multi-inventory does not
    over-apply the exporter/stack roles onto the SVC-01/06/07/09 guests.

    Unfixed tree: both plays are `hosts: all` -> FAILS, confirming the bug.

    Validates: Requirements 2.1, 2.2
    """
    plays = _play_docs(_OBSERVABILITY_PLAY)
    assert len(plays) >= 2, (
        f"expected at least two plays in {_OBSERVABILITY_PLAY.name}, "
        f"got {len(plays)}"
    )

    offenders = [
        (idx, str(p.get("hosts")))
        for idx, p in enumerate(plays, start=1)
        if str(p.get("hosts")).strip() != _EXPECTED_HOST_SCOPE
    ]
    assert not offenders, (
        "both observability.yml plays must be scoped to "
        f"`hosts: {_EXPECTED_HOST_SCOPE}` (not bare `hosts: all`), so the merged "
        "multi-inventory feeds the controller-side scrape-target index without "
        "installing the fleet exporters / observability_stack onto the "
        "SVC-01/06/07/09 guests now visible in the inventory. Offending plays "
        f"(1-indexed, hosts value): {offenders!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 — Preservation (these PASS on the unfixed tree too; the fix must
# keep them green). Observation-first: each pins a behaviour already true on the
# UNFIXED tree so the two coordinated fix changes cannot regress it.
# ---------------------------------------------------------------------------

def test_common_baseline_child_run_stays_single_obs_inventory():
    """(a) The Task-1 `common.yml` baseline child run must STILL carry a SINGLE
    `-i "{{ obs_generated_inventory }}"` (no merged glob) so the Docker/hardening
    baseline runs on exactly the three observability guests and NEVER on the
    SVC-01/06/07/09 guests. The multi-inventory fix touches ONLY Task 2.

    Holds on the unfixed tree -> PASSES now, and must keep passing after the fix.

    Validates: Requirements 3.1
    """
    task = _common_baseline_child_run_task(_load_tasks(_BOOTSTRAP_TASKS))
    argv = _argv_of(task)
    sources = _inventory_sources(argv)

    # Exactly one inventory source, and it is the obs inventory.
    assert len(sources) == 1, (
        "the Task-1 common.yml baseline child run must carry exactly ONE "
        "inventory source (the obs inventory) so the common baseline stays "
        f"obs-guests-only. Got inventory sources: {sources!r}"
    )
    assert _OBS_INV_ONLY in sources[0], (
        "the Task-1 common.yml baseline child run's single `-i` source must be "
        f"the obs inventory (`{{{{ {_OBS_INV_ONLY} }}}}`). Got: {sources[0]!r}"
    )

    # No glob/fan-out crept into Task 1.
    blob = yaml.safe_dump(task)
    globbed = ("fileglob" in blob) or ("*.generated.yml" in blob) or (
        "with_fileglob" in blob
    )
    assert not globbed, (
        "the Task-1 common.yml baseline child run must NOT acquire a merged "
        "inventory glob — the common baseline stays obs-guests-only; only the "
        "Task-2 observability.yml configurator run loads all generated "
        "inventories."
    )


def test_scrape_targets_still_drops_absent_service_role():
    """(b) `observability_stack/tasks/scrape_targets.yml` must STILL drop a
    built-service row whose service_role group is absent/empty — the degrade
    filter `when: (_obs_row_index | length) > 0` ("scrape what exists") is
    unchanged, so an absent service yields no target file, not a failure.

    Holds on the unfixed tree -> PASSES now, and must keep passing after the fix.

    Validates: Requirements 3.2
    """
    tasks = _load_tasks(_SCRAPE_TARGETS)
    degrade_tasks = [
        t for t in tasks if _DEGRADE_WHEN in str(t.get("when", ""))
    ]
    assert degrade_tasks, (
        "scrape_targets.yml must keep the degrade filter "
        f"`when: {_DEGRADE_WHEN}` on the 'resolve present built-services' step so "
        "a built service absent from every loaded inventory renders NO target "
        "file (degrade, not fail — 'scrape what exists')."
    )
    # It is specifically the row-accumulation (set_fact) task that degrades.
    accumulator = [
        t for t in degrade_tasks
        if "observability_render_targets" in yaml.safe_dump(t)
    ]
    assert accumulator, (
        "the degrade filter must gate the step that accumulates "
        "`observability_render_targets` (one row per present built service), so "
        "an absent service_role contributes no row and therefore no file."
    )


def test_configurator_child_run_preserves_token_env_bridge_and_no_log():
    """(c) The Task-2 configurator child run (argv invokes observability.yml)
    must STILL carry an `environment` dict containing OPENBAO_ADMIN_TOKEN, the
    task must STILL be `no_log: true`, and OPENBAO_ADMIN_TOKEN must NEVER appear
    on an argv `=` assignment (ENV only — secret hygiene). This mirrors the
    contract the sibling test_obs_bootstrap_token_bridging.py pins.

    Holds on the unfixed tree -> PASSES now, and must keep passing after the fix
    (the multi-inventory change touches only the `-i` argv pairs).

    Validates: Requirements 3.4
    """
    task = _configurator_child_run_task(_load_tasks(_BOOTSTRAP_TASKS))

    env = task.get("environment")
    assert isinstance(env, dict) and _TOKEN_VAR in env, (
        "the Task-2 observability.yml configurator child run must keep its "
        f"`environment` dict containing the {_TOKEN_VAR} bridge (the token "
        "travels in the child ENV, never the argv)."
    )
    assert _no_log_true(task), (
        "the Task-2 observability.yml configurator child run must stay "
        "`no_log: true` (it carries the admin token in the child ENV)."
    )

    # The token must never be interpolated onto an argv `=` assignment — it lives
    # only in the task ENV. Mirrors the sibling token-bridging argv-hygiene check.
    text = _BOOTSTRAP_TASKS.read_text(encoding="utf-8")
    assert f"-e {_TOKEN_VAR}=" not in text and f'{_TOKEN_VAR}="{{{{' not in text, (
        "obs_bootstrap main.yml must not place OPENBAO_ADMIN_TOKEN on an argv `=` "
        "assignment; the token travels only in the task ENV (no_log)."
    )
