"""Offline structural guard for the workload-guest AppRole cred-staging bugfix.

Feature: fix-workload-guest-creds-staging.

BUG: on a DEDICATED workload guest the openbao-agent sidecar bind-mounts its
AppRole role-id/secret-id READ-ONLY from host paths on the GUEST
(``openbao_agent_role_id_host_path`` / ``openbao_agent_secret_id_host_path``,
default ``/etc/openbao-workload/<slug>/<slug>-workload-{role,secret}-id``). But the
control-plane ``openbao_workload_approle`` role (composed by ``consumer_bootstrap``
STEP 3) hands the minted 0600 creds to the CONTROL NODE
(``$HOME/.openbao-workload/<slug>``), never the guest. Nothing in
``hello_workload_deploy`` stages them onto the guest, so Docker auto-creates empty
DIRECTORIES at the missing mount sources and the Agent reads a directory where a
file must be (``read /openbao/agent/role-id: is a directory``) -> auth crash-loop
-> workload never healthy.

FIX (Option 1, hello_workload_deploy-only, additive): a new STEP 2 in
``hello_workload_deploy/tasks/main.yml`` — BEFORE it composes ``openbao-agent`` —
reads the control-node 0600 creds and copies them onto the GUEST at exactly the two
agent host paths (0600 root:root, parent dir 0700 root:root), failing closed if the
source is absent, with ``no_log`` on the secret_id copy.

Pure PyYAML/text parse — no infrastructure.

Property 1 (Bug Condition) = tests that FAIL on unfixed code (no staging), PASS
after the fix. Property 2 (Preservation) = tests that PASS on unfixed code AND
after the fix (composition / mount paths / handoff / hygiene unchanged).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"

_DEPLOY_TASKS = _ROLES / "hello_workload_deploy" / "tasks" / "main.yml"
_DEPLOY_DEFAULTS = _ROLES / "hello_workload_deploy" / "defaults" / "main.yml"
_AGENT_DEFAULTS = _ROLES / "openbao-agent" / "defaults" / "main.yml"
_CB_DEFAULTS = _ROLES / "consumer_bootstrap" / "defaults" / "main.yml"

# The exact variable names the openbao-agent sidecar mounts role-id/secret-id
# from (read-only). The staged copy destinations MUST be these — source and
# destination provably the same paths.
_ROLE_ID_HOST_PATH_VAR = "openbao_agent_role_id_host_path"
_SECRET_ID_HOST_PATH_VAR = "openbao_agent_secret_id_host_path"


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #
def _load_tasks(path: Path) -> list:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _load_defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _iter_flat(tasks: list):
    """Yield (index, task) for every task dict, descending into block/rescue/always.

    Index is a float encoding position so "before the agent include" ordering is
    well-defined even for nested tasks (nested tasks inherit their parent's
    top-level index — adequate here because the staging step is top-level).
    """
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        yield i, task
        for nest in ("block", "rescue", "always"):
            if isinstance(task.get(nest), list):
                for j, sub in enumerate(task[nest]):
                    if isinstance(sub, dict):
                        yield i + (j + 1) / 1000.0, sub


def _openbao_agent_include_index(tasks: list) -> float:
    """Top-level index of the `include_role: openbao-agent` task."""
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        inc = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(inc, dict) and inc.get("name") == "openbao-agent":
            return float(i)
    raise AssertionError(
        "hello_workload_deploy must still compose the openbao-agent role "
        "(include_role: openbao-agent not found)"
    )


def _copy_tasks(tasks: list):
    """Yield (index, copy_module_dict) for every ansible.builtin.copy / copy task."""
    for idx, task in _iter_flat(tasks):
        cp = task.get("ansible.builtin.copy") or task.get("copy")
        if isinstance(cp, dict):
            yield idx, task, cp


def _dest_references_var(copy_dict: dict, var_name: str) -> bool:
    """True if the copy `dest` Jinja references the given host-path variable."""
    return var_name in str(copy_dict.get("dest", ""))


# The staging copies are identified by their SOURCE path var (unchanged by the
# fix-workload-creds-dest-path-scope fix, and unambiguous) rather than by the
# DEST var name. Before that fix the dest referenced the agent-defaults-only
# host-path vars (the bug); after it the dest references this role's own in-scope
# `hello_workload_*_dest_path` vars. Matching on src keeps these prior-spec
# hygiene assertions correct across the fix.
_ROLE_ID_SRC_PATH_VAR = "hello_workload_role_id_src_path"
_SECRET_ID_SRC_PATH_VAR = "hello_workload_secret_id_src_path"


def _src_references_var(copy_dict: dict, var_name: str) -> bool:
    """True if the copy `src` Jinja references the given source-path variable."""
    return var_name in str(copy_dict.get("src", ""))


# --------------------------------------------------------------------------- #
# Property 1 — BUG CONDITION. These FAIL on unfixed code (no staging), PASS
# after the fix. (Requirements 1.1/1.2 current defect; 2.1-2.4 expected.)
# --------------------------------------------------------------------------- #
def test_role_id_staged_onto_guest_before_agent_include():
    """Req 2.1/2.2: a `copy` task stages the role_id onto the guest, BEFORE the
    openbao-agent include. Identified by its SOURCE (hello_workload_role_id_src_path),
    which the fix-workload-creds-dest-path-scope fix leaves unchanged.

    FAILS on unfixed code — no such copy task exists (bug 1.1/1.2)."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    agent_idx = _openbao_agent_include_index(tasks)
    staged = [
        idx
        for (idx, _t, cp) in _copy_tasks(tasks)
        if _src_references_var(cp, _ROLE_ID_SRC_PATH_VAR)
    ]
    assert staged, (
        "hello_workload_deploy must stage the role_id onto the guest via a copy "
        f"task whose src is {{{{ {_ROLE_ID_SRC_PATH_VAR} }}}}. None found (the bug: "
        "nothing stages the control-node creds onto the guest)."
    )
    assert any(idx < agent_idx for idx in staged), (
        "the role_id staging must run BEFORE the openbao-agent include (the "
        "sidecar mounts the file at compose time)."
    )


