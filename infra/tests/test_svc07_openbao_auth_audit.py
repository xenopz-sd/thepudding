"""Offline structural-contract tests for the task-7.2 auth/audit/promtail wiring.

Task 7.2 (spec: svc-07-secrets-manager) — requirements.md Requirements 4.1, 6.5,
6.6, 7.1, 7.2, 7.3, 7.4, 7.6; design.md "Traefik ingress and UI auth" +
"Error Handling (audit fail-closed)".

These are OFFLINE, dependency-light assertions over the ``openbao_init_unseal``
role's task-7.2 additions to ``tasks/engines_auth_audit.yml`` (the §7.2 section),
the §7.2 vars added to ``defaults/main.yml``, and the new Promtail scrape-config
template ``templates/promtail-openbao.yml.j2``. They are NOT property-based tests
and NOT ``requires_infra``: there is no live OpenBao/Docker here. They pin the
auth/audit/promtail contract the task's brief and the secrets-manager steering
demand:

  * ``jwt`` auth method enabled-if-absent + configured with ``bound_issuer`` =
    GitLab URL and ``oidc_discovery_url`` = the GitLab OIDC discovery endpoint
    (Req 4.1) — METHOD only; per-project roles are onboarding (task 9.1);
  * ``oidc`` auth method enabled-if-absent + configured against ZITADEL as the UI
    human-auth path, with the client secret from a never-committed env var
    (Req 6.5);
  * local human-operator auth (``userpass``) is disabled and its absence
    asserted, and the enforcement of "no local human login completes" (Req 6.6)
    is documented in-file;
  * a ``file`` audit device enabled-if-absent writing JSON-per-line to a path on
    the mounted audit volume, with OpenBao's native fail-closed behaviour NOT
    overridden (no ``log_raw``) — Req 7.1/7.2/7.3/7.6;
  * a Promtail scrape config tailing the audit log across rotations and shipping
    to Loki tagged ``service=openbao``/``vlan=20`` (Req 7.4);
  * every enable is check-then-enable against the parsed ``bao auth list`` /
    ``bao audit list`` maps, so a re-run is a no-op (Req 13 idempotency);
  * NO secret value is defaulted anywhere — the OIDC client secret is resolved
    from a never-committed env var NAME, and every task carrying a credential (or
    ``BAO_TOKEN``, the Root_Token) sets ``no_log: true``;
  * the §7.3 marker is left intact (task 7.3 fills it after this task).

The whole suite is offline and always runs (no gating).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_auth_audit.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_ENGINES = _ROLE_DIR / "tasks" / "engines_auth_audit.yml"
_PROMTAIL_TMPL = _ROLE_DIR / "templates" / "promtail-openbao.yml.j2"
_PRIMARY_CONFIG_TMPL = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_install" / "templates" / "config.hcl.j2"
)

#: Runtime-secret-carrying references. Any task whose body references one of
#: these MUST set no_log: true (the exec env carries the Root_Token, or the
#: write carries a client credential, or the register is a raw root-token exec
#: stdout).
#:
#: NOTE the parsed maps ``openbao_auth_map`` / ``openbao_audit_map`` are NOT
#: listed here: they hold only auth-method / audit-device MOUNT-PATH NAMES (e.g.
#: "jwt/", "file/"), not secret material — an ``assert``/``when`` that inspects
#: them carries no credential and benefits from visible diagnostics, so it is
#: intentionally exempt from the no_log requirement. The raw ``register`` names
#: of the underlying ``bao … list`` execs (whose exec env carries the Root_Token)
#: ARE listed, so the list reads themselves must stay no_log.
_SECRET_REFS = (
    "BAO_TOKEN=",
    "openbao_root_token",
    "openbao_oidc_client_secret",
    "openbao_primary_auth_methods",
    "openbao_primary_audit_devices",
)


# --- 30th correction: no_log may be True OR the dev-opt-in template that hides by
# default. `no_log: "{{ openbao_no_log }}"` resolves to True unless a developer
# passes -e openbao_dev_expose_secrets=true for a single debugging run
# (security-standards.md "Secrets handling"). Both forms satisfy the
# "secrets hidden by default" discipline.
_NO_LOG_HIDDEN_BY_DEFAULT = (True, "{{ openbao_no_log }}")


def _no_log_hidden(task: dict) -> bool:
    return task.get("no_log") in _NO_LOG_HIDDEN_BY_DEFAULT


@pytest.fixture(scope="module")
def engine_tasks() -> list[dict]:
    tasks = yaml.safe_load(_ENGINES.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and tasks, "engines_auth_audit.yml must be a task list"
    return tasks


@pytest.fixture(scope="module")
def defaults() -> dict:
    raw = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


def _names(tasks: list[dict]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def _task(tasks: list[dict], needle: str) -> dict:
    for t in tasks:
        if needle in t.get("name", ""):
            return t
    raise AssertionError(f"no task whose name contains {needle!r}; names={_names(tasks)}")


def _argv(task: dict):
    return task["ansible.builtin.command"]["argv"]


def _argv_tokens(argv) -> list[str]:
    """Return the argv token list whether ``argv`` is a plain YAML list OR a
    Jinja list-concat expression string.

    The primary-side ``bao`` docker-exec tasks build their ``argv`` as a Jinja
    expression that conditionally splices ``-tls-skip-verify``, e.g.::

        argv: >-
          {{ ['docker','exec', ..., 'auth','enable']
             + (['-tls-skip-verify'] if (openbao_primary_tls_skip_verify | bool) else [])
             + ['-path=' ~ openbao_jwt_mount_path, 'jwt'] }}

    so ``task["ansible.builtin.command"]["argv"]`` is a single *string*, not a
    Python list. This helper yields the ordered list of string-literal tokens
    inside that expression so token/substring assertions work regardless of the
    ``-tls-skip-verify`` splice. When ``argv`` is already a plain YAML list it is
    returned as-is (stringified).

    NOTE on concatenation: an item like ``bound_issuer={{ openbao_jwt_gitlab_url }}``
    in the OLD static shape becomes ``'bound_issuer=' ~ openbao_jwt_gitlab_url`` in
    the expression — the literal PREFIX ``bound_issuer=`` is a token and the var
    is separate. Tests that verify such items assert BOTH the literal prefix
    token is present AND the var name appears in the raw ``argv`` expression
    string (see ``_argv_raw``).
    """
    if isinstance(argv, list):
        return [str(a) for a in argv]
    return [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", argv)]


def _argv_raw(argv) -> str:
    """Return the raw ``argv`` as a single string for substring/var checks."""
    if isinstance(argv, list):
        return " ".join(str(a) for a in argv)
    return argv


def _asserts_tls_skip_splice(argv) -> None:
    """Assert the argv expression carries the gated ``-tls-skip-verify`` splice.

    Locks in the bugfix: every primary-side ``bao`` exec must conditionally add
    ``-tls-skip-verify`` gated on ``openbao_primary_tls_skip_verify``.
    """
    raw = _argv_raw(argv)
    assert "-tls-skip-verify" in raw, f"missing -tls-skip-verify splice in argv: {raw}"
    assert "openbao_primary_tls_skip_verify" in raw, (
        f"-tls-skip-verify not gated on openbao_primary_tls_skip_verify: {raw}"
    )


class TestSectionMarkersIntact:
    """§7.2 filled; §7.3 now filled too (task 7.3 landed its rotate-root wiring)."""

    def test_all_three_section_markers_present(self):
        text = _ENGINES.read_text(encoding="utf-8")
        assert "[TASK 7.1]" in text
        assert "[TASK 7.2]" in text
        assert "[TASK 7.3]" in text

    def test_73_placeholder_now_filled_by_task_73(self):
        text = _ENGINES.read_text(encoding="utf-8")
        # task 7.3's insertion placeholder is GONE once §7.3 is implemented; the
        # rotate-root wiring (verified in detail by test_svc07_openbao_rotate_root.py)
        # replaced it. This §7.2 suite only pins that §7.2 stays above §7.3.
        assert "(task 7.3: rotate-root wiring goes HERE)" not in text

    def test_72_ordering_before_73(self):
        text = _ENGINES.read_text(encoding="utf-8")
        # every §7.2 task must sit above the §7.3 marker.
        marker_73 = text.index("§7.3 — ROOT-CREDENTIAL ROTATION WIRING")
        assert text.index("§7.2 — AUTH METHODS") < marker_73
        # the last §7.2 task (promtail render) is above the 7.3 marker.
        assert text.index("Render the Promtail audit-tail scrape config") < marker_73


class TestJwtMethod:
    """Req 4.1 — jwt enabled-if-absent + bound_issuer + oidc_discovery_url."""

    def test_jwt_enabled_if_absent(self, engine_tasks, defaults):
        t = _task(engine_tasks, "Enable the jwt auth method")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        assert "auth" in tokens and "enable" in tokens and "jwt" in tokens
        # `-path=` literal prefix + the mount var spliced separately
        assert "-path=" in tokens
        assert "openbao_jwt_mount_path" in _argv_raw(argv)
        _asserts_tls_skip_splice(argv)
        # enable-if-absent: gated on the auth-methods map
        assert "openbao_auth_map" in yaml.safe_dump(t["when"])
        assert defaults["openbao_jwt_mount_path"] == "jwt"

    def test_jwt_config_sets_bound_issuer_and_discovery(self, engine_tasks, defaults):
        t = _task(engine_tasks, "Configure auth/jwt/config")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        # the config path is built as `'auth/' ~ openbao_jwt_mount_path ~ '/config'`:
        # literal prefix `auth/` + literal suffix `/config` tokens, var spliced.
        assert "auth/" in tokens
        assert "/config" in tokens
        assert "openbao_jwt_mount_path" in raw
        # bound_issuer / oidc_discovery_url are concat literal prefixes + vars.
        assert "bound_issuer=" in tokens
        assert "openbao_jwt_gitlab_url" in raw
        assert "oidc_discovery_url=" in tokens
        assert "openbao_jwt_oidc_discovery_url" in raw
        _asserts_tls_skip_splice(argv)
        # the placeholders are non-secret URLs, not literals-in-task
        assert defaults["openbao_jwt_gitlab_url"].startswith("https://")
        assert defaults["openbao_jwt_oidc_discovery_url"].startswith("https://")
        # a platform audience placeholder exists for onboarding + task 7.4
        assert defaults["openbao_jwt_bound_audience"]

    def test_jwt_config_is_opt_in_off_by_default(self, engine_tasks, defaults):
        """jwt/config is deferred behind openbao_configure_jwt (default false).

        The config write validates the OIDC discovery URL against a live IdP
        (GitLab/SVC-13) at config time, which is not deployed on a base bring-up,
        so it must be gated (29th correction). The METHOD enable stays ungated.
        """
        assert defaults.get("openbao_configure_jwt") is False, (
            "openbao_configure_jwt must default false — jwt/config validates the "
            "IdP discovery URL and fails when GitLab is not yet deployed"
        )
        cfg = _task(engine_tasks, "Configure auth/jwt/config")
        assert "openbao_configure_jwt" in yaml.safe_dump(cfg["when"])
        # the enable must NOT be gated on the configure flag
        enable = _task(engine_tasks, "Enable the jwt auth method")
        assert "openbao_configure_jwt" not in yaml.safe_dump(enable.get("when", ""))


class TestOidcMethod:
    """Req 6.5 — oidc enabled-if-absent + ZITADEL config + client secret from env."""

    def test_oidc_enabled_if_absent(self, engine_tasks, defaults):
        t = _task(engine_tasks, "Enable the oidc auth method")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        assert "auth" in tokens and "enable" in tokens and "oidc" in tokens
        assert "-path=" in tokens
        assert "openbao_oidc_mount_path" in _argv_raw(argv)
        _asserts_tls_skip_splice(argv)
        assert "openbao_auth_map" in yaml.safe_dump(t["when"])
        assert defaults["openbao_oidc_mount_path"] == "oidc"

    def test_oidc_config_targets_zitadel_with_client_secret_from_env(self, engine_tasks, defaults):
        t = _task(engine_tasks, "Configure auth/oidc/config against ZITADEL")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        # config path built as `'auth/' ~ openbao_oidc_mount_path ~ '/config'`
        assert "auth/" in tokens
        assert "/config" in tokens
        assert "openbao_oidc_mount_path" in raw
        # each key= is a concat literal prefix + var.
        assert "oidc_discovery_url=" in tokens
        assert "openbao_oidc_discovery_url" in raw
        assert "oidc_client_id=" in tokens
        assert "openbao_oidc_client_id" in raw
        # the client secret comes from a var (resolved from env), never a literal
        assert "oidc_client_secret=" in tokens
        assert "openbao_oidc_client_secret" in raw
        assert "default_role=" in tokens
        assert "openbao_oidc_default_role" in raw
        _asserts_tls_skip_splice(argv)
        # discovery is ZITADEL, not GitLab
        assert defaults["openbao_oidc_discovery_url"].startswith("https://")
        assert defaults["openbao_oidc_client_id"]

    def test_oidc_client_secret_resolved_from_env_and_asserted(self, engine_tasks, defaults):
        # the secret is set_fact from an env lookup keyed on the env var NAME
        resolve = _task(engine_tasks, "Resolve the OIDC client secret")
        body = yaml.safe_dump(resolve)
        assert "ansible.builtin.env" in body
        assert "openbao_oidc_client_secret_env_var" in body
        # and there is a fail-closed assert when the secret is empty
        assertion = _task(engine_tasks, "Assert the OIDC client secret was supplied")
        assert "openbao_oidc_client_secret" in yaml.safe_dump(assertion["ansible.builtin.assert"]["that"])
        # only the env var NAME is defaulted, never a secret value
        assert defaults["openbao_oidc_client_secret_env_var"]
        assert "openbao_oidc_client_secret" not in defaults

    def test_oidc_default_role_maps_to_platform_admin(self, engine_tasks, defaults):
        t = _task(engine_tasks, "Write the default oidc role")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        # role path built as `'auth/' ~ openbao_oidc_mount_path ~ '/role/' ~
        # openbao_oidc_default_role`: the `auth/` and `/role/` literals are tokens
        # and both vars are spliced.
        assert "auth/" in tokens
        assert "/role/" in tokens
        assert "openbao_oidc_mount_path" in raw
        assert "openbao_oidc_default_role" in raw
        _asserts_tls_skip_splice(argv)
        # a successful ZITADEL human login maps to the break-glass platform-admin
        assert defaults["openbao_oidc_default_role_token_policies"] == [
            "{{ openbao_platform_admin_policy_name }}"
        ]


    def test_oidc_config_is_opt_in_off_by_default(self, engine_tasks, defaults):
        """oidc config/secret/role are deferred behind openbao_configure_oidc (false).

        The oidc config write validates the ZITADEL (SVC-06) discovery URL and
        carries the client secret; the secret-assert fails closed if unset. On a
        base bring-up ZITADEL is not deployed, so the resolve/assert/config/role
        tasks must be gated (29th correction). The METHOD enable stays ungated.
        """
        assert defaults.get("openbao_configure_oidc") is False, (
            "openbao_configure_oidc must default false — oidc config validates the "
            "ZITADEL discovery URL and fail-closes on a missing client secret"
        )
        for name in (
            "Resolve the OIDC client secret",
            "Assert the OIDC client secret was supplied",
            "Configure auth/oidc/config against ZITADEL",
            "Write the default oidc role",
        ):
            tk = _task(engine_tasks, name)
            assert "openbao_configure_oidc" in yaml.safe_dump(tk.get("when", "")), (
                f"oidc task {name!r} must be gated on openbao_configure_oidc"
            )
        enable = _task(engine_tasks, "Enable the oidc auth method")
        assert "openbao_configure_oidc" not in yaml.safe_dump(enable.get("when", ""))


class TestLocalHumanAuthDisabled:
    """Req 6.6 — userpass disabled + absence asserted; enforcement documented."""

    def test_userpass_in_disable_list(self, defaults):
        assert "userpass" in defaults["openbao_local_human_auth_paths_to_disable"]

    def test_disable_task_is_conditional_and_looped(self, engine_tasks):
        t = _task(engine_tasks, "Disable any enabled local human-operator auth method")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        assert "auth" in tokens and "disable" in tokens
        _asserts_tls_skip_splice(argv)
        assert t["loop"] == "{{ openbao_local_human_auth_paths_to_disable }}"
        # only disables when actually present (idempotent no-op on a clean run)
        assert "openbao_auth_map" in yaml.safe_dump(t["when"])

    def test_absence_reasserted_after_disable(self, engine_tasks):
        # a re-list happens after the disable, then an assert-absent over the list
        _task(engine_tasks, "Re-list auth methods after the local-auth disable")
        assertion = _task(engine_tasks, "Assert no local human-operator auth method remains enabled")
        that = yaml.safe_dump(assertion["ansible.builtin.assert"]["that"])
        assert "openbao_auth_map_after" in that
        assert "is none" in that
        assert assertion["loop"] == "{{ openbao_local_human_auth_paths_to_disable }}"

    def test_req66_enforcement_documented(self):
        """Req 6.6: how "no local human login completes" is enforced is stated."""
        text = _ENGINES.read_text(encoding="utf-8")
        # the root-token UI form neutralisation is tied to the 6.2 revoke
        assert "root-token UI form" in text
        assert "6.2-STEP-5" in text or "Root_Token being revoked" in text
        # the built-in token method caveat is documented
        assert "built-in `token` method" in text


class TestFileAuditDevice:
    """Req 7.1/7.2/7.3/7.6 — file audit, JSON-per-line, fail-closed not overridden."""

    def test_audit_device_declared_in_primary_config_stanza(self, defaults):
        """Req 7.1 — the file audit device is DECLARED in config.hcl (OpenBao 2.4).

        OpenBao 2.4 removed API-driven audit enablement (`bao audit enable` ->
        `400: cannot enable audit device via API; use declarative, config-based
        audit device management instead`, hardening after CVE-2025-54997). The
        device MUST therefore be declared in the primary config.hcl `audit "file"`
        stanza, created by OpenBao at startup/SIGHUP — NOT enabled at runtime.
        """
        cfg = _PRIMARY_CONFIG_TMPL.read_text(encoding="utf-8")
        # declarative audit stanza present — HCL two-label form: TYPE then PATH,
        # i.e. `audit "file" "<path>" { ... }` (a nested `<path> {}` block instead
        # makes OpenBao report `audit type must be specified`, 32nd correction).
        assert 'audit "file" "{{ openbao_audit_device_path }}"' in cfg
        assert "file_path" in cfg and "openbao_audit_log_file" in cfg
        assert "openbao_audit_log_format" in cfg
        # JSON-per-line contract pinned (Req 7.1)
        assert defaults["openbao_audit_log_format"] == "json"
        assert defaults["openbao_audit_device_path"] == "file"

    def test_role_verifies_declarative_audit_device_present(self, engine_tasks):
        """The role VERIFIES the config-declared device is live (no API enable)."""
        # there is NO `bao audit enable` task anymore
        names = " ".join(tk.get("name", "") for tk in engine_tasks)
        assert "Enable the file audit device" not in names, (
            "audit device must be declarative (config.hcl), not enabled via API "
            "on OpenBao 2.4"
        )
        # instead: a `bao audit list` verify + an assert-present
        v = _task(engine_tasks, "Verify the declarative file audit device is present")
        argv = _argv(v)
        tokens = _argv_tokens(argv)
        assert "audit" in tokens and "list" in tokens
        _asserts_tls_skip_splice(argv)
        a = _task(engine_tasks, "Assert the config-declared file audit device is enabled")
        that = yaml.safe_dump(a["ansible.builtin.assert"]["that"])
        assert "openbao_audit_device_path" in that and "from_json" in that

    def test_audit_log_on_mounted_audit_dir(self, defaults):
        # the audit file lives under the mounted audit volume dir (/openbao/audit)
        assert defaults["openbao_audit_dir"] == "/openbao/audit"
        assert "{{ openbao_audit_dir }}" in _DEFAULTS.read_text(encoding="utf-8")
        # openbao_audit_log_file templates onto that dir
        assert "openbao_audit_dir" in str(
            yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))["openbao_audit_log_file"]
        )

    def test_fail_closed_not_overridden(self):
        """OpenBao's native fail-closed audit must NOT be relaxed (no log_raw)."""
        # the declarative audit stanza in config.hcl must not set log_raw (which
        # would disable HMAC / relax handling)
        cfg = _PRIMARY_CONFIG_TMPL.read_text(encoding="utf-8")
        import re
        m = re.search(r'audit "file" "\{\{ openbao_audit_device_path \}\}".*?\n\}', cfg, re.DOTALL)
        assert m is not None, "audit stanza not found in primary config.hcl"
        assert "log_raw" not in m.group(0)
        # the fail-closed native behaviour is documented as preserved (Req 7.3/7.6)
        text = _ENGINES.read_text(encoding="utf-8")
        assert "fail-closed" in text.lower()
        assert "refuse" in text.lower() or "refuses" in text.lower()


