"""Offline guards for the zitadel-org-provision production-path insecure-TLS bugfix.

Feature: fix-zitadel-org-provision-production-path-insecure-tls.

The `zitadel-org-provision` role (SVC-06) has two helper execution paths:

* DEV-GUEST path (``when: zop_dev_guest_run | bool``): runs the helper ON the
  ZITADEL guest at ``http://<container-ip>:8080`` — PLAIN HTTP, no TLS, no
  ``--insecure``.
* PRODUCTION path (``when: not (zop_dev_guest_run | bool)``): runs the helper on
  the control node over HTTPS via Traefik at ``zop_base_url``
  (``https://auth.<domain>``). Its argv never carried ``--insecure``.

SVC-09 Traefik serves a self-signed cert by default (the permanent default-off
``traefik_tls_self_signed`` mock — ADR-0013 — SVC-08/Let's-Encrypt not deployed),
so the production-path helper's HTTPS call failed
``[SSL: CERTIFICATE_VERIFY_FAILED] … self-signed certificate`` before any
provisioning, aborting SSO onboarding (live symptom at consumer_bootstrap STEP 4).

The fix adds a gating default ``zop_base_url_tls_skip_verify: true`` and, on the
PRODUCTION path only, appends ``--insecure`` to the helper argv via a
``zop_insecure_arg`` vars entry (``['--insecure']`` iff the knob, else ``[]``),
appended after ``zop_redirect_arg``. The helper already supports ``--insecure``;
the dev-guest path, token-in-ENV + ``no_log`` posture, per-entry ``changed_when``,
loop label, and OpenBao capture are untouched.

These tests are pure-logic (PyYAML parse of the committed role files) — no infra.

* **Exploration / Fix-Checking (Property 1):** the default var exists (true) and
  the production-path argv appends the gated ``--insecure``. FAIL on unfixed.
* **Preservation (Property 2):** dev-guest path has no ``--insecure``; production
  task keeps ``no_log: true``; token via ``environment:`` not argv; per-entry
  ``changed_when``; loop label = ``item.name``. PASS on unfixed.
* **Real-cert-off (Property 3):** knob false -> argv carries no ``--insecure``
  (verification enforced). Pinned with the fix-checking set (goes green with fix).
"""

from __future__ import annotations

from pathlib import Path

import yaml

# tests/installer/test_zitadel_org_provision_production_tls.py -> repo root parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ZOP_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision" / "tasks" / "main.yml"
)
_ZOP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision" / "defaults" / "main.yml"
)

_SKIP_VAR = "zop_base_url_tls_skip_verify"
_INSECURE_ARG_VAR = "zop_insecure_arg"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path):
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _load_defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _command_argv_text(task: dict) -> str | None:
    for mod in ("ansible.builtin.command", "command"):
        spec = task.get(mod)
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return " ".join(str(a) for a in argv)
            if isinstance(argv, str):
                return argv
        elif isinstance(spec, str):
            return spec
    return None


def _when_text(task: dict) -> str:
    return str(task.get("when", ""))


def _vars_text(task: dict) -> str:
    return str(task.get("vars", ""))


def _find_helper_tasks(tasks):
    """All command tasks invoking the provisioning helper (dev + production).

    The PRODUCTION task invokes the control-node helper via ``zop_provision_helper``;
    the DEV-GUEST task invokes the staged-on-guest copy via ``zop_guest_helper_path``.
    Match either marker so both paths are discovered.
    """
    out = []
    for t in tasks:
        argv = _command_argv_text(t)
        if argv and ("zop_provision_helper" in argv or "zop_guest_helper_path" in argv):
            out.append(t)
    return out


def _find_production_helper_task(tasks) -> dict | None:
    """The production-path helper task: invokes the helper, gated
    ``when: not (zop_dev_guest_run | bool)`` (control node -> Traefik HTTPS)."""
    for t in _find_helper_tasks(tasks):
        w = _when_text(t)
        if "not" in w and "zop_dev_guest_run" in w:
            return t
    return None