def test_secret_id_staged_onto_guest_before_agent_include():
    """Req 2.1/2.2: a `copy` task stages the secret_id onto the guest, BEFORE the
    openbao-agent include. Identified by its SOURCE
    (hello_workload_secret_id_src_path), unchanged by the dest-path-scope fix.

    FAILS on unfixed code — no such copy task exists (bug 1.1/1.2)."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    agent_idx = _openbao_agent_include_index(tasks)
    staged = [
        idx
        for (idx, _t, cp) in _copy_tasks(tasks)
        if _src_references_var(cp, _SECRET_ID_SRC_PATH_VAR)
    ]
    assert staged, (
        "hello_workload_deploy must stage the secret_id onto the guest via a copy "
        f"task whose src is {{{{ {_SECRET_ID_SRC_PATH_VAR} }}}}. None found (the bug)."
    )
    assert any(idx < agent_idx for idx in staged), (
        "the secret_id staging must run BEFORE the openbao-agent include."
    )


def test_staged_creds_are_0600_root_owned():
    """Req 2.1/2.4: both staging copies land 0600 root:root (the secret_id is
    sensitive; the role_id is non-secret but staged at the same perms). Staging
    copies identified by their SOURCE path vars (unchanged by the fix).

    FAILS on unfixed code — the staging tasks do not exist."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    staged = [
        cp
        for (_idx, _t, cp) in _copy_tasks(tasks)
        if _src_references_var(cp, _ROLE_ID_SRC_PATH_VAR)
        or _src_references_var(cp, _SECRET_ID_SRC_PATH_VAR)
    ]
    assert len(staged) >= 2, (
        f"expected both role_id and secret_id staging copies; found {len(staged)}"
    )
    for cp in staged:
        assert str(cp.get("mode")) in ("0600", "600"), (
            f"staged cred must be mode 0600; got {cp.get('mode')!r}"
        )
        assert str(cp.get("owner")) == "root", (
            f"staged cred must be owner root; got {cp.get('owner')!r}"
        )
        assert str(cp.get("group")) == "root", (
            f"staged cred must be group root; got {cp.get('group')!r}"
        )


