"""Offline role-structure tests for zitadel-org-provision two-client onboarding.

Spec: zitadel-spa-pkce-onboarding, Task 6.7 (R4.3, R4.5, R5.5, R5.7). These
tests are STATIC: they parse the role's defaults/main.yml and tasks/main.yml
with PyYAML and assert structural invariants of the two-client onboarding
extension. They do NOT run ansible-playbook and need no live Proxmox / ZITADEL /
OpenBao — they are pure offline guards runnable in the standard offline gate.

What they pin:
  * The no-list default synthesizes exactly the single `<slug>-webapp` web entry
    (R4.3 / R6.1) — via the default vars + the synthesis task's else-branch.
  * An unknown app_type entry fails naming the entry (R4.5) — the validation
    assert loops the registrations and checks membership in zop_valid_app_types.
  * Neither token source yields a token -> fail naming BOTH sources (R5.7) — the
    token assert's fail_msg names the env var AND the OpenBao field/path.
  * no_log: true is present on every secret-handling task (R5.5).

Run:  ~/venv/devinfra/bin/pytest tests/installer/test_zitadel_org_provision_role.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "zitadel-org-provision"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_TASKS = _ROLE_DIR / "tasks" / "main.yml"

_NON_MODULE_KEYS = {
    "name", "when", "register", "no_log", "changed_when", "failed_when", "loop",
    "loop_control", "delegate_to", "become", "vars", "tags", "args", "block",
    "rescue", "always", "until", "retries", "delay", "environment", "check_mode",
    "notify", "listen", "ignore_errors",
}


def _load_defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _load_tasks() -> list[dict]:
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(_TASKS.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _module_names(t: dict) -> set[str]:
    return {k for k in t if k not in _NON_MODULE_KEYS}


def _body_text(t: dict) -> str:
    parts = [str(t.get("name", "")), str(t.get("environment", ""))]
    for m in _module_names(t):
        parts.append(str(m))
        parts.append(str(t.get(m)))
    return " ".join(parts)


def _no_log_true(t: dict) -> bool:
    v = t.get("no_log")
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "yes", "1"))


# --------------------------------------------------------------------------- #
# Both files parse (offline sanity).
# --------------------------------------------------------------------------- #
def test_role_files_parse():
    assert isinstance(_load_defaults(), dict)
    assert len(_load_tasks()) > 0


# --------------------------------------------------------------------------- #
# R4.1 — the new defaults from the design Variables table are present.
# --------------------------------------------------------------------------- #
def test_new_defaults_present_and_typed():
    d = _load_defaults()
    # The list default is empty (synthesize legacy default downstream).
    assert d.get("zop_app_registrations") == []
    assert d.get("zop_default_app_type") == "web"
    assert d.get("zop_default_auth_method") == "basic"
    assert d.get("zop_valid_app_types") == ["spa", "web", "api"]
    assert d.get("zop_app_type_default_auth_method") == {
        "spa": "none", "web": "basic", "api": "jwt",
    }
    # Per-app unique path template carries the {app_name} format token.
    assert "{app_name}" in d.get("zop_openbao_app_secret_path_tmpl", "")
    # Pre-existing vars retained unchanged.
    assert d.get("zop_app_name") == "{{ zop_project_slug }}-webapp"
    assert "zop_roles" in d and "zop_redirect_uris" in d


# --------------------------------------------------------------------------- #
# R4.3 — no-list default synthesizes the single <slug>-webapp web entry.
# --------------------------------------------------------------------------- #
def test_no_list_default_synthesizes_single_webapp():
    d = _load_defaults()
    # The synthesized default's fields come from these vars.
    assert d["zop_default_app_name"] == "{{ zop_project_slug }}-webapp"
    assert d["zop_default_app_type"] == "web"
    assert d["zop_default_auth_method"] == "basic"

    # The synthesis task builds the single-entry else-branch from those vars.
    synth = [t for t in _load_tasks()
             if "Synthesize the legacy single-webapp default" in str(t.get("name", ""))]
    assert len(synth) == 1, "exactly one synthesis task expected"
    body = _body_text(synth[0])
    # else-branch references the default name/type/method vars.
    assert "zop_default_app_name" in body
    assert "zop_default_app_type" in body
    assert "zop_default_auth_method" in body
    # Guarded on the list being empty.
    assert "zop_app_registrations | length > 0" in body


# --------------------------------------------------------------------------- #
# R4.5 — unknown app_type entry fails naming the entry.
# --------------------------------------------------------------------------- #
def test_unknown_app_type_fails_naming_entry():
    validate = [t for t in _load_tasks()
                if "app_type is recognized" in str(t.get("name", ""))]
    assert len(validate) == 1, "exactly one app_type validation task expected"
    t = validate[0]
    # It is an assert that loops the effective registrations.
    assert ("ansible.builtin.assert" in t) or ("assert" in t)
    assert t.get("loop") == "{{ zop_effective_registrations }}"
    that = (t.get("ansible.builtin.assert") or t.get("assert")).get("that")
    joined = " ".join(str(x) for x in that)
    assert "zop_valid_app_types" in joined
    # The fail_msg names the offending entry (item.name) and its app_type.
    fail_msg = str((t.get("ansible.builtin.assert") or t.get("assert")).get("fail_msg"))
    assert "item.name" in fail_msg
    assert "item.app_type" in fail_msg


# --------------------------------------------------------------------------- #
# R5.7 — neither token source -> fail naming BOTH sources.
# --------------------------------------------------------------------------- #
def test_token_assert_names_both_sources():
    tok = [t for t in _load_tasks()
           if "Assert a provisioning token resolved" in str(t.get("name", ""))]
    assert len(tok) == 1, "exactly one token assert expected"
    spec = tok[0].get("ansible.builtin.assert") or tok[0].get("assert")
    fail_msg = str(spec.get("fail_msg"))
    # Names BOTH the env var and the OpenBao field/path.
    assert "ZITADEL_PROVISION_TOKEN" in fail_msg
    assert "zop_openbao_provision_token_field" in fail_msg
    assert "zop_openbao_provision_secret_path" in fail_msg
    assert _no_log_true(tok[0])


# --------------------------------------------------------------------------- #
# R5.5 — no_log: true on every secret-handling task.
# --------------------------------------------------------------------------- #
# A secret-handling task is one whose body carries the provisioning token, the
# OpenBao admin token, a client_secret, or an environment bridging either token.
_SECRET_MARKERS = (
    "zop_provision_token",
    "ZITADEL_PROVISION_TOKEN",
    "zop_bao_token",
    "OPENBAO_ADMIN_TOKEN",
    "client_secret",
    "provision_token",
)
# Message-only modules reference a var NAME, not its value — they need not be
# no_log (the token assert IS no_log and is checked above explicitly).
_MESSAGE_ONLY = {"ansible.builtin.assert", "assert", "ansible.builtin.debug", "debug",
                 "ansible.builtin.fail", "fail"}


def _handles_secret(t: dict) -> bool:
    mods = _module_names(t)
    if mods and mods <= _MESSAGE_ONLY:
        return False
    body = _body_text(t)
    return any(m in body for m in _SECRET_MARKERS)


def test_every_secret_handling_task_is_no_log_true():
    offenders = []
    for t in _load_tasks():
        if _handles_secret(t) and not _no_log_true(t):
            offenders.append(repr(t.get("name", "<unnamed>")))
    assert not offenders, (
        "R5.5: these secret-handling tasks are missing no_log: true:\n  "
        + "\n  ".join(offenders)
    )


def test_no_no_log_false_on_secret_task():
    offenders = []
    for t in _load_tasks():
        if t.get("no_log") is False and _handles_secret(t):
            offenders.append(repr(t.get("name", "<unnamed>")))
    assert not offenders, "R5.5: no_log: false on a secret-handling task:\n  " + "\n  ".join(offenders)


# --------------------------------------------------------------------------- #
# R6.4 / Component 5 — both run paths loop the registrations; label is the name.
# --------------------------------------------------------------------------- #
def test_both_run_paths_loop_registrations_with_name_label():
    tasks = _load_tasks()
    dev = [t for t in tasks if "DEV: provision each app" in str(t.get("name", ""))]
    prod = [t for t in tasks if str(t.get("name", "")).startswith("ZITADEL-org | Provision each app")]
    assert len(dev) == 1 and len(prod) == 1
    for t in (dev[0], prod[0]):
        assert t.get("loop") == "{{ zop_normalized_registrations }}"
        # loop label is the entry NAME only (never a secret).
        assert t.get("loop_control", {}).get("label") == "{{ item.name }}"
        assert _no_log_true(t)
        body = _body_text(t)
        assert "--app-type" in body and "--auth-method" in body
