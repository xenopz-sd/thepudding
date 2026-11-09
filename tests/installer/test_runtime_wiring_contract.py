"""Offline drift-guard: runtime-wiring contract (WS-M1).

Feature: runtime-wiring-contract-tests (WS-M1).

Spec: .kiro/specs/runtime-wiring-contract-tests/ (requirements.md FR-1..FR-6;
design.md "Check 1/2/3"). Traces to PLANNING.md Part B/F WS-M1 and its motivating
C-items: WS-C1 (TF-env missing proxmox_insecure), WS-C5/C6 (openbao-agent wiring),
and the live-caught obs_clean_slate delegate-without-connection bug.

WHY THIS FILE EXISTS (the risk it closes)
-----------------------------------------
A whole class of defects here are RUNTIME-ONLY wiring gaps: the YAML is valid,
`--syntax-check` passes, the offline suite is green, but the wiring is wrong in a
way that only surfaces when a live run connects / applies / reads a secret:

  * TF-env incompleteness (WS-C1): a `terraform` destroy env omits a TF_VAR_* the
    sibling bring-up env carries (proxmox_insecure) -> live x509/TLS failure.
  * delegate-without-connection (live-caught, obs_clean_slate): a task delegates
    to a bare IP in a `hosts: localhost` orchestrator play with no add_host and no
    inline connection vars -> the delegated command has no SSH connection; with
    `failed_when: false` it silently reports a false "already absent".
  * secret-path drift (WS-C5/C6-adjacent): a producer writes a KV path and a
    consumer reads an independently hand-written path string that can drift.

This guard adds a mechanical offline check for each class so the next such
omission fails at author time. It is REUSABLE BY GLOB: a new role/service is
covered with no edit to this file (adoption is "the guard already scans your
role", not "copy the test").

The helpers below are COPIED locally (NOT imported across test trees -- the
conftest-collision rule), mirroring test_agent_live_wiring_coverage.py (glob
discovery, repo-root via Path(__file__).resolve().parents[2], PyYAML
safe_load_all, the _iter_tasks block/rescue/always recursion, _NON_MODULE_KEYS
module detection). Pure text/YAML parse: offline, no requires_infra marker, runs
in the default offline gate. Never reads or echoes a secret VALUE -- it inspects
TF_VAR_* KEY NAMES and secret-path TEMPLATE SHAPES only.

Validates: FR-1..FR-6 / AC-1..AC-5.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests/installer/test_runtime_wiring_contract.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ROLE_TASKS_GLOB = "ansible/roles/*/tasks/*.yml"
_ROLE_DEFAULTS_GLOB = "ansible/roles/*/defaults/main.yml"
_PLAYBOOK_GLOB = "ansible/playbooks/*.yml"

# --------------------------------------------------------------------------- #
# Check-specific constants.
# --------------------------------------------------------------------------- #
# Check 2: every *_tf_env dict consumed by a provider-authenticated terraform
# command MUST carry at least these keys. proxmox_insecure is the WS-C1 key.
_REQUIRED_TF_VARS = frozenset(
    {
        "TF_VAR_proxmox_endpoint",
        "TF_VAR_proxmox_api_token",
        "TF_VAR_proxmox_insecure",
    }
)
# Non-vacuity anchors for Check 2: the apply<->destroy parity pair that
# remediated WS-C1 (both must exist and both must carry proxmox_insecure).
_TF_ENV_ANCHORS = ("platform_api_tf_env", "platform_clean_slate_tf_env")

# Check 1: inline connection vars that make a delegate target connectable.
_CONNECTION_VARS = (
    "ansible_connection",
    "ansible_user",
    "ansible_host",
    "ansible_ssh_private_key_file",
    "ansible_python_interpreter",
)

# Check 3: the canonical OpenBao secret-path prefix shape. Either a Jinja mount
# var ({{ ..._kv_mount }}) or the literal `secret`, then `/`, then the canonical
# `proj-` or `observability/` namespace. Tolerates differing Jinja var names in
# the tail (project_slug vs postgres_project_slug, etc.) by design -- this is a
# structural-prefix consistency check, NOT byte-equality (see design.md).
_SECRET_PATH_SHAPE = re.compile(r"^(?:\{\{[^}]*\}\}|secret)/(?:proj-|observability/)")
# Non-vacuity anchor for Check 3.
_SECRET_PATH_ANCHOR = "postgres_openbao_secret_path"


# --------------------------------------------------------------------------- #
# Helpers -- COPIED locally (no cross-tree import; conftest-collision rule).
# --------------------------------------------------------------------------- #
_NON_MODULE_KEYS = frozenset(
    {
        "name", "when", "register", "no_log", "changed_when", "failed_when",
        "loop", "loop_control", "delegate_to", "delegate_facts", "become",
        "vars", "tags", "args", "block", "rescue", "always", "until", "retries",
        "delay", "environment", "check_mode", "notify", "listen", "ignore_errors",
    }
)


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _iter_playbook_tasks(play: dict):
    """Yield every task dict from a play's task sections, recursing blocks."""
    for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
        block = play.get(section)
        if isinstance(block, list):
            yield from _iter_tasks(block)