def test_secret_id_staging_carries_no_log_from_openbao_no_log():
    """Req 2.4: the secret_id staging task carries `no_log` resolved from the
    repo's `openbao_no_log` convention (default true), so the value never echoes.
    Identified by its SOURCE path var (unchanged by the fix).

    FAILS on unfixed code — the task does not exist."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    secret_copies = [
        task
        for (_idx, task, cp) in _copy_tasks(tasks)
        if _src_references_var(cp, _SECRET_ID_SRC_PATH_VAR)
    ]
    assert secret_copies, "no secret_id staging copy task found (the bug)"
    for task in secret_copies:
        assert "no_log" in task, (
            "the secret_id staging task must carry no_log (Req 2.4 / security-standards)"
        )
        assert "openbao_no_log" in str(task["no_log"]), (
            "no_log must resolve from the openbao_no_log convention (default true); "
            f"got {task['no_log']!r}"
        )


def test_fail_closed_guard_precedes_agent_include():
    """Req 2.3: the role fails closed if the control-node source creds are absent,
    BEFORE composing openbao-agent, with a remedy pointing at the consumer bootstrap.

    FAILS on unfixed code — no such guard exists."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    agent_idx = _openbao_agent_include_index(tasks)
    found_assert_before_agent = False
    remedy_seen = False
    for idx, task in _iter_flat(tasks):
        if idx >= agent_idx:
            continue
        a = task.get("ansible.builtin.assert") or task.get("assert")
        if isinstance(a, dict):
            found_assert_before_agent = True
            blob = str(a.get("fail_msg", "")) + str(a.get("that", ""))
            if re.search(r"consumer\s+bootstrap|bootstrap.*(slug|workload)", blob, re.IGNORECASE):
                remedy_seen = True
    assert found_assert_before_agent, (
        "a fail-closed assert (on the source cred paths) must precede the "
        "openbao-agent include (Req 2.3)."
    )
    assert remedy_seen, (
        "the fail-closed assert must name a remedy pointing at the consumer "
        "bootstrap (Req 2.3)."
    )


def test_source_dir_default_matches_consumer_handoff_location():
    """Req 2.1/3.5: the overridable source-dir default resolves under
    $HOME/.openbao-workload/<slug> with the <slug>-workload-{role,secret}-id
    filenames — exactly where consumer_bootstrap wrote the creds.

    FAILS on unfixed code — the vars do not exist."""
    defaults = _load_defaults(_DEPLOY_DEFAULTS)
    src_dir = str(defaults.get("hello_workload_creds_src_dir", ""))
    assert src_dir, (
        "hello_workload_deploy must define an overridable hello_workload_creds_src_dir "
        "(Req 2.1/3.5)"
    )
    assert "env" in src_dir and "HOME" in src_dir, (
        f"the source dir default must be $HOME-anchored (got {src_dir!r}) — where "
        "consumer_bootstrap wrote the creds"
    )
    assert ".openbao-workload" in src_dir, (
        f"the source dir default must be the ~/.openbao-workload path; got {src_dir!r}"
    )
    role_src = str(defaults.get("hello_workload_role_id_src_path", ""))
    secret_src = str(defaults.get("hello_workload_secret_id_src_path", ""))
    assert "workload-role-id" in role_src, (
        f"role_id src filename must match <slug>-workload-role-id; got {role_src!r}"
    )
    assert "workload-secret-id" in secret_src, (
        f"secret_id src filename must match <slug>-workload-secret-id; got {secret_src!r}"
    )


