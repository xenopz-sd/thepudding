"""Offline bug-condition exploration + fixed-contract test for the SVC-06
org-provision `delegate_to` crash bugfix.

Spec: .kiro/specs/fix-svc06-org-provision-delegate-to-crash/
      (bugfix.md, design.md, tasks.md).

Background: the one-command SVC-06 Developer-B bring-up stands up a fully
healthy ZITADEL, then dies at its last step — the `zitadel-org-provision`
org-onboard — with (captured live, `no_log` lifted):

    TASK [ZITADEL-org | DEV: resolve the ZITADEL container IP on the guest]
    [ERROR]: Empty hostname produced from delegate_to: "{{ zop_zitadel_guest_host }}"
    fatal: [localhost -> {{ zop_zitadel_guest_host }}]: FAILED!

The defect has two layers:

* Layer A — the crash. The FOUR "DEV guest-run" tasks in
  `ansible/roles/zitadel-org-provision/tasks/main.yml` each carry
  `delegate_to: "{{ zop_zitadel_guest_host }}"` AND `when: zop_dev_guest_run | bool`.
  Ansible evaluates `delegate_to` BEFORE `when`, so when
  `zop_zitadel_guest_host` is EMPTY (the intended PRODUCTION / control-node
  path, where `zop_dev_guest_run` is false) the first DEV task's
  `delegate_to: ""` throws "Empty hostname produced from delegate_to" BEFORE
  the `when: false` can skip it. Net: the PRODUCTION control-node path is DEAD
  CODE, unreachable by construction whenever the guest host is empty.
* Layer B — the homelab path wiring. `svc06_bootstrap/defaults/main.yml` sets
  `svc06_zitadel_dev_guest_host: "{{ '10.0.20.15' if (zitadel_tls_self_signed | default(false) | bool) else '' }}"`,
  so the guest-run path is selected ONLY when `zitadel_tls_self_signed=true`.
  In the real homelab cross-guest h2c topology (ADR-0018) ZITADEL runs cleartext
  behind Traefik, so `zitadel_tls_self_signed` is FALSE, the guest host resolves
  EMPTY, the bring-up takes the (dead) production path, and it fails at
  org-provision. Gating on `zitadel_tls_self_signed` conflates "ZITADEL serves
  its own TLS" with "org-provision must run on the guest."

The fix (operator-approved — fix BOTH):
* Fix A: guard each of the four DEV tasks' `delegate_to` as
  `"{{ zop_zitadel_guest_host if (zop_dev_guest_run | bool) else 'localhost' }}"`
  so an inactive dev path yields a harmless no-op delegate that `when: false`
  cleanly skips, restoring the production control-node path.
* Fix B: decouple `svc06_zitadel_dev_guest_host`'s default from
  `zitadel_tls_self_signed` so the homelab/Traefik-less bring-up selects the
  guest-run path by default.

This module encodes the FIXED contract so it VALIDATES the fix when it passes
after implementation, and so the regression cannot silently return. On the
UNFIXED tree BOTH assertions FAIL — the four DEV tasks carry the bare
`delegate_to: "{{ zop_zitadel_guest_host }}"`, and `svc06_zitadel_dev_guest_host`
IS gated on `zitadel_tls_self_signed` — and that failure CONFIRMS the bug
(bug-condition exploration test).

Pure PyYAML + text parse; no ansible-playbook binary, no live infra. Mirrors the
task-walking helpers (`_iter_tasks` / `_load_tasks`) + the single-doc defaults
load of the sibling tests infra/tests/test_svc07_openbao_metrics_port_publish.py
and infra/tests/test_svc06_zitadel_contract.py.

Run:  set +H
      ~/venv/devinfra/bin/pytest infra/tests/test_svc06_org_provision_delegate_to.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_ORG_PROVISION_TASKS = _ROLES / "zitadel-org-provision" / "tasks" / "main.yml"
_SVC06_BOOTSTRAP_DEFAULTS = _ROLES / "svc06_bootstrap" / "defaults" / "main.yml"

# The guest-host var the bare DEV `delegate_to` references (the defect).
_GUEST_HOST_VAR = "zop_zitadel_guest_host"
# The dev-path selector the guarded `delegate_to` must reference.
_DEV_GUEST_RUN_VAR = "zop_dev_guest_run"
# The safe no-op delegate the inactive-path branch must fall back to.
_LOCALHOST_FALLBACK = "localhost"
# The TLS flag the svc06 dev-guest-host default must NOT be gated on (the defect).
_TLS_FLAG = "zitadel_tls_self_signed"
# The DEV guest-run task name prefix (all four start with this).
_DEV_TASK_PREFIX = "ZITADEL-org | DEV:"
# The svc06_bootstrap var under test.
_DEV_GUEST_HOST_VAR = "svc06_zitadel_dev_guest_host"


# ---------------------------------------------------------------------------
# Shared task-walking helpers (mirror the sibling svc06/svc07 tests)
# ---------------------------------------------------------------------------

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


def _dev_guest_run_tasks(tasks: list[dict]) -> list[dict]:
    """The FOUR "DEV guest-run" tasks — those whose `name` starts with
    "ZITADEL-org | DEV:" (resolve container IP / stage helper / run helper /
    scrub helper). These are exactly the tasks that carry `delegate_to` gated on
    `zop_dev_guest_run`."""
    return [
        t
        for t in tasks
        if str(t.get("name", "")).startswith(_DEV_TASK_PREFIX)
    ]


def _delegate_to_text(task: dict) -> str:
    """Stringify a task's `delegate_to` value (always a Jinja string here)."""
    value = task.get("delegate_to")
    return "" if value is None else str(value)


