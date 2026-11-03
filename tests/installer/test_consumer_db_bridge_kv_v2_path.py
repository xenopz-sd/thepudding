"""Offline guards for the consumer DB-bridge KV-v2 doubled-``data/`` path bugfix.

Feature: fix-consumer-db-bridge-kv-v2-path.

The ``secret/`` mount is **KV v2**. The ``bao kv`` CLI (``get``/``put``/``list``/
``metadata``) is v2-aware: given a *bare logical* path it injects the ``data/`` (or
``metadata/``) infix ITSELF to form the HTTP API path. The DB-credential bridge in
``consumer_bootstrap`` (STEP 2a) instead passed a path that ALREADY contained
``data/`` (``'secret/data/' ~ project_slug ~ '/' ~ consumer_db_bridge_subpath``), so
the CLI doubled it and the credential landed at logical ``secret/data/<slug>/db``
(API ``secret/data/data/<slug>/db``) instead of the intended logical
``secret/<slug>/db``. The workload AppRole policy grants — and the Agent
consul-template reads — the intended path, so the Agent found "no secret exists".

The raw ``secret/data/<slug>/...`` form is CORRECT for consul-template / ``bao
read`` / the HTTP API (they take the raw API path) — which is why ``agent.hcl.j2``
and the policy legitimately contain it. It is WRONG only as an argument to
``bao kv``. These tests therefore target ``bao kv`` invocation paths specifically.

Two layers (design.md Correctness Properties):

* **Exploration / Fix-Checking (Property 1):** the STEP 2a.3 read-or-reuse probe
  and the STEP 2a.6 put each pass a BARE logical path ``secret/<slug>/<subpath>``
  (no ``data/`` after ``secret/``); and NO ``bao kv`` call in any role tasks file
  passes a ``secret/data/...`` argument. On UNFIXED code these FAIL (the two bridge
  paths carry ``secret/data/``) — the structural counterexample.

* **Preservation (Property 2):** the STEP 2a.1 source read still uses
  ``consumer_openbao_db_secret_path`` (``secret/proj-<slug>/postgres``, no ``data/``);
  the bridge subpath/field defaults are unchanged; the Agent template keeps the
  raw-API ``secret/data/<slug>/...`` consul-template form. These PASS on UNFIXED
  code and MUST keep passing.

Pure-logic (PyYAML parse + source-string checks of committed files) — no infra.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# tests/installer/test_*.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONSUMER_BOOTSTRAP_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
)
_CONSUMER_BOOTSTRAP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "defaults" / "main.yml"
)
_ROLES_DIR = _REPO_ROOT / "ansible" / "roles"
_AGENT_HCL_TEMPLATE = (
    _REPO_ROOT / "ansible" / "roles" / "openbao-agent" / "templates" / "agent.hcl.j2"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path):
    """Return the flat ordered task list of a role ``tasks/main.yml``."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _command_argv_text(task: dict) -> str | None:
    """Return the flat argv/command string of a command/shell task, else None."""
    for mod in (
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "command",
        "shell",
    ):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return " ".join(str(a) for a in argv)
            if isinstance(argv, str):
                return argv
            cmd = spec.get("cmd")
            if isinstance(cmd, str):
                return cmd
    return None


# A ``bao kv <sub>`` subcommand token sequence (tolerant of the quoting/commas a
# Jinja list argv collapses to). Covers get/put/list/metadata.
_BAO_KV_RE = re.compile(
    r"\bbao\b[\s'\",]+kv\b[\s'\",]+(get|put|list|metadata)\b",
    re.IGNORECASE,
)
# A literal ``secret/data/`` path ARGUMENT. We look for the string ``secret/data/``
# appearing as a path token (the defect: a doubled-data/ argument to bao kv).
_SECRET_DATA_ARG_RE = re.compile(r"secret/data/")