class TestPromtailWiring:
    """Req 7.4 — Promtail tails audit log across rotations -> Loki service/vlan."""

    def test_promtail_dir_and_template_tasks_present(self, engine_tasks):
        d = _task(engine_tasks, "Ensure the Promtail config directory exists")
        assert d["ansible.builtin.file"]["state"] == "directory"
        r = _task(engine_tasks, "Render the Promtail audit-tail scrape config")
        assert r["ansible.builtin.template"]["src"] == "promtail-openbao.yml.j2"
        assert "openbao_promtail_config_file" in r["ansible.builtin.template"]["dest"]

    def test_promtail_defaults_labels_and_endpoint(self, defaults):
        assert defaults["openbao_promtail_labels"] == {"service": "openbao", "vlan": "20"}
        # Loki push endpoint placeholder (non-secret), positions file for rotation
        assert defaults["openbao_loki_push_url"]
        assert defaults["openbao_promtail_positions_file"]
        # the audit glob covers rotated files (…audit*.log)
        assert "audit" in defaults["openbao_promtail_audit_log_glob"]
        assert defaults["openbao_promtail_audit_log_glob"].endswith("*.log")

    def test_promtail_template_ships_labels_and_positions(self):
        tmpl = _PROMTAIL_TMPL.read_text(encoding="utf-8")
        # renders the static labels loop, a positions file, a Loki client, and a
        # __path__ glob so rotation is followed without loss/dup (Req 7.4)
        assert "openbao_promtail_labels.items()" in tmpl
        assert "positions:" in tmpl
        assert "{{ openbao_loki_push_url }}" in tmpl
        assert "{{ openbao_promtail_audit_log_glob }}" in tmpl
        assert "{{ openbao_promtail_positions_file }}" in tmpl
        # one-JSON-object-per-line audit is parsed (json pipeline stage)
        assert "json:" in tmpl


