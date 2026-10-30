"""Offline role-shape + resolution-order + secret-hygiene tests for the SVC-06
org-provision token-wiring feature.

Spec: .kiro/specs/svc06-org-provision-token-wiring/ (ADR-0016, Option A).

Covers tasks:
  2.6 — zitadel-server mint/persist block shape: read-back / obtain-once /
        persist / scrub / fail-fast tasks exist, all no_log, gated on
        zitadel_openbao_addr, persist+obtain gated on "no stored token"
        (idempotency), and the FirstInstance env + guest emit mount are wired.
  4.5 — zitadel-org-provision resolution order: env override wins, else OpenBao
        read; both-empty fails fast naming BOTH sources.
  7.1 — provision_token secret hygiene: never on an argv `=` assignment, every
        task binding/reading/writing it is no_log, dedicated bounded non-human
        machine-user posture (ADR-0016) — NOT a narrower-than-IAM_OWNER grant.

Pure PyYAML + text parse; no ansible-playbook binary, no live ZITADEL.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc06_provision_token_wiring.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLES = _REPO_ROOT / "ansible" / "roles"
_ZS_TASKS = _ROLES / "zitadel-server" / "tasks" / "main.yml"
_ZS_DEFAULTS = _ROLES / "zitadel-server" / "defaults" / "main.yml"
_ZOP_TASKS = _ROLES / "zitadel-org-provision" / "tasks" / "main.yml"
_ZOP_DEFAULTS = _ROLES / "zitadel-org-provision" / "defaults" / "main.yml"

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


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _module_names(t: dict) -> set[str]:
    return {k for k in t if k not in _NON_MODULE_KEYS}


def _no_log_true(t: dict) -> bool:
    v = t.get("no_log")
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1")


def _name(t: dict) -> str:
    return str(t.get("name", "")).lower()


def _when_list(t: dict) -> list[str]:
    w = t.get("when")
    if w is None:
        return []
    if isinstance(w, list):
        return [str(x) for x in w]
    return [str(w)]


def _when_text(t: dict) -> str:
    return " ".join(_when_list(t)).lower()


def _find(tasks, needle: str) -> dict | None:
    for t in tasks:
        if needle in _name(t):
            return t
    return None


# --------------------------------------------------------------------------- #
# 2.6 — zitadel-server first-init mint + persist block shape.
# --------------------------------------------------------------------------- #
class TestZitadelServerMintBlock:
    def setup_method(self):
        self.tasks = _load_tasks(_ZS_TASKS)
        self.defaults = _defaults(_ZS_DEFAULTS)

    def test_defaults_declare_provision_sa_bounded_pat_and_mount(self):
        d = self.defaults
        assert d.get("zitadel_provision_sa_username"), "dedicated machine-user username default missing"
        assert d.get("zitadel_provision_token_field") == "provision_token"
        # Bounded PAT expiry (not a non-expiring credential) — ADR-0016 control.
        exp = str(d.get("zitadel_provision_pat_expiration", ""))
        assert exp and exp.endswith("Z") and "T" in exp, (
            "zitadel_provision_pat_expiration must be a bounded ISO-8601 date"
        )
        # Container emit path + guest mount dir both declared.
        assert d.get("zitadel_provision_pat_emit_path", "").startswith("/")
        assert d.get("zitadel_provision_pat_host_dir", "").startswith("/")

    def test_firstinstance_env_and_emit_mount_wired(self):
        text = _ZS_TASKS.read_text(encoding="utf-8")
        for env_key in (
            "ZITADEL_FIRSTINSTANCE_PATPATH",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_USERNAME",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_NAME",
            "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_PAT_EXPIRATIONDATE",
        ):
            assert env_key in text, f"FirstInstance env {env_key} not wired into the Compose stack"
        # The guest->container emit-dir mount must be present on the service.
        assert "zitadel_provision_pat_host_dir" in text and "zitadel_provision_pat_emit_path | dirname" in text, (
            "the guest PAT-emit dir must be mounted into the container"
        )

    def test_read_back_task_exists_gated_and_no_log(self):
        t = _find(self.tasks, "read any existing provision_token from openbao")
        assert t is not None, "read-back of provision_token from OpenBao is missing"
        assert _no_log_true(t), "read-back must be no_log"
        assert "zitadel_openbao_addr" in _when_text(t), "read-back must be gated on zitadel_openbao_addr"

    def test_obtain_once_gated_on_no_stored_token_and_no_log(self):
        t = _find(self.tasks, "read the first-init pat from the emit file")
        assert t is not None, "obtain-once slurp of the emit file is missing"
        assert _no_log_true(t), "obtain-once must be no_log"
        wt = _when_text(t)
        assert "zitadel_openbao_addr" in wt
        assert "zitadel_provision_token_stored" in wt, "obtain must be gated on 'no stored token'"

    def test_persist_gated_on_no_stored_token_stdin_not_argv_no_log(self):
        t = _find(self.tasks, "persist the obtained provision_token to openbao")
        assert t is not None, "persist of provision_token is missing"
        assert _no_log_true(t), "persist must be no_log"
        wt = _when_text(t)
        assert "zitadel_openbao_addr" in wt
        assert "zitadel_provision_token_stored" in wt, (
            "persist must be gated on 'no stored token' (idempotency: never re-mint/rotate)"
        )
        # Value must travel via stdin/env, NEVER as `provision_token=<value>` on argv.
        mods = _module_names(t)
        body = str({m: t.get(m) for m in mods})
        assert "provision_token=-" in body or "=-" in body, (
            "persist must pass the token over stdin (`field=-`), not inline on argv"
        )
        assert "ZITADEL_PROVISION_TOKEN_VALUE" in body, "the token value must travel in the task environment"

    def test_scrub_task_exists_after_persist(self):
        t = _find(self.tasks, "scrub the first-init pat emit file")
        assert t is not None, "scrub of the emit file after persist is missing"
        wt = _when_text(t)
        assert "zitadel_provision_token_stored" in wt

    def test_fail_fast_when_wired_but_no_token(self):
        t = _find(self.tasks, "fail fast when wired but no provision_token")
        assert t is not None, "fail-fast for the wired-but-no-token case is missing"
        spec = t.get("ansible.builtin.fail") or t.get("fail") or {}
        msg = str(spec.get("msg", "")) if isinstance(spec, dict) else str(spec)
        assert "provision_token" in msg and "emit" in msg.lower(), (
            "fail-fast must name the KV field and the emit path"
        )
        # Must not echo a secret value: references a var name, not an interpolated token.
        assert "zitadel_provision_token }}" not in msg, "fail-fast must not echo the token value"


# --------------------------------------------------------------------------- #
# 4.5 — zitadel-org-provision resolution order.
# --------------------------------------------------------------------------- #
class TestOrgProvisionResolutionOrder:
    def setup_method(self):
        self.tasks = _load_tasks(_ZOP_TASKS)
        self.defaults = _defaults(_ZOP_DEFAULTS)

    def test_defaults_declare_openbao_read_vars(self):
        d = self.defaults
        assert d.get("zop_openbao_provision_token_field") == "provision_token"
        assert "proj-shared-zitadel/zitadel" in str(d.get("zop_openbao_provision_secret_path", "")), (
            "org-provision must read the token from the shared-zitadel KV path"
        )
        for v in ("zop_openbao_cli_host", "zop_openbao_cli_bao_addr", "zop_openbao_cli_user", "zop_openbao_cli_python"):
            assert v in d, f"delegate var {v} missing (mirror of zitadel-server)"

    def test_env_override_bound_no_log(self):
        t = _find(self.tasks, "bind the zitadel_provision_token env override")
        assert t is not None, "env-override bind is missing"
        assert _no_log_true(t)

    def test_openbao_read_gated_on_empty_env_and_wired_no_log(self):
        t = _find(self.tasks, "read provision_token from openbao when env override is empty")
        assert t is not None, "OpenBao read of provision_token is missing"
        assert _no_log_true(t)
        wt = _when_text(t)
        assert "zop_env_token" in wt and "length" in wt, "read must be gated on an EMPTY env token"
        assert "zop_openbao_addr" in wt, "read must be gated on OpenBao being wired"

    def test_resolution_prefers_env_else_openbao(self):
        t = _find(self.tasks, "resolve the provisioning token")
        assert t is not None, "resolution set_fact is missing"
        assert _no_log_true(t)
        spec = t.get("ansible.builtin.set_fact") or t.get("set_fact") or {}
        expr = str(spec.get("zop_provision_token", ""))
        assert "zop_env_token" in expr and "zop_bao_token_read" in expr, (
            "resolution must choose env override else the OpenBao-read value"
        )

    def test_fail_fast_names_both_sources(self):
        t = _find(self.tasks, "assert a provisioning token resolved")
        assert t is not None, "both-sources fail-fast assert is missing"
        assert _no_log_true(t)
        spec = t.get("ansible.builtin.assert") or t.get("assert") or {}
        msg = str(spec.get("fail_msg", ""))
        assert "ZITADEL_PROVISION_TOKEN" in msg, "fail-fast must name the env source"
        # Names the OpenBao source: the field and the KV path (as a var reference).
        assert "provision_token" in msg and "zop_openbao_provision_secret_path" in msg, (
            "fail-fast must name the OpenBao source (field + KV path)"
        )

    def test_helper_token_still_child_env_not_argv(self):
        # Renamed by the two-client onboarding (spec: zitadel-spa-pkce-onboarding)
        # to the per-app-loop "Provision each app via the helper".
        t = _find(self.tasks, "provision each app via the helper")
        assert t is not None
        env = t.get("environment") or {}
        assert "ZITADEL_PROVISION_TOKEN" in env, "token must travel to the helper in the child ENV"
        # The command argv must not carry a --token flag or the token value.
        body = str(t.get("ansible.builtin.command") or t.get("command") or {})
        assert "--token" not in body and "zop_provision_token" not in body, (
            "the token must never appear on the helper argv"
        )


# --------------------------------------------------------------------------- #
# 7.1 — provision_token secret hygiene across both roles.
# --------------------------------------------------------------------------- #
class TestProvisionTokenHygiene:
    # Modules that only carry a diagnostic MESSAGE referencing a var name (no
    # secret value) do not themselves need no_log — e.g. a fail/assert that names
    # the KV path/field. A set_fact binding ONLY a boolean (is-stored?) likewise
    # carries no value.
    _MESSAGE_ONLY_MODULES = {"ansible.builtin.fail", "fail", "ansible.builtin.assert", "assert",
                             "ansible.builtin.debug", "debug"}
    _NON_VALUE_FACTS = {"zitadel_provision_token_stored"}

    def _handles_token_value(self, t: dict) -> bool:
        mods = _module_names(t)
        if mods and mods <= self._MESSAGE_ONLY_MODULES:
            return False
        sf = set()
        for k in ("ansible.builtin.set_fact", "set_fact"):
            spec = t.get(k)
            if isinstance(spec, dict):
                sf = set(spec.keys())
        if sf and sf <= self._NON_VALUE_FACTS:
            return False
        blob = (_name(t) + " " + str({m: t.get(m) for m in mods})
                + " " + str(t.get("environment", ""))).lower()
        return any(k in blob for k in (
            "provision_token", "zop_provision_token", "zop_env_token", "zop_bao_token_read",
        ))

    def test_every_provision_token_value_task_is_no_log(self):
        offenders = []
        for path in (_ZS_TASKS, _ZOP_TASKS):
            for t in _load_tasks(path):
                if self._handles_token_value(t) and not _no_log_true(t):
                    offenders.append(f"{path.name} :: {t.get('name', '<unnamed>')!r}")
        assert not offenders, "provision_token VALUE task(s) not no_log:\n  " + "\n  ".join(offenders)

    def test_token_never_on_argv_assignment(self):
        # No `...provision_token=<jinja value>` on a command argv anywhere — only
        # the stdin form `provision_token=-` is allowed.
        for path in (_ZS_TASKS, _ZOP_TASKS):
            text = path.read_text(encoding="utf-8")
            for bad in (
                "provision_token=' ~ zitadel_provision_token",
                "provision_token={{ zitadel_provision_token",
                "provision_token=' ~ zop_provision_token",
            ):
                assert bad not in text, f"{path.name}: token value assigned on argv ({bad!r})"

    def test_token_not_committed_as_literal(self):
        # Defaults must not contain a literal token value — only names/paths.
        for path in (_ZS_DEFAULTS, _ZOP_DEFAULTS):
            d = _defaults(path)
            tok = str(d.get("zitadel_provision_token", d.get("zop_provision_token", "")))
            assert tok == "", "no provisioning token value may be defaulted/committed"

    def test_dedicated_bounded_nonhuman_posture_adr0016(self):
        # ADR-0016: the posture is a dedicated non-human machine user + bounded
        # PAT expiry + OpenBao-only storage — NOT a narrower-than-IAM_OWNER grant.
        d = _defaults(_ZS_DEFAULTS)
        assert d.get("zitadel_provision_sa_username") and d["zitadel_provision_sa_username"] != "admin", (
            "the provisioning identity must be a dedicated non-human machine user"
        )
        text = _ZS_TASKS.read_text(encoding="utf-8")
        # We must NOT claim to set a narrower instance grant (none exists); the
        # FirstInstance machine user is IAM_OWNER by necessity. Guard against a
        # future edit that tries to pass a bogus narrower-grant env.
        assert "ZITADEL_FIRSTINSTANCE_ORG_MACHINE_MACHINE_ROLE" not in text, (
            "do not attempt a narrower first-init grant — none exists for org creation (ADR-0016)"
        )