def _is_bao_kv_invocation(task: dict) -> str | None:
    """Return the argv text if this task is a ``bao kv ...`` command, else None."""
    argv = _command_argv_text(task)
    if argv is None:
        return None
    return argv if _BAO_KV_RE.search(argv) else None


def _targets_bridged_record(argv: str) -> bool:
    """True iff a bao-kv argv targets the bridged record (bare <slug>/<subpath> db).

    Matches either the var form (``project_slug`` + ``consumer_db_bridge_subpath``)
    or a literal ``<slug>/db`` form. Excludes the source read (``proj-<slug>/postgres``
    via ``consumer_openbao_db_secret_path``) and the metadata-path onboarding call
    (which uses ``openbao_kv_mount ~ '/' ~ project_slug``).
    """
    references_slug = "project_slug" in argv
    references_db_subpath = (
        "consumer_db_bridge_subpath" in argv or "/db" in argv
    )
    is_source = "consumer_openbao_db_secret_path" in argv or "postgres" in argv
    return references_slug and references_db_subpath and not is_source


def _bridge_bao_kv_tasks(tasks):
    """Return the bridge ``bao kv get``/``put`` tasks that target the bridged record."""
    out = []
    for t in tasks:
        argv = _is_bao_kv_invocation(t)
        if argv is not None and _targets_bridged_record(argv):
            out.append((t, argv))
    return out