def _load_task_file(path: Path) -> list[dict]:
    """Flat, document-ordered task list of a role tasks/ file."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
        elif isinstance(doc, dict):
            tasks.extend(_iter_tasks([doc]))
    return tasks


def _load_playbook_tasks(path: Path) -> list[dict]:
    """Flat, document-ordered own-task list of a playbook file."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, list):
            continue
        for play in doc:
            if isinstance(play, dict):
                tasks.extend(_iter_playbook_tasks(play))
    return tasks


def _load_defaults(path: Path) -> dict:
    """Top-level mapping of a role defaults/main.yml (or {} on non-mapping)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def _add_host_names(tasks: list[dict]) -> set[str]:
    """The set of `add_host: name:` values registered anywhere in the file."""
    names: set[str] = set()
    for task in tasks:
        spec = task.get("ansible.builtin.add_host") or task.get("add_host")
        if isinstance(spec, dict) and isinstance(spec.get("name"), str):
            names.add(spec["name"])
    return names


def _iter_role_task_files():
    for path in sorted(_REPO_ROOT.glob(_ROLE_TASKS_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


def _iter_playbook_files():
    for path in sorted(_REPO_ROOT.glob(_PLAYBOOK_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


def _iter_defaults_files():
    for path in sorted(_REPO_ROOT.glob(_ROLE_DEFAULTS_GLOB)):
        yield path.relative_to(_REPO_ROOT).as_posix(), path


# --------------------------------------------------------------------------- #
# Check 1 — classifier (used by the test AND by the bug-condition proof).
# --------------------------------------------------------------------------- #
def _delegate_class(task: dict, add_host_names: set[str]) -> str:
    """Classify a delegate_to task's connectability. Returns a PASS class name
    or "SUSPICIOUS". The caller supplies the add_host names registered in the
    SAME file (design.md Check 1, class 4 is same-file-scoped)."""
    target = str(task.get("delegate_to", "")).strip()
    if target == "localhost":
        return "localhost"
    # Runtime-resolved: inventory_hostname or a ternary fallback.
    if "inventory_hostname" in target or " if " in target or " else " in target:
        return "ternary/inventory_hostname"
    # Inline connection vars on the task.
    v = task.get("vars")
    if isinstance(v, dict) and any(cv in v for cv in _CONNECTION_VARS):
        return "inline-conn-vars"
    # add_host-registered target literal in the same file.
    if target in add_host_names:
        return "add_host-registered"
    return "SUSPICIOUS"


def _scan_delegate_tasks():
    """Yield (relfile, task_name, target, klass) for every delegate_to task in
    the role-tasks + playbook globs."""
    sources = [(rel, _load_task_file(p)) for rel, p in _iter_role_task_files()]
    sources += [(rel, _load_playbook_tasks(p)) for rel, p in _iter_playbook_files()]
    for rel, tasks in sources:
        names = _add_host_names(tasks)
        for task in tasks:
            if "delegate_to" not in task:
                continue
            klass = _delegate_class(task, names)
            yield rel, str(task.get("name") or "")[:70], str(task.get("delegate_to")), klass


def test_delegate_to_targets_are_connectable():
    """Every delegate_to target is offline-provably connectable (FR-1 / AC-1/2/3).

    Validates: FR-1. For every task with `delegate_to:` in ansible/roles/*/tasks
    and ansible/playbooks, the target must be `localhost`, runtime-resolved
    (inventory_hostname / ternary), carry inline connection vars, OR be an
    add_host-registered name in the same file. A bare IP/host-var delegate with
    none of these FAILS -- the live-caught obs_clean_slate class (a delegate with
    no working SSH connection that `failed_when:false` turns into a silent false
    'already absent').
    """
    offenders: list[str] = []
    seen = 0
    classes: dict[str, int] = {}
    for rel, name, target, klass in _scan_delegate_tasks():
        seen += 1
        classes[klass] = classes.get(klass, 0) + 1
        if klass == "SUSPICIOUS":
            offenders.append(f"{rel} :: {name!r} -> delegate_to: {target}")

    # Non-vacuity (FR-5): the scan saw delegate_to tasks AND exercised the
    # add_host-registered branch (otherwise the classifier's hardest leg is dead).
    assert seen > 0, (
        "No delegate_to task was discovered by the role-tasks/playbook glob -- the "
        "scan is vacuous; the layout may have moved, repoint the globs."
    )
    assert classes.get("add_host-registered", 0) > 0, (
        "No delegate_to task was classified as add_host-registered -- the "
        "orchestrator-context re-registration idiom (svc07/svc06/obs clean_slate "
        "registering traefik-cross-guest-cleanup) was not seen; the classifier's "
        "add_host leg may have regressed."
    )

    assert not offenders, (
        "The following delegated task(s) have a delegate_to target that is NOT "
        "offline-provably connectable (bare IP/host var with no inline "
        "connection vars and no same-file add_host registration): "
        f"{offenders}. This is the live-caught obs_clean_slate class -- before "
        "delegating from a `hosts: localhost` orchestrator play, add_host the "
        "target with connection vars (ansible_user/ansible_connection) so the "
        "delegated command actually connects. See the runtime-wiring contract in "
        "testing-strategy.md."
    )


# --------------------------------------------------------------------------- #
# Check 2 — tf_env completeness.
# --------------------------------------------------------------------------- #
def _scan_tf_env_dicts() -> dict[str, dict]:
    """Return {dict_name: {relfile, keys}} for every top-level *_tf_env dict."""
    found: dict[str, dict] = {}
    for rel, path in _iter_defaults_files():
        try:
            defaults = _load_defaults(path)
        except yaml.YAMLError:
            continue
        for key, val in defaults.items():
            if key.endswith("_tf_env") and isinstance(val, dict):
                found[key] = {"file": rel, "keys": set(val.keys())}
    return found


def _tf_env_references_in_tasks() -> set[str]:
    """The set of `<name>_tf_env` names referenced by an `environment:` in tasks."""
    refs: set[str] = set()
    pat = re.compile(r"\{\{\s*([A-Za-z0-9_]+_tf_env)\s*\}\}")
    for _, path in _iter_role_task_files():
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            tasks = _iter_tasks(doc) if isinstance(doc, list) else _iter_tasks([doc])
            for task in tasks:
                env = task.get("environment")
                if isinstance(env, str):
                    refs.update(pat.findall(env))
    return refs


def test_tf_env_dicts_carry_full_tfvar_set():
    """Every *_tf_env dict carries the full provider TF_VAR set (FR-2/FR-3).

    Validates: FR-2. Each top-level `*_tf_env` dict in a role defaults/main.yml
    must contain TF_VAR_proxmox_endpoint, _api_token AND _insecure. A dict with
    endpoint+token but no `insecure` is the WS-C1 bug (the clean-slate/destroy env
    that omits the insecure flag the bring-up env carries -> live x509 failure).
    Also cross-checks (FR-3) that `environment:` references resolve to a real
    dict, and pins the apply<->destroy parity anchors.
    """
    dicts = _scan_tf_env_dicts()
    offenders: list[str] = []
    for name, info in sorted(dicts.items()):
        missing = _REQUIRED_TF_VARS - info["keys"]
        if missing:
            offenders.append(f"{info['file']} :: {name} missing {sorted(missing)}")

    # Non-vacuity (FR-5) + the WS-C1 apply<->destroy parity (FR-3): both anchors
    # discovered and both carry proxmox_insecure.
    for anchor in _TF_ENV_ANCHORS:
        assert anchor in dicts, (
            f"Expected the {anchor!r} dict to be discovered by the defaults glob "
            "-- not found; the role layout may have moved, repoint the guard."
        )
        assert "TF_VAR_proxmox_insecure" in dicts[anchor]["keys"], (
            f"{anchor} is missing TF_VAR_proxmox_insecure -- this is the WS-C1 "
            "apply/destroy parity regression (the destroy env dropping the "
            "insecure flag the apply env carries)."
        )

    assert not offenders, (
        "The following terraform-env dict(s) are missing a required TF_VAR_* key "
        f"(the WS-C1 class): {offenders}. Any `*_tf_env` dict consumed by a "
        "provider-authenticated terraform apply/destroy/init MUST carry "
        f"{sorted(_REQUIRED_TF_VARS)} so the bring-up and teardown credential/TLS "
        "handling cannot drift. See the runtime-wiring contract in "
        "testing-strategy.md."
    )

    # FR-3 cross-check: every environment: "{{ X_tf_env }}" names a real dict.
    dangling = sorted(_tf_env_references_in_tasks() - set(dicts))
    assert not dangling, (
        "The following `environment:` reference(s) name a *_tf_env dict that does "
        f"not exist in any role defaults/main.yml: {dangling}. A terraform task's "
        "env references a renamed/deleted dict -- define it or fix the reference."
    )


# --------------------------------------------------------------------------- #
# Check 3 — secret-path structural consistency.
# --------------------------------------------------------------------------- #
def _scan_secret_paths() -> dict[str, dict]:
    """Return {var_name: {file, value}} for every *_secret_path* scalar."""
    found: dict[str, dict] = {}
    for rel, path in _iter_defaults_files():
        try:
            defaults = _load_defaults(path)
        except yaml.YAMLError:
            continue
        for key, val in defaults.items():
            if not isinstance(val, str):
                continue
            if key.endswith(("_secret_path", "_secret_path_prefix", "_secret_path_tmpl")):
                found[key] = {"file": rel, "value": " ".join(val.split())}
    return found


def test_secret_paths_match_canonical_shape():
    """Every OpenBao secret-path template matches the canonical prefix (FR-4).

    Validates: FR-4. Each `*_secret_path`/`_prefix`/`_tmpl` scalar in a role
    defaults/main.yml must begin with the canonical OpenBao shape
    `(secret|{{ <mount> }})/(proj-|observability/)...`. A drift like
    `secret/project-...` or a mis-namespaced path FAILS. This is a
    structural-prefix consistency check -- deliberately tolerant of differing
    Jinja var names in the tail (project_slug vs postgres_project_slug), NOT a
    byte-equality / single-constant check (design.md "Known limitation").
    """
    paths = _scan_secret_paths()
    offenders: list[str] = []
    for name, info in sorted(paths.items()):
        if not _SECRET_PATH_SHAPE.match(info["value"]):
            offenders.append(f"{info['file']} :: {name} = {info['value']}")

    # Non-vacuity (FR-5).
    assert paths, (
        "No *_secret_path scalar was discovered by the defaults glob -- the scan "
        "is vacuous; the layout may have moved, repoint the guard."
    )
    assert _SECRET_PATH_ANCHOR in paths, (
        f"Expected the {_SECRET_PATH_ANCHOR!r} anchor to be discovered -- not "
        "found; the role layout may have moved, repoint the guard."
    )

    assert not offenders, (
        "The following OpenBao secret-path template(s) do NOT match the canonical "
        f"`(secret|{{{{mount}}}})/(proj-|observability/)...` prefix: {offenders}. "
        "A drifted namespace/prefix (e.g. secret/project-... or a mis-namespaced "
        "path) can silently point a producer and consumer at different KV "
        "locations. See the runtime-wiring contract in testing-strategy.md."
    )