# --------------------------------------------------------------------------- #
# Property 2 — PRESERVATION. These PASS on unfixed code AND after the fix.
# (Requirements 3.1-3.5.)
# --------------------------------------------------------------------------- #
def test_openbao_agent_still_composed_with_reference_var_set():
    """Req 3.1: the deploy role still composes openbao-agent threading the same
    reference var set (secret mappings, image, entrypoint mount, non-secret env,
    healthcheck). PASS on unfixed code; must stay PASS after the fix."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    inc_vars = None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        inc = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(inc, dict) and inc.get("name") == "openbao-agent":
            inc_vars = task.get("vars") or {}
            break
    assert inc_vars is not None, "openbao-agent must still be composed"
    blob = str(inc_vars)
    for ref in (
        "hello_workload_secret_mappings",
        "hello_workload_image",
        "hello_workload_env",
        "hello_workload_healthcheck",
    ):
        assert ref in blob, (
            f"the openbao-agent composition must still thread {ref} (Req 3.1); got {inc_vars!r}"
        )


def test_agent_mount_paths_unchanged():
    """Req 3.2: the openbao-agent role still mounts role-id/secret-id from the
    unchanged host-path vars. PASS on unfixed code (we do not modify the role)."""
    defaults = _load_defaults(_AGENT_DEFAULTS)
    role_path = str(defaults.get(_ROLE_ID_HOST_PATH_VAR, ""))
    secret_path = str(defaults.get(_SECRET_ID_HOST_PATH_VAR, ""))
    assert "/etc/openbao-workload/" in role_path and "workload-role-id" in role_path, (
        f"{_ROLE_ID_HOST_PATH_VAR} must be unchanged; got {role_path!r}"
    )
    assert "/etc/openbao-workload/" in secret_path and "workload-secret-id" in secret_path, (
        f"{_SECRET_ID_HOST_PATH_VAR} must be unchanged; got {secret_path!r}"
    )


def test_control_plane_handoff_write_dir_unchanged():
    """Req 3.3: consumer_bootstrap still writes the handoff creds to the control
    node ($HOME/.openbao-workload/<slug>) — the fix reads from there, it does not
    move the write. PASS on unfixed code."""
    defaults = _load_defaults(_CB_DEFAULTS)
    val = str(defaults.get("consumer_workload_creds_dir", ""))
    assert "env" in val and "HOME" in val and ".openbao-workload" in val, (
        f"consumer_workload_creds_dir handoff location must be unchanged; got {val!r}"
    )


def test_no_secret_value_committed_or_echoed_in_deploy_role():
    """Req 3.4: no literal secret value is committed, and no debug echoes a
    secret value. PASS on unfixed code; must stay PASS after the fix (the staged
    value crosses control-node->guest over SSH only, referenced by path/var)."""
    deploy_tasks = _load_tasks(_DEPLOY_TASKS)
    # No hardcoded no_log: false anywhere in the deploy role.
    for _idx, task in _iter_flat(deploy_tasks):
        if "no_log" in task:
            assert task["no_log"] not in (False, "false", "no"), (
                f"a task hardcodes no_log off: {task.get('name')!r} (Req 3.4)"
            )
    # No debug interpolates a secret value.
    forbidden = re.compile(r"\.Data\.data\.|role_id\s*}}|secret_id\s*}}|root_token", re.IGNORECASE)
    for _idx, task in _iter_flat(deploy_tasks):
        dbg = task.get("ansible.builtin.debug") or task.get("debug")
        if isinstance(dbg, dict):
            msg = str(dbg.get("msg", "")) + str(dbg.get("var", ""))
            assert not forbidden.search(msg), (
                f"a debug task interpolates a secret value: {msg!r} (Req 3.4)"
            )
    # No literal 37-char-ish uuid-looking assignment of a role/secret id value.
    text = _DEPLOY_TASKS.read_text(encoding="utf-8") + _DEPLOY_DEFAULTS.read_text(encoding="utf-8")
    assert not re.search(r"(role_id|secret_id)\s*:\s*[0-9a-f]{8}-[0-9a-f]{4}", text, re.IGNORECASE), (
        "no literal role_id/secret_id value may be committed (Req 3.4)"
    )


def test_cred_staging_only_in_hello_workload_deploy():
    """Req 3.1/3.4 (cross-caller preservation): the cred-staging copy tasks that
    target the agent host paths appear ONLY in hello_workload_deploy, not in any
    other openbao-agent caller role. PASS on unfixed code (no staging anywhere);
    after the fix it must remain confined to hello_workload_deploy."""
    other_callers = [
        _ROLES / "postgres-server" / "tasks" / "main.yml",
        _ROLES / "zitadel-server" / "tasks" / "main.yml",
    ]
    for path in other_callers:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert _ROLE_ID_HOST_PATH_VAR not in text and _SECRET_ID_HOST_PATH_VAR not in text, (
            f"{path.relative_to(_REPO_ROOT)} must NOT stage agent creds — the fix "
            "is confined to hello_workload_deploy (Req 3.1/3.4)"
        )


# =========================================================================== #
# fix-workload-creds-dest-path-scope
#
# The staging STEP 2 added by fix-workload-guest-creds-staging referenced the
# DESTINATION paths via `openbao_agent_role_id_host_path` /
# `openbao_agent_secret_id_host_path` — variables defined ONLY in the
# `openbao-agent` role's defaults, which do not enter scope until the STEP 3
# `include_role: openbao-agent`. At STEP 2 they are undefined, so a live run
# fails hard ("'openbao_agent_role_id_host_path' is undefined", failed=1).
#
# The tests above string-MATCH the var NAME in the `dest:` value, so they passed
# on the broken code (the gap). The tests below assert variable SCOPE: every
# Jinja var used in a STEP 2 file:/copy: arg BEFORE the agent include must be
# resolvable from THIS role's own defaults (or be project_slug / a documented
# play var), and must NOT be an `openbao_agent_*` name that exists only in the
# agent role's defaults.
#
# Property 1 (Bug Condition) = tests that FAIL on the current (broken) code and
# PASS after the fix. Property 2 (Preservation) = the canonical-value parity
# test, which PASSES both before and after.
# =========================================================================== #

# These `openbao_agent_*` host-path names are defined ONLY in the openbao-agent
# role's defaults; referencing them before the include is the bug.
_AGENT_DEFAULTS_ONLY_HOST_PATH_VARS = frozenset(
    {_ROLE_ID_HOST_PATH_VAR, _SECRET_ID_HOST_PATH_VAR}
)

# The dest-path vars this role must define in its OWN defaults so they are in
# scope at STEP 2.
_DEPLOY_ROLE_ID_DEST_VAR = "hello_workload_role_id_dest_path"
_DEPLOY_SECRET_ID_DEST_VAR = "hello_workload_secret_id_dest_path"

_JINJA_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")


def _jinja_var_names(value) -> set:
    """Collect the leading identifier of every `{{ var ... }}` in a string."""
    return set(_JINJA_VAR_RE.findall(str(value)))


def _file_module(task: dict):
    return task.get("ansible.builtin.file") or task.get("file")


def _vars_used_in_file_copy_args_before_agent_include(tasks: list) -> set:
    """Every Jinja var name used in file:/copy: task args BEFORE the agent include."""
    agent_idx = _openbao_agent_include_index(tasks)
    used: set = set()
    for idx, task in _iter_flat(tasks):
        if idx >= agent_idx:
            continue
        for mod in (_file_module(task), task.get("ansible.builtin.copy"), task.get("copy")):
            if isinstance(mod, dict):
                for arg_val in mod.values():
                    used |= _jinja_var_names(arg_val)
    return used


def _deploy_defaults_keys() -> set:
    return set(_load_defaults(_DEPLOY_DEFAULTS).keys())


# --------------------------------------------------------------------------- #
# Property 1 — BUG CONDITION (SCOPE). FAIL on broken code, PASS after fix.
# --------------------------------------------------------------------------- #
def test_step2_file_copy_vars_are_in_this_roles_scope():
    """Req 2.1/2.3: every Jinja var used in a STEP 2 file:/copy: arg BEFORE the
    openbao-agent include must be resolvable from hello_workload_deploy's OWN
    scope — defined in this role's defaults, or `project_slug` / a documented
    play var. It must NOT be an `openbao_agent_*` name that exists only in the
    agent role's defaults (undefined at STEP 2).

    FAILS on broken code: STEP 2 references openbao_agent_{role,secret}_id_host_path."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    used = _vars_used_in_file_copy_args_before_agent_include(tasks)

    offending = used & _AGENT_DEFAULTS_ONLY_HOST_PATH_VARS
    assert not offending, (
        "STEP 2 file:/copy: task args reference openbao-agent-defaults-only "
        f"variable(s) {sorted(offending)} BEFORE the `include_role: openbao-agent` "
        "line. Those vars are defined only in openbao-agent/defaults/main.yml and "
        "are UNDEFINED at STEP 2 (the include is STEP 3) -> live run fails "
        "'... is undefined'. STEP 2 must use this role's own in-scope dest-path "
        "vars instead."
    )

    # Stronger, general form: nothing used before the include may be a name that
    # is ONLY in the agent defaults and not in this role's own defaults.
    deploy_keys = _deploy_defaults_keys()
    agent_keys = set(_load_defaults(_AGENT_DEFAULTS).keys())
    documented_play_vars = {"project_slug"}
    for var in used:
        if var in deploy_keys or var in documented_play_vars:
            continue
        assert var not in agent_keys, (
            f"STEP 2 references {var!r}, which is defined only in "
            "openbao-agent/defaults and is out of scope before the include "
            "(Req 2.1/2.3)."
        )


