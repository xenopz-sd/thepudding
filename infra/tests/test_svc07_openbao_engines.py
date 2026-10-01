"""Offline structural-contract tests for the task-7.1 secrets-engine enablement.

Task 7.1 (spec: svc-07-secrets-manager) — requirements.md Requirements 1.1, 1.4,
2.1, 2.2, 2.3, 3.1, 3.2, 3.3; design.md "Object shapes" table.

These are OFFLINE, dependency-light assertions over the ``openbao_init_unseal``
role's task-7.1 additions — ``tasks/engines_auth_audit.yml`` (the new include
file) and the engine vars added to ``defaults/main.yml`` — plus the wiring of
that include into ``tasks/primary_bootstrap.yml`` at the task-7.x marker. They
are NOT property-based tests and NOT ``requires_infra``: there is no live
OpenBao/Docker here. They pin the engine-level contract the task's brief and the
secrets-manager steering demand:

  * exactly one KV v2 engine at ``secret/`` with ``max_versions = 10``
    (Req 1.1, 1.4);
  * a ``database`` engine (``postgresql-database-plugin``) connecting as the
    dedicated ``openbao`` superuser, with 1h/24h role TTL defaults and a
    least-privilege ``creation_statements`` template (no superuser / CREATEROLE /
    cross-DB) (Req 2.1, 2.2, 2.3);
  * an ``aws`` engine against Garage's S3 endpoint with 1h/24h role TTLs and a
    per-``<slug>-<bucket>`` scoped policy template (Req 3.1, 3.2, 3.3);
  * every engine-enable is check-then-enable against the parsed ``bao secrets
    list -format=json`` mount map, so a re-run is a no-op (Req 13 idempotency);
  * NO secret value is defaulted anywhere — the PostgreSQL ``openbao`` superuser
    password and the Garage admin keys are resolved from never-committed env var
    NAMES, and every task carrying such a credential (or ``BAO_TOKEN``, the
    Root_Token) sets ``no_log: true``;
  * the include is wired INSIDE the root-token window, strictly BEFORE the
    Root_Token revoke (the task-7.x ordering contract).

The whole suite is offline and always runs (no gating).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_engines.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_BOOTSTRAP = _ROLE_DIR / "tasks" / "primary_bootstrap.yml"
_ENGINES = _ROLE_DIR / "tasks" / "engines_auth_audit.yml"

#: Runtime-secret-carrying references. Any task whose body references one of
#: these MUST set no_log: true (the exec env carries the Root_Token, or the
#: write carries a backend credential).
_SECRET_REFS = (
    "BAO_TOKEN=",
    "openbao_root_token",
    "openbao_database_superuser_password",
    "openbao_aws_access_key",
    "openbao_aws_secret_key",
    "openbao_mount_map",
    "openbao_primary_mounts",
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


def _argv_tokens(argv) -> list[str]:
    """Return the argv token list whether ``argv`` is a plain YAML list OR a
    Jinja list-concat expression string.

    The primary-side ``bao`` docker-exec tasks build their ``argv`` as a Jinja
    expression that conditionally splices ``-tls-skip-verify``, e.g.::

        argv: >-
          {{ ['docker','exec', ..., 'secrets','enable']
             + (['-tls-skip-verify'] if (openbao_primary_tls_skip_verify | bool) else [])
             + ['-path=' ~ openbao_kv_mount, '-version=2', 'kv'] }}

    so ``task["ansible.builtin.command"]["argv"]`` is a single *string*, not a
    Python list. This helper yields the ordered list of string-literal tokens
    inside that expression (``'docker'``, ``'exec'``, ``'secrets'``, ``'enable'``,
    ``'-path='``, ``'-version=2'``, ``'kv'``, ...) so token/substring assertions
    work regardless of the ``-tls-skip-verify`` splice. When ``argv`` is already
    a plain YAML list it is returned as-is (stringified).

    NOTE on concatenation: an item like ``-path={{ openbao_kv_mount }}`` in the
    OLD static shape becomes ``'-path=' ~ openbao_kv_mount`` in the expression —
    the literal PREFIX ``-path=`` is a token here and the var is separate. Tests
    that verify such items assert BOTH the literal prefix token is present AND
    the var name appears in the raw ``argv`` expression string (see
    ``_argv_raw``).
    """
    if isinstance(argv, list):
        return [str(a) for a in argv]
    return [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", argv)]


def _argv_raw(argv) -> str:
    """Return the raw ``argv`` as a single string for substring/var checks.

    For a plain YAML list this joins the items; for the Jinja expression string
    it returns the expression verbatim, so assertions can confirm a concatenated
    var name (e.g. ``openbao_kv_mount``) or the ``-tls-skip-verify`` splice
    (``openbao_primary_tls_skip_verify``) is present.
    """
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


def _names(tasks: list[dict]) -> list[str]:
    return [t.get("name", "") for t in tasks]


def _index_of(tasks: list[dict], needle: str) -> int:
    for i, t in enumerate(tasks):
        if needle in t.get("name", ""):
            return i
    raise AssertionError(f"no task whose name contains {needle!r}; names={_names(tasks)}")


class TestIncludeWiring:
    """The task-7.x ordering contract: included in-window, BEFORE the revoke."""

    def test_include_present_between_marker_and_revoke(self):
        text = _BOOTSTRAP.read_text(encoding="utf-8")
        marker = "EXTENSION POINT — Task 7.x"
        include = "engines_auth_audit.yml"
        revoke = "Revoke the Root_Token"
        assert marker in text, "task-7.x marker missing"
        assert include in text, "engines_auth_audit.yml include not wired into primary_bootstrap.yml"
        assert text.index(marker) < text.index(include) < text.index(revoke), (
            "the engines include must sit AFTER the task-7.x marker and BEFORE the "
            "Root_Token revoke (it needs the live Root_Token) — ordering contract violated"
        )

    def test_exactly_one_engines_include(self):
        tasks = yaml.safe_load(_BOOTSTRAP.read_text(encoding="utf-8"))
        includes = [
            t for t in tasks
            if t.get("ansible.builtin.include_tasks") == "engines_auth_audit.yml"
        ]
        assert len(includes) == 1, "expected exactly one engines_auth_audit.yml include directive"

    def test_section_markers_for_72_and_73_present(self):
        """7.2/7.3 slots exist so they extend this file cleanly, not rewrite it."""
        text = _ENGINES.read_text(encoding="utf-8")
        assert "[TASK 7.1]" in text
        assert "[TASK 7.2]" in text
        assert "[TASK 7.3]" in text


class TestKvEngine:
    """Req 1.1, 1.4 — exactly one KV v2 @ secret/ with max_versions = 10."""

    def test_kv_enabled_at_secret_version_2_if_absent(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Enable the KV v2 secrets engine")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        tokens = _argv_tokens(argv)
        assert "enable" in tokens and "-version=2" in tokens and "kv" in tokens
        # path is templated from openbao_kv_mount (which is "secret"): in the
        # concat expression the literal prefix `-path=` is a token and the var
        # is spliced in separately, so assert BOTH.
        assert "-path=" in tokens
        assert "openbao_kv_mount" in _argv_raw(argv)
        # the -tls-skip-verify conditional splice is present and gated
        _asserts_tls_skip_splice(argv)
        # enable-if-absent: gated on the mount map
        assert "openbao_mount_map" in yaml.safe_dump(engine_tasks[idx]["when"])
        assert defaults["openbao_kv_mount"] == "secret"

    def test_kv_max_versions_tuned_to_10(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Tune KV v2 max_versions")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        # `max_versions=` is a concatenation literal prefix; the value is spliced
        # from openbao_kv_max_versions in the expression.
        assert any("max_versions=" in a for a in _argv_tokens(argv))
        assert "openbao_kv_max_versions" in _argv_raw(argv)
        _asserts_tls_skip_splice(argv)
        assert defaults["openbao_kv_max_versions"] == 10


class TestDatabaseEngine:
    """Req 2.1, 2.2, 2.3 — postgres plugin, openbao superuser, TTLs, least-priv."""

    def test_database_engine_enabled_if_absent(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Enable the database secrets engine")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        tokens = _argv_tokens(argv)
        assert "enable" in tokens and "database" in tokens
        # `-path=` literal prefix + the mount var spliced separately
        assert "-path=" in tokens
        assert "openbao_database_mount_path" in _argv_raw(argv)
        _asserts_tls_skip_splice(argv)
        assert "openbao_mount_map" in yaml.safe_dump(engine_tasks[idx]["when"])
        assert defaults["openbao_database_mount_path"] == "database"

    def test_connection_uses_postgres_plugin_and_openbao_superuser(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Configure database/config/")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        # plugin_name is a concat literal prefix; the var (postgres plugin) is
        # spliced in — assert BOTH the prefix token and the var name.
        assert "plugin_name=" in tokens
        assert "openbao_database_plugin_name" in raw
        assert defaults["openbao_database_plugin_name"] == "postgresql-database-plugin"
        # connects as the dedicated `openbao` superuser account (Req 2.1)
        assert "username=" in tokens
        assert "openbao_database_superuser_name" in raw
        assert defaults["openbao_database_superuser_name"] == "openbao"
        # password comes from a var (resolved from env), never a literal
        assert "password=" in tokens
        assert "openbao_database_superuser_password" in raw
        _asserts_tls_skip_splice(argv)

    def test_database_role_ttls_are_1h_24h(self, defaults):
        assert defaults["openbao_database_default_ttl"] == "1h"
        assert defaults["openbao_database_max_ttl"] == "24h"

    def test_creation_statements_are_least_privilege(self, defaults):
        stmts = " ".join(defaults["openbao_database_creation_statements_rw"]).upper()
        # explicit least-privilege attributes — no superuser / createrole / createdb
        assert "NOSUPERUSER" in stmts
        assert "NOCREATEROLE" in stmts
        assert "NOCREATEDB" in stmts
        # must NOT grant superuser/createrole positively
        assert re.search(r"\bWITH\b.*\bSUPERUSER\b", stmts) is None
        assert " CREATEROLE" not in stmts.replace("NOCREATEROLE", "")


class TestAwsEngine:
    """Req 3.1, 3.2, 3.3 — Garage S3 endpoint, TTLs, per-bucket-scoped policy."""

    def test_aws_engine_enabled_if_absent(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Enable the aws secrets engine")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        tokens = _argv_tokens(argv)
        assert "enable" in tokens and "aws" in tokens
        assert "-path=" in tokens
        assert "openbao_aws_mount_path" in _argv_raw(argv)
        _asserts_tls_skip_splice(argv)
        assert "openbao_mount_map" in yaml.safe_dump(engine_tasks[idx]["when"])
        assert defaults["openbao_aws_mount_path"] == "aws"

    def test_aws_engine_is_opt_in_and_off_by_default(self, engine_tasks, defaults):
        """The aws engine enable is gated on openbao_enable_aws (default false).

        OpenBao 2.4 ships NO built-in `aws` plugin, so enabling it on a stock
        build fails `plugin not found in the catalog: aws` (28th correction). The
        enable MUST be gated on `openbao_enable_aws`, and that flag MUST default
        false so a base bring-up on stock OpenBao never touches aws.
        """
        assert defaults.get("openbao_enable_aws") is False, (
            "openbao_enable_aws must default to false — OpenBao 2.4 has no built-in "
            "aws plugin, so the engine is opt-in only after registering it"
        )
        idx = _index_of(engine_tasks, "Enable the aws secrets engine")
        when = yaml.safe_dump(engine_tasks[idx]["when"])
        assert "openbao_enable_aws" in when, (
            "the aws-engine enable must be gated on openbao_enable_aws so a stock "
            "OpenBao build (no aws plugin) does not fail the bootstrap"
        )

    def test_aws_root_config_targets_garage_endpoint(self, engine_tasks, defaults):
        idx = _index_of(engine_tasks, "Configure aws/config/root")
        argv = engine_tasks[idx]["ansible.builtin.command"]["argv"]
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        # the aws/config/root path is built as `openbao_aws_mount_path ~ '/config/root'`:
        # the literal suffix `/config/root` is a token and the mount var is spliced.
        assert "/config/root" in tokens
        assert "openbao_aws_mount_path" in raw
        # endpoint/access_key/secret_key are concat literal prefixes + vars.
        assert "endpoint=" in tokens
        assert "openbao_aws_endpoint" in raw
        assert "access_key=" in tokens
        assert "openbao_aws_access_key" in raw
        assert "secret_key=" in tokens
        assert "openbao_aws_secret_key" in raw
        _asserts_tls_skip_splice(argv)

    def test_aws_role_ttls_are_1h_24h(self, defaults):
        assert defaults["openbao_aws_default_ttl"] == "1h"
        assert defaults["openbao_aws_max_ttl"] == "24h"

    def test_aws_policy_template_scoped_to_slug_bucket(self, defaults):
        doc = defaults["openbao_aws_policy_document_rw_template"]
        stmt = doc["Statement"][0]
        assert stmt["Effect"] == "Allow"
        assert set(stmt["Action"]) >= {
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"
        }
        # scoped to the <slug>-<bucket> resource ONLY (Req 3.2)
        for resource in stmt["Resource"]:
            assert "<slug>-<bucket>" in resource


class TestIdempotencyAndSecrecy:
    """Req 13 idempotency + secrets discipline (no defaulted secrets, no_log)."""

    def test_all_three_engine_enables_are_check_then_enable(self, engine_tasks):
        # Scope to §7.1's three SECRETS-engine enables (KV v2 / database / aws),
        # which gate on the `openbao_mount_map` (`bao secrets list`) read. §7.2's
        # auth/audit enables also end in "(if absent)" but gate on a DIFFERENT
        # map (`bao auth list` / `bao audit list`), so they are excluded here by
        # matching on the mount-map gate rather than the name suffix alone.
        enables = [
            t
            for t in engine_tasks
            if t.get("name", "").endswith("(if absent)")
            and "openbao_mount_map" in yaml.safe_dump(t.get("when", ""))
        ]
        secrets_engine_enables = [
            t
            for t in enables
            if any(
                s in t.get("name", "")
                for s in ("KV v2 secrets engine", "database secrets engine", "aws secrets engine")
            )
        ]
        assert len(secrets_engine_enables) == 3, _names(engine_tasks)
        for t in secrets_engine_enables:
            assert "openbao_mount_map" in yaml.safe_dump(t["when"])

    def test_no_secret_value_defaulted(self, defaults):
        for k in (
            "openbao_database_superuser_password",
            "openbao_aws_access_key",
            "openbao_aws_secret_key",
        ):
            assert k not in defaults, f"{k} must not be defaulted to a value"
        # only env var NAMES are defaulted
        assert defaults["openbao_database_superuser_password_env_var"]
        assert defaults["openbao_aws_access_key_env_var"]
        assert defaults["openbao_aws_secret_key_env_var"]

    def test_every_credential_carrying_task_sets_no_log(self, engine_tasks):
        offenders = []
        for t in engine_tasks:
            body = yaml.safe_dump({k: v for k, v in t.items() if k != "no_log"})
            if any(ref in body for ref in _SECRET_REFS) and not _no_log_hidden(t):
                offenders.append(t.get("name"))
        assert not offenders, f"credential-carrying tasks missing no_log:true: {offenders}"

    def test_no_secret_literal_in_role_tree(self):
        pat = re.compile(r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})")
        for path in _ROLE_DIR.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".md"}:
                assert not pat.search(path.read_text(encoding="utf-8")), (
                    f"real-looking secret literal in {path}"
                )