def _svc06_dev_guest_host_default_text() -> str:
    """The `svc06_zitadel_dev_guest_host` value from svc06_bootstrap/defaults —
    a single-doc mapping (mirrors the sibling db-password tests' defaults load).
    Stringified regardless of shape."""
    doc = yaml.safe_load(_SVC06_BOOTSTRAP_DEFAULTS.read_text(encoding="utf-8")) or {}
    assert isinstance(doc, dict), (
        f"{_SVC06_BOOTSTRAP_DEFAULTS} did not parse to a mapping of defaults."
    )
    assert _DEV_GUEST_HOST_VAR in doc, (
        f"`{_DEV_GUEST_HOST_VAR}` not found in {_SVC06_BOOTSTRAP_DEFAULTS}."
    )
    value = doc[_DEV_GUEST_HOST_VAR]
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Property 1 (A) — the four DEV `delegate_to` must be GUARDED (FAILS on unfixed)
# ---------------------------------------------------------------------------

def test_dev_tasks_delegate_to_is_guarded_not_bare_guest_host():
    """Each of the four "ZITADEL-org | DEV:" guest-run tasks must GUARD its
    `delegate_to` so an EMPTY guest host (the production / dev-path-inactive
    case) yields a safe no-op delegate that `when: false` can cleanly skip —
    NOT the bare `{{ zop_zitadel_guest_host }}` that crashes with "Empty hostname
    produced from delegate_to" BEFORE its `when` can skip it (Ansible evaluates
    `delegate_to` before `when`).

    Concretely, each DEV task's `delegate_to` string must:
      (a) reference `zop_dev_guest_run` (the dev-path selector — a conditional
          expression), AND
      (b) fall back to `localhost` for the inactive path, AND
      (c) NOT be the bare `{{ zop_zitadel_guest_host }}` (optionally whitespaced).

    Unfixed tree: every DEV task carries `delegate_to: "{{ zop_zitadel_guest_host }}"`
    (no `zop_dev_guest_run`, no `localhost`) -> this assertion FAILS, confirming
    the Layer-A crash (the first DEV task's empty `delegate_to` throws on the
    production path, leaving the production control-node path dead code).

    Live corroboration (captured, `no_log` lifted):
    `TASK [ZITADEL-org | DEV: resolve the ZITADEL container IP on the guest]` ->
    `Empty hostname produced from delegate_to: "{{ zop_zitadel_guest_host }}"` /
    `fatal: [localhost -> {{ zop_zitadel_guest_host }}]`, token + slug both fine.
    Also triaged in `.kiro/specs/svc-06-cross-guest-ingress/validation.md`
    lines 489-495.

    Validates: Requirements 1.1, 2.1
    """
    tasks = _load_tasks(_ORG_PROVISION_TASKS)
    dev_tasks = _dev_guest_run_tasks(tasks)

    assert len(dev_tasks) == 4, (
        "expected exactly FOUR 'ZITADEL-org | DEV:' guest-run tasks (resolve "
        "container IP / stage helper / run helper / scrub helper) in "
        f"{_ORG_PROVISION_TASKS}; found {len(dev_tasks)}: "
        f"{[t.get('name') for t in dev_tasks]}"
    )

    for t in dev_tasks:
        name = t.get("name")
        delegate_text = _delegate_to_text(t)
        collapsed = delegate_text.replace(" ", "")

        assert delegate_text, (
            f"the DEV task {name!r} must declare a `delegate_to` value (found "
            f"none in {_ORG_PROVISION_TASKS})."
        )

        # (c) It must NOT be the bare `{{ zop_zitadel_guest_host }}` — the defect.
        assert collapsed != "{{" + _GUEST_HOST_VAR + "}}", (
            f"the DEV task {name!r} carries the BARE "
            f"`delegate_to: \"{{{{ {_GUEST_HOST_VAR} }}}}\"`. Ansible evaluates "
            "`delegate_to` BEFORE `when`, so when the guest host is EMPTY (the "
            "production / dev-path-inactive case) this throws 'Empty hostname "
            "produced from delegate_to' before `when: false` can skip the task "
            "— leaving the production control-node path dead code. It must be "
            "guarded, e.g. "
            f"`{{{{ {_GUEST_HOST_VAR} if ({_DEV_GUEST_RUN_VAR} | bool) "
            f"else '{_LOCALHOST_FALLBACK}' }}}}`. Current delegate_to:\n"
            f"{delegate_text}"
        )

        # (a) It must reference the dev-path selector (a conditional guard).
        assert _DEV_GUEST_RUN_VAR in delegate_text, (
            f"the DEV task {name!r} `delegate_to` must be GUARDED by a "
            f"conditional referencing `{_DEV_GUEST_RUN_VAR}` so an inactive dev "
            "path yields a safe delegate. Current delegate_to:\n"
            f"{delegate_text}"
        )

        # (b) It must fall back to localhost for the inactive path.
        assert _LOCALHOST_FALLBACK in delegate_text, (
            f"the DEV task {name!r} `delegate_to` must fall back to "
            f"'{_LOCALHOST_FALLBACK}' (a harmless no-op delegate for a task that "
            "`when: false` skips anyway) when the dev path is inactive. Current "
            f"delegate_to:\n{delegate_text}"
        )