def test_step2_dest_comes_from_this_roles_own_defaults():
    """Req 2.1: the STEP 2 role_id/secret_id copy `dest:` values reference
    hello_workload_role_id_dest_path / hello_workload_secret_id_dest_path, and
    both are defined in hello_workload_deploy's own defaults.

    FAILS on broken code: dest uses openbao_agent_*_host_path; the dest vars
    do not exist in this role's defaults."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    deploy_defaults = _load_defaults(_DEPLOY_DEFAULTS)

    role_dest = None
    secret_dest = None
    for _idx, _task, cp in _copy_tasks(tasks):
        dest = str(cp.get("dest", ""))
        if _DEPLOY_ROLE_ID_DEST_VAR in dest:
            role_dest = dest
        if _DEPLOY_SECRET_ID_DEST_VAR in dest:
            secret_dest = dest

    assert role_dest is not None, (
        f"a STEP 2 copy `dest:` must reference {{{{ {_DEPLOY_ROLE_ID_DEST_VAR} }}}} "
        "(this role's own in-scope dest var), not the agent-defaults-only host path."
    )
    assert secret_dest is not None, (
        f"a STEP 2 copy `dest:` must reference {{{{ {_DEPLOY_SECRET_ID_DEST_VAR} }}}}."
    )
    assert _DEPLOY_ROLE_ID_DEST_VAR in deploy_defaults, (
        f"{_DEPLOY_ROLE_ID_DEST_VAR} must be defined in "
        "hello_workload_deploy/defaults/main.yml so it is in scope at STEP 2."
    )
    assert _DEPLOY_SECRET_ID_DEST_VAR in deploy_defaults, (
        f"{_DEPLOY_SECRET_ID_DEST_VAR} must be defined in "
        "hello_workload_deploy/defaults/main.yml so it is in scope at STEP 2."
    )

    # The guest-cred-dir `file:` path must also come from the in-scope dest var.
    dir_paths = []
    agent_idx = _openbao_agent_include_index(tasks)
    for idx, task in _iter_flat(tasks):
        if idx >= agent_idx:
            continue
        fm = _file_module(task)
        if isinstance(fm, dict) and fm.get("state") == "directory":
            dir_paths.append(str(fm.get("path", "")))
    assert any(_DEPLOY_ROLE_ID_DEST_VAR in p or _DEPLOY_SECRET_ID_DEST_VAR in p for p in dir_paths), (
        "the STEP 2 guest-cred-dir `file:` path must derive from an in-scope "
        f"dest var (e.g. {{{{ {_DEPLOY_ROLE_ID_DEST_VAR} | dirname }}}}); got {dir_paths!r}"
    )


def test_step3_threads_agent_host_paths_from_dest_vars():
    """Req 2.2: the STEP 3 `include_role: openbao-agent` vars block threads
    openbao_agent_role_id_host_path / openbao_agent_secret_id_host_path from the
    hello_workload_*_dest_path vars, so what STEP 2 stages is exactly what the
    agent mounts (no drift).

    FAILS on broken code: the include does not thread the host paths at all."""
    tasks = _load_tasks(_DEPLOY_TASKS)
    inc_vars = None
    for task in tasks:
        if not isinstance(task, dict):
            continue
        inc = task.get("ansible.builtin.include_role") or task.get("include_role")
        if isinstance(inc, dict) and inc.get("name") == "openbao-agent":
            inc_vars = task.get("vars") or {}
            break
    assert inc_vars is not None, "openbao-agent must still be composed"

    assert _ROLE_ID_HOST_PATH_VAR in inc_vars, (
        f"the openbao-agent include must thread {_ROLE_ID_HOST_PATH_VAR} (Req 2.2)"
    )
    assert _SECRET_ID_HOST_PATH_VAR in inc_vars, (
        f"the openbao-agent include must thread {_SECRET_ID_HOST_PATH_VAR} (Req 2.2)"
    )
    assert _DEPLOY_ROLE_ID_DEST_VAR in str(inc_vars[_ROLE_ID_HOST_PATH_VAR]), (
        f"{_ROLE_ID_HOST_PATH_VAR} must be threaded from {_DEPLOY_ROLE_ID_DEST_VAR} "
        f"(staged == mounted); got {inc_vars[_ROLE_ID_HOST_PATH_VAR]!r}"
    )
    assert _DEPLOY_SECRET_ID_DEST_VAR in str(inc_vars[_SECRET_ID_HOST_PATH_VAR]), (
        f"{_SECRET_ID_HOST_PATH_VAR} must be threaded from {_DEPLOY_SECRET_ID_DEST_VAR} "
        f"(staged == mounted); got {inc_vars[_SECRET_ID_HOST_PATH_VAR]!r}"
    )


# --------------------------------------------------------------------------- #
# Property 2 — PRESERVATION. PASS on broken code AND after the fix.
# --------------------------------------------------------------------------- #
def test_canonical_dest_value_matches_agent_default():
    """Req 3.4: the canonical destination VALUE is byte-identical to the
    openbao-agent default — the fix changes WHERE the var is defined, not the
    value it computes. The agent default canonical shape is unchanged.

    PASSES on broken code (agent default already canonical; this role's dest
    vars may be absent pre-fix) AND after the fix (both resolve to the same
    /etc/openbao-workload/<slug>/<slug>-workload-{role,secret}-id value)."""
    agent_defaults = _load_defaults(_AGENT_DEFAULTS)
    deploy_defaults = _load_defaults(_DEPLOY_DEFAULTS)

    def _canon(expr: str) -> str:
        # Normalise whitespace (YAML folded scalars insert newlines/spaces) and
        # collapse the project_slug Jinja so two equivalent expressions compare.
        s = re.sub(r"\s+", "", str(expr))
        s = s.replace("{{project_slug}}", "<slug>")
        return s

    agent_role = _canon(agent_defaults.get(_ROLE_ID_HOST_PATH_VAR, ""))
    agent_secret = _canon(agent_defaults.get(_SECRET_ID_HOST_PATH_VAR, ""))

    # The agent default canonical shape is unchanged (preservation of 3.1).
    assert agent_role == "/etc/openbao-workload/<slug>/<slug>-workload-role-id", (
        f"openbao-agent role_id host-path default must be the canonical value; got {agent_role!r}"
    )
    assert agent_secret == "/etc/openbao-workload/<slug>/<slug>-workload-secret-id", (
        f"openbao-agent secret_id host-path default must be the canonical value; got {agent_secret!r}"
    )

    # When this role defines its own dest vars (after the fix), they must compute
    # the SAME canonical value. Pre-fix the vars are absent and this is skipped —
    # keeping the test green on the broken code while still asserting parity once
    # the fix lands.
    if _DEPLOY_ROLE_ID_DEST_VAR in deploy_defaults:
        assert _canon(deploy_defaults[_DEPLOY_ROLE_ID_DEST_VAR]) == agent_role, (
            f"{_DEPLOY_ROLE_ID_DEST_VAR} must compute the same canonical value as the "
            "openbao-agent default (the fix changes scope, not value)"
        )
    if _DEPLOY_SECRET_ID_DEST_VAR in deploy_defaults:
        assert _canon(deploy_defaults[_DEPLOY_SECRET_ID_DEST_VAR]) == agent_secret, (
            f"{_DEPLOY_SECRET_ID_DEST_VAR} must compute the same canonical value as the "
            "openbao-agent default"
        )