def _find_dev_guest_helper_task(tasks) -> dict | None:
    """The dev-guest helper task: invokes the helper on the guest over HTTP,
    gated ``when: zop_dev_guest_run | bool`` (no ``not``)."""
    for t in _find_helper_tasks(tasks):
        w = _when_text(t)
        if "zop_dev_guest_run" in w and "not" not in w:
            return t
    return None


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 2.1, 2.3, 2.5
# --------------------------------------------------------------------------- #
def test_defaults_define_tls_skip_verify_true():
    """Property 1 (Validates: Req 2.4).

    defaults/main.yml defines zop_base_url_tls_skip_verify: true (the gating knob,
    default-true for the self-signed-mock reality). FAILS on unfixed (no key).
    """
    data = _load_defaults(_ZOP_DEFAULTS)
    assert _SKIP_VAR in data, (
        f"zitadel-org-provision defaults must define {_SKIP_VAR} (production-path "
        "TLS-skip knob, mirroring zop_openbao_tls_skip_verify)."
    )
    assert bool(data[_SKIP_VAR]) is True, (
        f"{_SKIP_VAR} must default to true (tolerate Traefik's self-signed mock cert "
        "today; flip to false once real certs are deployed)."
    )


def test_production_path_argv_appends_gated_insecure():
    """Property 1 (Validates: Req 2.1, 2.3, 2.5).

    The production-path helper task appends the gated --insecure: a zop_insecure_arg
    vars entry gated on zop_base_url_tls_skip_verify, appended to argv after
    zop_redirect_arg. FAILS on unfixed (argv ends at + zop_redirect_arg; no var).
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    prod = _find_production_helper_task(tasks)
    assert prod is not None, "production-path helper task (when: not zop_dev_guest_run) not found."
    argv = _command_argv_text(prod) or ""
    vars_text = _vars_text(prod)
    assert _INSECURE_ARG_VAR in argv, (
        "production-path argv must append the gated insecure arg "
        f"(+ {_INSECURE_ARG_VAR}) after zop_redirect_arg."
    )
    assert _INSECURE_ARG_VAR in vars_text, (
        f"production-path task vars: must define {_INSECURE_ARG_VAR}."
    )
    assert "--insecure" in vars_text, (
        f"{_INSECURE_ARG_VAR} must yield ['--insecure'] (the helper flag)."
    )
    assert _SKIP_VAR in vars_text, (
        f"{_INSECURE_ARG_VAR} must be gated on {_SKIP_VAR}."
    )


def test_production_path_insecure_appended_after_redirect_arg():
    """Property 1 (Validates: Req 2.1).

    The insecure arg is appended AFTER zop_redirect_arg (argv tail
    '+ zop_redirect_arg + zop_insecure_arg'), so the existing redirect handling is
    untouched. FAILS on unfixed.
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    prod = _find_production_helper_task(tasks)
    assert prod is not None, "production-path helper task not found."
    argv = _command_argv_text(prod) or ""
    assert "zop_redirect_arg" in argv, "production argv must keep zop_redirect_arg."
    assert argv.index("zop_redirect_arg") < argv.index(_INSECURE_ARG_VAR), (
        "zop_insecure_arg must be appended AFTER zop_redirect_arg in the argv list."
    )