# ---------------------------------------------------------------------------
# Property 1 (B) — the svc06 dev-guest-host default must NOT be gated on the TLS
# flag (FAILS on unfixed)
# ---------------------------------------------------------------------------

def test_svc06_dev_guest_host_default_not_gated_on_tls_self_signed():
    """`svc06_zitadel_dev_guest_host`'s default must NOT condition on
    `zitadel_tls_self_signed`. Gating the homelab guest-run path on self-signed
    TLS conflates "ZITADEL serves its own TLS" with "org-provision must run on
    the guest" — false for the real cross-guest cleartext-h2c-behind-Traefik
    topology (ADR-0018), where `zitadel_tls_self_signed` is FALSE, so the default
    resolves EMPTY, the bring-up takes the (dead) production path, and
    org-provision fails.

    Robust check: the stringified default value must NOT contain
    `zitadel_tls_self_signed`.

    Unfixed tree: the default IS
    `{{ '10.0.20.15' if (zitadel_tls_self_signed | default(false) | bool) else '' }}`
    -> contains `zitadel_tls_self_signed` -> this assertion FAILS, confirming the
    Layer-B wiring defect (the homelab h2c bring-up resolves the guest host EMPTY
    and hits the Layer-A crash).

    Validates: Requirements 1.2, 2.2
    """
    default_text = _svc06_dev_guest_host_default_text()

    assert default_text, (
        f"`{_DEV_GUEST_HOST_VAR}` must declare a non-empty default expression in "
        f"{_SVC06_BOOTSTRAP_DEFAULTS}."
    )

    assert _TLS_FLAG not in default_text, (
        f"`{_DEV_GUEST_HOST_VAR}`'s default is gated on `{_TLS_FLAG}`, which ties "
        "the homelab guest-run org-provision path to self-signed TLS. In the real "
        "cross-guest cleartext-h2c-behind-Traefik topology (ADR-0018) "
        f"`{_TLS_FLAG}` is FALSE, so the default resolves EMPTY, the bring-up "
        "takes the (dead) production path, and org-provision fails. The default "
        "must select the guest-run path for the homelab bring-up REGARDLESS of "
        f"`{_TLS_FLAG}` (with an explicit production opt-in). Current default:\n"
        f"{default_text}"
    )


