"""Offline structural guard for the composed workload-creds-dir writable-path bugfix.

Feature: fix-composed-workload-creds-dir-writable-path.

The openbao_workload_approle STEP 8 `file`-channel handoff writes the 0600
Role_ID/Secret_ID files into openbao_workload_creds_dir (role default
/etc/openbao-workload/<slug>) owned by openbao_workload_service_user (default
root). Those tasks run on the host the role runs on (NOT delegated). On the
STANDALONE path (hosts: openbao, become: true) that is root-on-guest and works;
on the COMPOSED path (consumer_bootstrap STEP 3 on hosts: localhost, unprivileged
Developer-B user) creating /etc/openbao-workload fails `[Errno 13] Permission
denied`.

Fix (consumer_bootstrap-only, additive): STEP 3 overrides openbao_workload_creds_dir
to a $HOME-anchored path and openbao_workload_service_user to ansible_user_id
(the invoking non-root user). The role defaults (/etc + root) stay unchanged, so
the standalone path is byte-for-byte preserved.

Pure PyYAML/text parse — no infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CB_TASKS = _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
_CB_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "defaults" / "main.yml"
_WA_DEFAULTS = _REPO_ROOT / "ansible" / "roles" / "openbao_workload_approle" / "defaults" / "main.yml"


def _iter_tasks(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for k in ("block", "rescue", "always"):
            if k in t:
                yield from _iter_tasks(t[k])


def _cb_tasks():
    out = []
    for doc in yaml.safe_load_all(_CB_TASKS.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            out.extend(_iter_tasks(doc))
    return out


def _step3_vars() -> str:
    """The STEP 3 include_role (openbao_workload_approle) vars block, as text."""
    for t in _cb_tasks():
        inc = t.get("ansible.builtin.include_role") or t.get("include_role")
        if isinstance(inc, dict) and "openbao_workload_approle" in str(inc.get("name", "")):
            return str(t.get("vars", ""))
    raise AssertionError("STEP 3 include_role openbao_workload_approle not found")


# --------------------------------------------------------------------------- #
# Property 1 — Bug Condition / Expected Behavior. FAILS on unfixed code.
# --------------------------------------------------------------------------- #
def test_step3_overrides_creds_dir_to_home_and_owner_to_invoking_user():
    """STEP 3 must pass openbao_workload_creds_dir ($HOME-anchored) AND
    openbao_workload_service_user (ansible_user_id, non-root) (Req 2.1, 2.2)."""
    v = _step3_vars()
    assert "openbao_workload_creds_dir" in v, (
        "STEP 3 must override openbao_workload_creds_dir for the composed run (Req 2.1)"
    )
    assert "consumer_workload_creds_dir" in v, (
        "STEP 3 should set openbao_workload_creds_dir from consumer_workload_creds_dir"
    )
    assert "openbao_workload_service_user" in v and "ansible_user_id" in v, (
        "STEP 3 must override openbao_workload_service_user to ansible_user_id "
        "(the invoking non-root user, Req 2.2)"
    )


def test_consumer_workload_creds_dir_is_home_anchored_and_outside_repo():
    """consumer_workload_creds_dir must resolve under $HOME (absolute, outside
    the repo tree, not /etc) (Req 2.1, 2.3)."""
    data = yaml.safe_load(_CB_DEFAULTS.read_text(encoding="utf-8"))
    val = str(data.get("consumer_workload_creds_dir", ""))
    assert val, "consumer_bootstrap defaults must define consumer_workload_creds_dir (Req 2.1)"
    assert "env" in val and "HOME" in val, (
        "consumer_workload_creds_dir must be anchored at the invoking user's $HOME "
        f"(got {val!r}) — Developer-B-writable, outside the repo tree (Req 2.3)"
    )
    assert "/etc/" not in val, "composed creds dir must NOT be under /etc (Req 2.1)"
    assert ".openbao-workload" in val, "composed creds dir should be the ~/.openbao-workload path"


# --------------------------------------------------------------------------- #
# Property 2 — Preservation. PASS on unfixed AND after fix.
# --------------------------------------------------------------------------- #
def test_role_default_creds_dir_and_owner_unchanged():
    """The openbao_workload_approle role defaults stay /etc + root (standalone
    path preserved; the fix lives only in consumer_bootstrap) (Req 3.1)."""
    data = yaml.safe_load(_WA_DEFAULTS.read_text(encoding="utf-8"))
    cd = str(data.get("openbao_workload_creds_dir", ""))
    su = str(data.get("openbao_workload_service_user", ""))
    assert cd == "/etc/openbao-workload/{{ project_slug }}", (
        f"role default openbao_workload_creds_dir must stay /etc/openbao-workload/<slug> (got {cd!r}, Req 3.1)"
    )
    assert su == "root", (
        f"role default openbao_workload_service_user must stay root (got {su!r}, Req 3.1)"
    )


def test_step3_still_passes_primary_delegation_vars():
    """The delegation fix is preserved — STEP 3 still passes the delegate host
    (Req 3.5)."""
    v = _step3_vars()
    assert "openbao_workload_delegate_host" in v, (
        "STEP 3 must still pass openbao_workload_delegate_host (primary delegation preserved, Req 3.5)"
    )


def test_gitlab_channel_and_no_log_unchanged_in_role():
    """The role's gitlab channel + no_log posture are untouched (the fix is
    consumer_bootstrap-only) (Req 3.2, 3.3)."""
    text = (_REPO_ROOT / "ansible" / "roles" / "openbao_workload_approle" / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "openbao_workload_handoff == 'gitlab'" in text, "gitlab channel gate must be unchanged (Req 3.2)"
    assert 'no_log: "{{ openbao_no_log }}"' in text, "no_log posture must be unchanged (Req 3.3)"
    assert 'openbao_workload_secret_id: ""' in text, "STEP 9 Secret_ID scrub must be unchanged (Req 3.3)"