class TestIdempotencyAndSecrecy:
    """Req 13 idempotency + secrets discipline (no defaulted secrets, no_log)."""

    def test_auth_and_audit_enables_are_check_then_enable(self, engine_tasks):
        # jwt + oidc enables gated on the auth map; audit enable gated on audit map
        # jwt + oidc method enables are check-then-enable (gated on the auth map).
        # The audit device is DECLARATIVE (config.hcl) on OpenBao 2.4, so there is
        # no audit-enable task to gate here (see TestFileAuditDevice).
        for needle, gate in (
            ("Enable the jwt auth method", "openbao_auth_map"),
            ("Enable the oidc auth method", "openbao_auth_map"),
        ):
            t = _task(engine_tasks, needle)
            assert gate in yaml.safe_dump(t["when"]), needle

    def test_no_secret_value_defaulted(self, defaults):
        assert "openbao_oidc_client_secret" not in defaults
        assert defaults["openbao_oidc_client_secret_env_var"]

    def test_every_credential_carrying_task_sets_no_log(self, engine_tasks):
        offenders = []
        for t in engine_tasks:
            body = yaml.safe_dump({k: v for k, v in t.items() if k != "no_log"})
            if any(ref in body for ref in _SECRET_REFS) and not _no_log_hidden(t):
                offenders.append(t.get("name"))
        assert not offenders, f"credential-carrying tasks missing no_log:true: {offenders}"

    def test_no_secret_literal_in_role_tree(self):
        pat = re.compile(
            r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
        )
        for path in _ROLE_DIR.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".md"}:
                assert not pat.search(path.read_text(encoding="utf-8")), (
                    f"real-looking secret literal in {path}"
                )