# ===========================================================================
# Property 2 — PRESERVATION (Task 2). Observation-first: these pin UNCHANGED
# behaviours that already hold on the unfixed tree and MUST keep holding after
# the fix. Only `delegate_to` (Fix A) and the svc06 dev-guest-host default
# (Fix B) change — everything asserted below is untouched by the fix.
# ===========================================================================

# The production control-node task name (the single `when: not (...)` task).
_PROD_TASK_NAME = (
    "ZITADEL-org | Provision org/roles/OIDC-app via the helper (idempotent, no_log)"
)
# The fail-fast token assert task name (names BOTH sources).
_TOKEN_ASSERT_NAME = (
    "ZITADEL-org | Assert a provisioning token resolved (fail fast, names both sources)"
)
# The OpenBao OIDC-secret write task name.
_OIDC_WRITE_NAME = (
    "ZITADEL-org | Persist the new OIDC client_id/secret to OpenBao (no_log)"
)


def _task_by_name(tasks: list[dict], name: str) -> dict:
    for t in tasks:
        if str(t.get("name", "")) == name:
            return t
    raise AssertionError(
        f"expected a task named {name!r} in {_ORG_PROVISION_TASKS}; "
        f"found: {[t.get('name') for t in tasks]}"
    )


def _when_text(task: dict) -> str:
    """Stringify a task's `when` (may be a scalar or a list of conditions)."""
    value = task.get("when")
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def _set_fact_var_names(task: dict) -> set[str]:
    """The var names bound by a `set_fact` task (empty set if not a set_fact)."""
    sf = task.get("set_fact") or task.get("ansible.builtin.set_fact")
    if not isinstance(sf, dict):
        return set()
    return set(sf.keys())


def _argv_text(task: dict) -> str:
    """Stringify a command task's argv (list or folded string), else ''."""
    cmd = task.get("command") or task.get("ansible.builtin.command")
    if isinstance(cmd, dict):
        argv = cmd.get("argv")
        if isinstance(argv, list):
            return " ".join(str(a) for a in argv)
        if argv is not None:
            return str(argv)
    return ""


# ---------------------------------------------------------------------------
# (a) guest-run `when` gating preserved on all four DEV tasks + the production
#     path's `when: not (...)` preserved.
# ---------------------------------------------------------------------------