def _all_role_bao_kv_invocations():
    """Yield (path, argv) for every ``bao kv ...`` command across role tasks files."""
    for tasks_file in sorted(_ROLES_DIR.glob("*/tasks/*.yml")):
        try:
            tasks = _load_role_tasks(tasks_file)
        except yaml.YAMLError:
            continue
        for t in tasks:
            argv = _is_bao_kv_invocation(t)
            if argv is not None:
                yield tasks_file, argv


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 1.1, 1.2, 1.3 (bug) -> 2.1, 2.2, 2.3 (fix)
# --------------------------------------------------------------------------- #
def test_bridge_bao_kv_paths_are_bare_logical_no_double_data():
    """Property 1 (Validates: Req 2.1, 2.2).

    The STEP 2a.3 read-or-reuse probe (``bao kv get``) and the STEP 2a.6 bridge
    (``bao kv put``) that target the bridged record MUST pass a BARE logical path
    ``secret/<slug>/<subpath>`` — NOT ``secret/data/<slug>/...``. The KV-v2 CLI
    injects ``data/`` itself; a ``secret/data/`` argument is doubled.

    FAILS on unfixed code: both paths are
    ``'secret/data/' ~ project_slug ~ '/' ~ consumer_db_bridge_subpath``.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    bridge = _bridge_bao_kv_tasks(tasks)
    # There must be at least the probe (get) and the put targeting the bridged record.
    assert len(bridge) >= 2, (
        "Expected at least two bridge `bao kv` calls targeting secret/<slug>/<subpath> "
        f"(the 2a.3 probe + the 2a.6 put); found {len(bridge)}. "
        "The bridge shape changed unexpectedly."
    )
    for task, argv in bridge:
        name = task.get("name", "<unnamed>")
        assert _SECRET_DATA_ARG_RE.search(argv) is None, (
            f"Bridge `bao kv` call {name!r} passes a 'secret/data/...' path argument "
            "to a KV-v2-aware CLI, which doubles the infix -> the credential lands at "
            "secret/data/data/<slug>/<subpath> instead of the intended logical path. "
            "Use the BARE logical path 'secret/' ~ project_slug ~ '/' ~ "
            "consumer_db_bridge_subpath (the CLI injects data/ once)."
        )
        # Positive: the path is composed from the bare 'secret/' prefix + slug + subpath.
        assert "secret/" in argv and "project_slug" in argv, (
            f"Bridge `bao kv` call {name!r} must compose a secret/<slug>/<subpath> path."
        )


def test_no_bao_kv_call_in_any_role_uses_secret_data_argument():
    """Property 1 (Validates: Req 1.1-1.3, 2.1, 2.2, 3.5) — repo-wide guard.

    NO ``bao kv`` (get/put/list/metadata) invocation in ANY
    ``ansible/roles/**/tasks/*.yml`` may pass a ``secret/data/...`` path argument,
    because ``bao kv`` injects ``data/`` itself (the raw ``data/`` API path is only
    for ``bao read``/``bao write`` / the HTTP API / consul-template, never ``bao
    kv``). Guards the sibling roles against the same mistake.

    FAILS on unfixed code: the two consumer_bootstrap bridge calls trip it. PASSES
    after the fix (the source read uses ``consumer_openbao_db_secret_path`` ->
    ``secret/proj-<slug>/postgres``, no ``data/``; siblings use bare logical paths;
    ``openbao_project_onboard`` uses ``openbao_kv_mount ~ '/' ~ project_slug``).
    """
    offenders: list[str] = []
    for tasks_file, argv in _all_role_bao_kv_invocations():
        if _SECRET_DATA_ARG_RE.search(argv):
            rel = tasks_file.relative_to(_REPO_ROOT)
            offenders.append(f"{rel}: {argv.strip()[:160]}")
    assert not offenders, (
        "Found `bao kv` invocation(s) passing a 'secret/data/...' path argument. "
        "The KV-v2 CLI injects 'data/' itself, so this doubles the infix. Use the "
        "bare logical path (e.g. 'secret/<slug>/<subpath>') for `bao kv`; the raw "
        "'secret/data/...' form is only for `bao read`/`bao write`/consul-template. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.3, 3.4
# --------------------------------------------------------------------------- #
def test_preservation_source_read_uses_bare_proj_postgres_path():
    """Preservation (Validates: Req 3.1).

    The STEP 2a.1 source read keeps using ``consumer_openbao_db_secret_path``, which
    resolves to ``secret/proj-<slug>/postgres`` (a BARE logical path, no ``data/``).
    Holds on unfixed and fixed code.
    """
    tasks_text = _CONSUMER_BOOTSTRAP_TASKS.read_text(encoding="utf-8")
    assert "consumer_openbao_db_secret_path" in tasks_text, (
        "STEP 2a.1 must keep reading the source via consumer_openbao_db_secret_path."
    )
    defaults = yaml.safe_load(_CONSUMER_BOOTSTRAP_DEFAULTS.read_text(encoding="utf-8"))
    src = str(defaults.get("consumer_openbao_db_secret_path", ""))
    assert "secret/proj-" in src and "data/" not in src, (
        "consumer_openbao_db_secret_path must stay secret/proj-<slug>/<engine> with "
        f"no data/ infix (got {src!r})."
    )


def test_preservation_bridge_subpath_and_field_defaults_unchanged():
    """Preservation (Validates: Req 3.4).

    The bridge subpath (``db``) and field (``database_url``) defaults are unchanged —
    only the ``bao kv`` invocation path loses the ``data/`` infix.
    """
    defaults = yaml.safe_load(_CONSUMER_BOOTSTRAP_DEFAULTS.read_text(encoding="utf-8"))
    assert defaults.get("consumer_db_bridge_subpath") == "db"
    assert defaults.get("consumer_db_bridge_field") == "database_url"


def test_preservation_agent_template_keeps_raw_api_data_path_form():
    """Preservation (Validates: Req 3.3).

    ``agent.hcl.j2`` keeps the raw-API ``secret/data/<slug>/<subpath>`` consul-template
    form — correct for consul-template (which uses the raw KV-v2 API path), and the
    reason the raw form legitimately survives there while ``bao kv`` must not use it.
    """
    text = _AGENT_HCL_TEMPLATE.read_text(encoding="utf-8")
    assert "secret/data/{{ project_slug }}/" in text, (
        "agent.hcl.j2 must keep rendering the raw-API secret/data/<slug>/<subpath> "
        "consul-template stanza (unchanged by this fix)."
    )
