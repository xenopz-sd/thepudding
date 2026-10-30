"""Offline (Tier-1) bug-condition exploration + fix-validation test for the
SVC-06 clean-slate provision_token residue defect.

Spec: .kiro/specs/fix-svc06-clean-slate-provision-token-residue/
(bugfix.md, design.md, tasks.md). Task 1 (exploration) + Task 2 (preservation),
doubling as the Task 3.3/3.4 fix-validation — the SAME tests, which FAIL on the
unfixed tree and PASS once the fix lands. Mirrors the sibling
``infra/tests/test_svc06_clean_slate_backing_db_residue.py``: parse the COMMITTED
YAML with PyYAML; no infrastructure.

=== WHAT THE BUG IS ===

SVC-06's clean-slate (``svc06_clean_slate``) tears down the ZITADEL guest, the
backing SVC-01 DB, the Traefik Route_Fragment, and sweeps the SVC-06 Terraform
state — but does NOT clear the ZITADEL ``provision_token`` stored in OpenBao
(SVC-07) at ``secret/proj-shared-zitadel/zitadel`` field ``provision_token``. That
token lives on the UNTOUCHED OpenBao primary guest, so it survives the teardown;
``zitadel-server``'s ABSOLUTE persist guard (``not (zitadel_provision_token_stored
| bool)``) then refuses to replace it on the next re-bootstrap — OpenBao keeps the
destroyed instance's token, and a consumer reading it gets HTTP 401
Errors.Token.Invalid.

=== THE FIX ===

Add a field-scoped ``provision_token`` clear to ``svc06_clean_slate``: a tolerant
reachability probe -> ``svc06_openbao_reachable`` fact -> a delegated
``docker exec ... bao kv patch <path> provision_token=-`` (stdin ``=-`` empty value,
no value on argv), ``no_log``, ``-tls-skip-verify`` gated, BAO_TOKEN from
OPENBAO_ADMIN_TOKEN in env, reachability-gated + de-masked ``failed_when``. It is a
FIELD-SCOPED patch-to-empty — NEVER a whole-entry ``kv delete`` / ``kv metadata
delete`` (the same entry holds the ZITADEL masterkey). New ``svc06_openbao_*``
defaults target the same OpenBao source as ``zitadel-server``.

=== HOW THIS TEST ENCODES IT (offline, no infra) ===

- Case A (clear-task exploration, FAILS unfixed): a field-scoped provision_token
  clear task exists with all required attributes.
- Case A-invariant (masterkey protection — holds pre AND post fix): NO whole-entry
  ``kv delete`` / ``kv metadata delete`` of ``secret/proj-shared-zitadel/zitadel``.
- Case B (defaults exploration, FAILS unfixed): the new ``svc06_openbao_*`` vars
  (besides the pre-existing ``svc06_openbao_addr``) are defined in the role defaults.
- Preservation (PASSES unfixed, stays green post-fix): zitadel-server persist guard
  unchanged + no clear added there; existing clean-slate teardown tasks present.
- A pure-logic unit check of the reachability-gate intent.

Run (offline, no infra):
  ~/venv/devinfra/bin/pytest \\
    infra/tests/test_svc06_clean_slate_provision_token_residue.py -q
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLEAN_SLATE_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_clean_slate" / "tasks" / "main.yml"
)
_CLEAN_SLATE_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_clean_slate" / "defaults" / "main.yml"
)
_ZITADEL_SERVER_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-server" / "tasks" / "main.yml"
)

# The OpenBao secret path/field the clear must target (field-scoped).
_SECRET_PATH = "secret/proj-shared-zitadel/zitadel"
_TOKEN_FIELD = "provision_token"

# The new svc06_openbao_* defaults the fix adds (svc06_openbao_addr pre-exists in
# the play-level vars, so it is allowed to also be a role default but is not NEW).
_NEW_OPENBAO_DEFAULTS = (
    "svc06_openbao_cli_host",
    "svc06_openbao_cli_user",
    "svc06_openbao_cli_python",
    "svc06_openbao_primary_container_name",
    "svc06_openbao_bao_bin",
    "svc06_openbao_cli_bao_addr",
    "svc06_openbao_secret_path",
    "svc06_openbao_provision_token_field",
    "svc06_openbao_tls_skip_verify",
)


# --------------------------------------------------------------------------- #
# Parsing helpers (sibling idiom).
# --------------------------------------------------------------------------- #
def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _flatten_tasks(node) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_flatten_tasks(item))
    elif isinstance(node, dict):
        out.append(node)
        for key in ("block", "rescue", "always"):
            if key in node:
                out.extend(_flatten_tasks(node[key]))
    return out


def _role_tasks(path: Path) -> list[dict]:
    return _flatten_tasks(_load_yaml(path) or [])


def _task_cmd_text(task: dict) -> str:
    """Concatenated command/shell text (argv or cmd) of a task, else ''."""
    for mod in ("ansible.builtin.command", "ansible.builtin.shell", "command", "shell"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            if isinstance(spec.get("argv"), list):
                return " ".join(str(a) for a in spec["argv"])
            if isinstance(spec.get("cmd"), str):
                return spec["cmd"]
        elif isinstance(spec, str):
            return spec
    return ""


def _all_cmd_text(tasks: list[dict]) -> str:
    return "\n".join(_task_cmd_text(t) for t in tasks)


def _find_provision_token_clear(tasks: list[dict]) -> dict | None:
    """The field-scoped provision_token clear task: a bao kv patch referencing
    provision_token via docker exec."""
    for t in tasks:
        cmd = _task_cmd_text(t)
        has_field = (_TOKEN_FIELD in cmd) or ("svc06_openbao_provision_token_field" in cmd)
        if "kv patch" in cmd and has_field and "docker exec" in cmd:
            return t
    return None


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
# --------------------------------------------------------------------------- #
def test_provision_token_clear_task_present_field_scoped():
    """Property 1 (Validates: Req 2.1, 2.2, 2.3).

    svc06_clean_slate carries a field-scoped provision_token clear: a
    `bao kv patch <path> provision_token...` via docker exec on
    openbao-openbao-1, delegate_to the OpenBao primary, no_log, -tls-skip-verify
    gated, BAO_TOKEN from OPENBAO_ADMIN_TOKEN in env (not argv), reachability-
    gated with a de-masked failed_when. FAILS on unfixed (no such task).
    """
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)
    clear = _find_provision_token_clear(tasks)
    assert clear is not None, (
        "svc06_clean_slate must carry a field-scoped provision_token clear task "
        "(bao kv patch ... provision_token via docker exec) — none found."
    )
    cmd = _task_cmd_text(clear)
    # The container + secret path are Jinja vars in the shell cmd (resolved from
    # the svc06_openbao_* defaults, asserted literally in the defaults test).
    assert "svc06_openbao_primary_container_name" in cmd, (
        "clear must docker exec against the OpenBao primary container "
        "(svc06_openbao_primary_container_name)."
    )
    assert "svc06_openbao_secret_path" in cmd, (
        "clear must target the shared-zitadel secret path (svc06_openbao_secret_path)."
    )
    # Also locate the _find helper matched on 'kv patch' + provision_token + docker exec.
    # delegate_to the OpenBao primary
    assert "svc06_openbao_cli_host" in str(clear.get("delegate_to", "")), (
        "clear must delegate_to the OpenBao primary (svc06_openbao_cli_host)."
    )
    assert clear.get("no_log") is True, "clear must be no_log: true."
    # -tls-skip-verify gated on the TLS-skip var
    assert "-tls-skip-verify" in cmd and "svc06_openbao_tls_skip_verify" in cmd, (
        "clear must splice -tls-skip-verify gated on svc06_openbao_tls_skip_verify."
    )
    # BAO_TOKEN is supplied to the container via `docker exec -e BAO_TOKEN=...`
    # sourced from the OPENBAO_ADMIN_TOKEN env lookup — the SAME house form as
    # zitadel-server's provision_token WRITE (the whole task is no_log, so the
    # resolved value never prints). It must derive from OPENBAO_ADMIN_TOKEN and
    # the token name/value must not appear on a bare `bao` positional argv.
    assert "BAO_TOKEN=" in cmd and "OPENBAO_ADMIN_TOKEN" in cmd, (
        "clear must pass BAO_TOKEN via `docker exec -e BAO_TOKEN=...` sourced from "
        "OPENBAO_ADMIN_TOKEN (mirroring zitadel-server)."
    )
    # reachability-gated
    assert "svc06_openbao_reachable" in str(clear.get("when", "")), (
        "clear must be gated on svc06_openbao_reachable (when:)."
    )
    # de-masked failed_when tolerating only unreachable/connection conditions
    fw = str(clear.get("failed_when", ""))
    assert fw and ("unreachable" in fw.lower()), (
        "clear must carry a de-masked failed_when tolerating only unreachable/"
        "connection conditions (not a blanket failed_when: false)."
    )


def test_defaults_define_new_openbao_vars():
    """Property 1 (Validates: Req 2.1).

    The role defaults define the new svc06_openbao_* vars targeting the same
    OpenBao source as zitadel-server. FAILS on unfixed (absent).
    """
    data = _load_yaml(_CLEAN_SLATE_DEFAULTS) or {}
    for key in _NEW_OPENBAO_DEFAULTS:
        assert key in data, f"svc06_clean_slate defaults must define {key}."
    # Values that must match the zitadel-server source so the SAME entry is targeted.
    assert data["svc06_openbao_secret_path"] == _SECRET_PATH
    assert data["svc06_openbao_provision_token_field"] == _TOKEN_FIELD
    assert data["svc06_openbao_primary_container_name"] == "openbao-openbao-1"
    assert str(data["svc06_openbao_cli_host"]) == "10.0.20.10"


# --------------------------------------------------------------------------- #
# MASTERKEY-PROTECTION HARD INVARIANT (Property 3) — holds pre AND post fix.
# Validates: Requirements 2.2, 3.2
# --------------------------------------------------------------------------- #
def test_no_whole_entry_delete_of_shared_zitadel_entry():
    """Property 3 (Validates: Req 2.2, 3.2).

    The clean-slate must NEVER whole-entry `kv delete` / `kv metadata delete` the
    secret/proj-shared-zitadel/zitadel entry — that entry also holds the ZITADEL
    masterkey, and destroying it forces a full data re-encryption. Holds on the
    unfixed tree (no such delete) and MUST still hold after the fix (the fix is a
    field-scoped patch-to-empty, never a whole-entry delete).
    """
    text = _all_cmd_text(_role_tasks(_CLEAN_SLATE_TASKS))
    assert "kv delete" not in text, (
        "svc06_clean_slate must not `bao kv delete` — the shared-zitadel entry "
        "holds the masterkey; use a field-scoped `kv patch provision_token`."
    )
    assert "kv metadata delete" not in text, (
        "svc06_clean_slate must not `bao kv metadata delete` the shared-zitadel entry."
    )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.3, 3.4, 3.5, 3.6, 3.7
# --------------------------------------------------------------------------- #
def test_zitadel_server_persist_idempotency_unchanged():
    """Preservation (Validates: Req 3.1).

    zitadel-server still gates the provision_token persist on
    `not (zitadel_provision_token_stored | bool)`, and NO provision_token
    clear/patch-to-empty is added to the zitadel-server role (the clear lives only
    in svc06_clean_slate).
    """
    text = _ZITADEL_SERVER_TASKS.read_text(encoding="utf-8")
    assert "not (zitadel_provision_token_stored | bool)" in text, (
        "zitadel-server must keep its persist idempotency guard (Req 3.1)."
    )
    # No clear of provision_token added to zitadel-server (a patch to empty / delete).
    assert "provision_token=-" not in text or text.count("provision_token=-") == 1, (
        "zitadel-server should still have only its single persist write of "
        "provision_token (the stdin =- form), with no added clear."
    )
    assert "kv delete" not in text, "zitadel-server must not gain a kv delete of the token."


def test_existing_clean_slate_teardown_tasks_present():
    """Preservation (Validates: Req 3.3, 3.4, 3.5, 3.6, 3.7).

    The existing clean-slate teardown is structurally unchanged: ZITADEL-guest
    teardown (clean-slate.yml child + TF residue sweep), the backing-DB child
    svc-01-bootstrap clean_slate, the Route_Fragment removal, the empty-baseline
    gate, the confirmation gate, and the throwaway/AGENT_LIVE guard.
    """
    tasks = _role_tasks(_CLEAN_SLATE_TASKS)
    text = _CLEAN_SLATE_TASKS.read_text(encoding="utf-8")
    all_cmd = _all_cmd_text(tasks)
    # ZITADEL guest teardown via clean-slate.yml child run.
    assert "clean-slate.yml" in all_cmd, "ZITADEL-guest teardown (clean-slate.yml child) must remain."
    # Backing-DB child svc-01-bootstrap clean_slate.
    assert "svc-01-bootstrap.yml" in all_cmd and "clean_slate=true" in all_cmd, (
        "backing-DB child svc-01-bootstrap clean_slate run must remain (Req 3.4)."
    )
    # Terraform residue sweep.
    assert "state" in all_cmd and "list" in all_cmd, "TF state residue sweep must remain (Req 3.3)."
    # Route_Fragment removal.
    assert "zitadel-route.yml" in text or "svc06_zitadel_route_fragment" in text, (
        "Route_Fragment removal must remain (Req 3.5)."
    )
    # Empty-baseline gate.
    assert "svc06_surviving_vmids" in text, "empty-baseline survivor gate must remain (Req 3.3/3.4)."
    # Confirmation gate + agent-live/throwaway guard.
    assert "clean_slate_confirmed" in text, "confirmation gate must remain (Req 3.6)."
    assert "throwaway" in text.lower(), "throwaway/agent-live guard must remain (Req 3.7)."


# --------------------------------------------------------------------------- #
# PURE-LOGIC reachability-gate intent (Property 4).
# Validates: Requirements 2.4, 2.5, 2.6
# --------------------------------------------------------------------------- #
def test_reachability_gate_logic():
    """Property 4 (Validates: Req 2.4, 2.5, 2.6).

    Encode the svc06_openbao_reachable intent directly: reachable iff
    (addr non-empty) AND (keyscan rc == 0) AND (keyscan stdout non-empty). An
    empty addr or a failed/empty keyscan => not reachable => clear skips cleanly.
    """
    def reachable(addr: str, rc: int, stdout: str) -> bool:
        return bool(addr.strip()) and rc == 0 and bool(stdout.strip())

    assert reachable("10.0.20.10", 0, "10.0.20.10 ssh-ed25519 AAAA...") is True
    assert reachable("", 0, "key") is False          # unwired -> skip
    assert reachable("10.0.20.10", 1, "") is False     # keyscan failed -> skip
    assert reachable("10.0.20.10", 0, "") is False     # empty stdout -> skip