def test_preservation_dev_tasks_keep_dev_guest_run_when_and_prod_keeps_negated():
    """The four DEV tasks STILL carry `when: zop_dev_guest_run | bool` (only
    `delegate_to` changed, not the `when`), and the production control-node task
    STILL carries `when: not (zop_dev_guest_run | bool)`.

    Holds on the unfixed tree (baseline); must keep holding after Fix A.

    Validates: Requirements 3.1
    """
    tasks = _load_tasks(_ORG_PROVISION_TASKS)
    dev_tasks = _dev_guest_run_tasks(tasks)

    assert len(dev_tasks) == 4, (
        "expected exactly FOUR 'ZITADEL-org | DEV:' guest-run tasks; found "
        f"{len(dev_tasks)}: {[t.get('name') for t in dev_tasks]}"
    )

    for t in dev_tasks:
        when_text = _when_text(t).replace(" ", "")
        assert when_text == "{{zop_dev_guest_run|bool}}" or (
            _DEV_GUEST_RUN_VAR in when_text and "not" not in when_text
        ), (
            f"the DEV task {t.get('name')!r} must keep "
            f"`when: {_DEV_GUEST_RUN_VAR} | bool` unchanged; found when: "
            f"{_when_text(t)!r}"
        )

    prod = _task_by_name(tasks, _PROD_TASK_NAME)
    prod_when = _when_text(prod).replace(" ", "")
    assert _DEV_GUEST_RUN_VAR in prod_when and "not" in prod_when, (
        "the production control-node task must keep "
        f"`when: not ({_DEV_GUEST_RUN_VAR} | bool)`; found when: "
        f"{_when_text(prod)!r}"
    )


# ---------------------------------------------------------------------------
# (b) token-resolution set_fact chain + fail-fast assert present and no_log.
# ---------------------------------------------------------------------------

def test_preservation_token_resolution_chain_and_assert_present_and_no_log():
    """The env-override-else-OpenBao token resolution `set_fact` chain is present
    and `no_log` — specifically tasks binding `zop_env_token`, the OpenBao read
    registering `zop_bao_token_read`, and the resolve binding `zop_provision_token`
    — plus the fail-fast assert that names BOTH sources. All token-handling tasks
    stay `no_log`.

    Holds on the unfixed tree (baseline); the fix does not touch token resolution.

    Validates: Requirements 3.2
    """
    tasks = _load_tasks(_ORG_PROVISION_TASKS)

    # set_fact binding zop_env_token (the env override), no_log.
    env_tasks = [t for t in tasks if "zop_env_token" in _set_fact_var_names(t)]
    assert env_tasks, "expected a set_fact binding `zop_env_token` (env override)."
    for t in env_tasks:
        assert t.get("no_log") is True, (
            f"the `zop_env_token` set_fact {t.get('name')!r} must be no_log."
        )

    # The OpenBao read registering zop_bao_token_read, no_log.
    read_tasks = [t for t in tasks if t.get("register") == "zop_bao_token_read"]
    assert read_tasks, (
        "expected the OpenBao token-read task registering `zop_bao_token_read`."
    )
    for t in read_tasks:
        assert t.get("no_log") is True, (
            f"the OpenBao token-read {t.get('name')!r} must be no_log."
        )

    # set_fact resolving zop_provision_token (env wins else OpenBao), no_log.
    resolve_tasks = [
        t for t in tasks if "zop_provision_token" in _set_fact_var_names(t)
    ]
    assert resolve_tasks, (
        "expected a set_fact resolving `zop_provision_token`."
    )
    for t in resolve_tasks:
        assert t.get("no_log") is True, (
            f"the `zop_provision_token` resolve {t.get('name')!r} must be no_log."
        )

    # The fail-fast assert naming BOTH sources, no_log.
    assert_task = _task_by_name(tasks, _TOKEN_ASSERT_NAME)
    assert assert_task.get("no_log") is True, (
        "the token-resolved fail-fast assert must be no_log."
    )
    assert_block = assert_task.get("assert") or assert_task.get(
        "ansible.builtin.assert"
    )
    assert isinstance(assert_block, dict), (
        "the token assert must carry an `assert` block."
    )
    fail_msg = str(assert_block.get("fail_msg", ""))
    assert "ZITADEL_PROVISION_TOKEN" in fail_msg and "OpenBao" in fail_msg, (
        "the token assert fail_msg must name BOTH sources "
        "(the ZITADEL_PROVISION_TOKEN env override AND OpenBao); found:\n"
        f"{fail_msg}"
    )