# --------------------------------------------------------------------------- #
# REAL-CERT-OFF ENFORCEMENT (Property 3) — goes green with the fix.
# Validates: Requirements 2.2, 2.4
# --------------------------------------------------------------------------- #
def test_insecure_arg_gating_both_directions():
    """Property 3 (Validates: Req 2.2, 2.4).

    The zop_insecure_arg Jinja gates ONLY on the knob: yields ['--insecure'] when
    truthy, [] when falsy. We assert the gating expression references the knob and
    the --insecure literal, and uses a conditional (if/else) form — so knob false
    => no --insecure (verification enforced). FAILS on unfixed (var absent).
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    prod = _find_production_helper_task(tasks)
    assert prod is not None, "production-path helper task not found."
    vars_block = prod.get("vars") or {}
    expr = str(vars_block.get(_INSECURE_ARG_VAR, ""))
    assert expr, f"{_INSECURE_ARG_VAR} not defined in the production task vars block."
    assert "--insecure" in expr, "gating expr must produce the --insecure flag."
    assert _SKIP_VAR in expr, "gating expr must reference the knob."
    assert ("if" in expr and "else" in expr), (
        "gating expr must be a conditional (['--insecure'] if knob else []) so "
        "knob false yields no --insecure (verification enforced)."
    )
    # The gating must NOT depend on any per-entry App_Registration field.
    for field in ("item.app_type", "item.auth_method", "item.redirect_uris", "item.name"):
        assert field not in expr, (
            f"the --insecure gating must depend ONLY on the knob, not on {field}."
        )


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.6, 3.7
# --------------------------------------------------------------------------- #
def test_dev_guest_path_has_no_insecure():
    """Preservation (Validates: Req 3.1).

    The dev-guest helper task (HTTP) carries NO --insecure and is unchanged.
    PASSES on unfixed and after the fix.
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    dev = _find_dev_guest_helper_task(tasks)
    assert dev is not None, "dev-guest helper task (when: zop_dev_guest_run) not found."
    argv = _command_argv_text(dev) or ""
    assert "--insecure" not in argv, (
        "dev-guest path reaches ZITADEL over plain HTTP — it must NOT add --insecure (Req 3.1)."
    )
    assert _INSECURE_ARG_VAR not in argv, (
        "dev-guest path must not acquire the production-path zop_insecure_arg (Req 3.1)."
    )


def test_production_task_keeps_no_log_and_token_in_env():
    """Preservation (Validates: Req 3.2).

    The production-path task keeps no_log: true and carries the token via
    environment: ZITADEL_PROVISION_TOKEN — never on the argv.
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    prod = _find_production_helper_task(tasks)
    assert prod is not None, "production-path helper task not found."
    assert prod.get("no_log") is True, "production-path task must keep no_log: true (Req 3.2)."
    env = prod.get("environment") or {}
    assert "ZITADEL_PROVISION_TOKEN" in env, (
        "production-path task must carry the token via environment: (Req 3.2)."
    )
    argv = _command_argv_text(prod) or ""
    assert "ZITADEL_PROVISION_TOKEN" not in argv, (
        "the provisioning token must NEVER appear on the argv (Req 3.2)."
    )


def test_production_task_keeps_changed_when_and_loop_label():
    """Preservation (Validates: Req 3.3, 3.6).

    The production-path task retains its per-entry changed_when (already_existed
    semantics) and loop label = item.name.
    """
    tasks = _load_role_tasks(_ZOP_TASKS)
    prod = _find_production_helper_task(tasks)
    assert prod is not None, "production-path helper task not found."
    assert "already_existed" in str(prod.get("changed_when", "")), (
        "production-path task must keep its per-entry changed_when (Req 3.3)."
    )
    lc = prod.get("loop_control") or {}
    assert "item.name" in str(lc.get("label", "")), (
        "production-path task must keep loop_control.label = item.name (Req 3.6)."
    )


def test_helper_still_supports_insecure_flag():
    """Preservation (Validates: Req 3.7).

    The helper scripts/installer/zitadel_provision.py still exposes --insecure and
    its unverified-SSL context (no Python behavior change by this fix).
    """
    helper = (_REPO_ROOT / "scripts" / "installer" / "zitadel_provision.py").read_text(
        encoding="utf-8"
    )
    assert '"--insecure"' in helper or "'--insecure'" in helper, (
        "the helper must keep its --insecure argparse flag (Req 3.7)."
    )
    assert "CERT_NONE" in helper or "check_hostname" in helper, (
        "the helper must keep its unverified-SSL-context logic (Req 3.7)."
    )