# ---------------------------------------------------------------------------
# (c) OpenBao OIDC-secret write gate + no_log + token not on bare argv.
# ---------------------------------------------------------------------------

def test_preservation_oidc_secret_write_gate_nolog_and_token_in_exec_env():
    """The OpenBao OIDC-secret write task is still gated on
    `zop_openbao_addr | length > 0` AND `not (...already_existed...)` AND a
    non-empty client_secret, still `no_log`, and the OpenBao token travels only
    in the delegated docker-exec ENV (`BAO_TOKEN=`), never on a bare argv `=`
    assignment of OPENBAO_ADMIN_TOKEN / token.

    Holds on the unfixed tree (baseline); the fix does not touch the OIDC write.

    Validates: Requirements 3.3
    """
    tasks = _load_tasks(_ORG_PROVISION_TASKS)
    write = _task_by_name(tasks, _OIDC_WRITE_NAME)

    assert write.get("no_log") is True, (
        "the OIDC-secret write task must be no_log."
    )

    when_text = _when_text(write)
    collapsed = when_text.replace(" ", "")
    assert "zop_openbao_addr|length>0" in collapsed, (
        "the OIDC-secret write must stay gated on `zop_openbao_addr | length > 0`; "
        f"found when: {when_text!r}"
    )
    assert "already_existed" in collapsed and "not" in collapsed, (
        "the OIDC-secret write must stay gated on a newly-created secret "
        "(`not (...already_existed...)`); found when: "
        f"{when_text!r}"
    )
    assert "client_secret" in when_text and "length" in collapsed, (
        "the OIDC-secret write must stay gated on a non-empty client_secret; "
        f"found when: {when_text!r}"
    )

    argv_text = _argv_text(write)
    # The token must travel in the delegated exec ENV via `BAO_TOKEN=`.
    assert "BAO_TOKEN=" in argv_text, (
        "the OpenBao token must be passed via the delegated docker-exec ENV "
        "(`-e BAO_TOKEN=...`); found argv:\n"
        f"{argv_text}"
    )
    # The token must NOT appear as a bare argv `=` assignment of the admin token
    # or a top-level `token=` kv argument.
    assert "OPENBAO_ADMIN_TOKEN=" not in argv_text, (
        "the admin token must never be assigned on a bare argv `=`; found argv:\n"
        f"{argv_text}"
    )
    assert "token=" not in argv_text.replace("BAO_TOKEN=", ""), (
        "no bare `token=` argv assignment is permitted (token only in exec ENV); "
        f"found argv:\n{argv_text}"
    )


# ---------------------------------------------------------------------------
# (d) production control-node path still invokes the helper with --base-url
#     {{ zop_base_url }}.
# ---------------------------------------------------------------------------

def test_preservation_production_path_uses_base_url_zop_base_url():
    """The production control-node task still invokes the helper with
    `--base-url {{ zop_base_url }}` (the Traefik-fronted
    `https://auth.<platform_domain>`), unchanged.

    Holds on the unfixed tree (baseline); the fix does not touch the production
    helper invocation.

    Validates: Requirements 3.4
    """
    tasks = _load_tasks(_ORG_PROVISION_TASKS)
    prod = _task_by_name(tasks, _PROD_TASK_NAME)
    argv_text = _argv_text(prod)

    assert "--base-url" in argv_text, (
        "the production task must invoke the helper with `--base-url`; found argv:\n"
        f"{argv_text}"
    )
    collapsed = argv_text.replace(" ", "")
    assert "{{zop_base_url}}" in collapsed, (
        "the production task's `--base-url` must be `{{ zop_base_url }}`; found "
        f"argv:\n{argv_text}"
    )
