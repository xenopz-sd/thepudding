"""Offline structural-contract tests for the task-7.3 rotate-root wiring.

Task 7.3 (spec: svc-07-secrets-manager) — requirements.md Requirement 8
(8.1–8.6); design.md "Error Handling (rotate-root rows)".

These are OFFLINE, dependency-light assertions over the ``openbao_init_unseal``
role's task-7.3 additions to ``tasks/engines_auth_audit.yml`` (the §7.3 section)
and the ``templates/platform-admin.hcl.j2`` break-glass policy the §7.3 section
reads back and asserts against. They are NOT property-based tests and NOT
``requires_infra``: there is no live OpenBao/Docker here.

The NATURE of task 7.3 (see the task brief and the §7.3 header comment) is
AUTHORIZATION WIRING, not re-implementing OpenBao behaviour:

  * Req 8.1–8.4 (rotate leaves pre-rotation leases valid; the next issuance uses
    the new root; a failed rotation retains the previous credential and audits
    the failure) are OpenBao-NATIVE to the ``database/rotate-root`` /
    ``aws/config/rotate-root`` endpoints — this task adds no config overriding
    them, and these tests only assert that non-override is DOCUMENTED, since
    proving the runtime behaviour needs the live backend (tasks 7.4 / 12.3).
  * Req 8.6 (a ``platform-admin``-scoped token can rotate roots WITHOUT the
    Root_Token) is the one thing this task actively WIRES: the platform-admin
    policy must grant both rotate-root endpoints with the ``update`` capability,
    and the §7.3 section reads the live policy back and fail-closed asserts it.
    These tests pin BOTH the policy-template grant and the read-back assertion.
  * Req 8.5 (deny a non-platform-admin caller) is the policy-engine DEFAULT
    (no matching grant -> permission-denied). These tests assert NO explicit
    ``deny`` rule was added (a hard deny would break Req 8.5's "or a superset
    thereof") and that the default-deny reliance is documented.

The suite also pins that §7.3 performs NO rotation at bootstrap (rotating a live
credential is a day-2 operational action), stays read-only/idempotent (Req 13),
and keeps the secrets discipline (root-token-carrying tasks set ``no_log``; no
secret literal anywhere in the role tree).

The whole suite is offline and always runs (no gating).

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_rotate_root.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_init_unseal"
_ENGINES = _ROLE_DIR / "tasks" / "engines_auth_audit.yml"
_PLATFORM_ADMIN_TMPL = _ROLE_DIR / "templates" / "platform-admin.hcl.j2"

#: Runtime-secret-carrying references specific to the §7.3 additions. Any §7.3
#: task whose body references one of these MUST set ``no_log: true`` — the exec
#: env carries the Root_Token, or the register/derived fact comes from a
#: root-token exec stdout. (The rendered policy body is an ACL document with no
#: secret, but it is derived from a root-token exec, so it stays no_log too.)
_SECRET_REFS = (
    "BAO_TOKEN=",
    "openbao_root_token",
    "openbao_platform_admin_policy_raw",
    "openbao_platform_admin_policy_body",
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
def section_73_tasks(engine_tasks) -> list[dict]:
    """Only the §7.3 tasks (names beginning with the ``7.3`` section prefix)."""
    return [t for t in engine_tasks if (t.get("name") or "").startswith("7.3")]


@pytest.fixture(scope="module")
def platform_admin_tmpl() -> str:
    # Render the one Jinja substitution the policy uses so the parse/regex see a
    # realistic ACL document (no Ansible needed for this single-var substitution).
    return _PLATFORM_ADMIN_TMPL.read_text(encoding="utf-8").replace(
        "{{ openbao_kv_mount }}", "secret"
    )


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
          {{ ['docker','exec', ..., 'policy', 'read']
             + (['-tls-skip-verify'] if (openbao_primary_tls_skip_verify | bool) else [])
             + ['-format=json', openbao_platform_admin_policy_name] }}

    so ``task["ansible.builtin.command"]["argv"]`` is a single *string*, not a
    Python list. This helper yields the ordered list of string-literal tokens
    inside that expression (``'docker'``, ``'exec'``, ``'policy'``, ``'read'``,
    ``'-format=json'``, ...) so token/substring assertions work regardless of the
    ``-tls-skip-verify`` splice. When ``argv`` is already a plain YAML list it is
    returned as-is (stringified).

    NOTE: a BARE var argument (e.g. the final positional
    ``openbao_platform_admin_policy_name`` — not quoted) is NOT a string literal,
    so it will not appear in the token list. Tests assert such bare vars via the
    raw expression (see ``_argv_raw``); the ``BAO_TOKEN={{ openbao_root_token }}``
    exec-env token, however, is the single-quoted literal
    ``'BAO_TOKEN=' ~ openbao_root_token`` so its ``BAO_TOKEN=`` prefix IS a token
    and the var name IS in the raw expression.
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


class TestSectionMarkerFilled:
    """The §7.3 marker is now filled (the placeholder is gone)."""

    def test_placeholder_removed(self):
        text = _ENGINES.read_text(encoding="utf-8")
        # task 7.3's insertion placeholder must be gone once 7.3 is implemented.
        assert "(task 7.3: rotate-root wiring goes HERE)" not in text

    def test_section_header_present(self):
        text = _ENGINES.read_text(encoding="utf-8")
        assert "[TASK 7.3]" in text
        assert "§7.3 — ROOT-CREDENTIAL ROTATION WIRING" in text

    def test_73_runs_after_72(self):
        text = _ENGINES.read_text(encoding="utf-8")
        marker_73 = text.index("§7.3 — ROOT-CREDENTIAL ROTATION WIRING")
        assert text.index("§7.2 — AUTH METHODS") < marker_73
        # the §7.3 tasks live below the marker
        assert text.index("Read the live platform-admin policy back") > marker_73


class TestPlatformAdminPolicyGrantsRotateRoot:
    """Req 8.6 — the platform-admin policy authorises both rotate-root paths."""

    def test_database_wildcard_grants_update(self, platform_admin_tmpl):
        # database/* must grant update (covers database/rotate-root/<conn>)
        m = re.search(
            r'path\s+"database/\*"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]',
            platform_admin_tmpl,
        )
        assert m, "no database/* path block in platform-admin policy"
        assert "update" in m.group(1)

    def test_aws_wildcard_grants_update(self, platform_admin_tmpl):
        m = re.search(
            r'path\s+"aws/\*"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]',
            platform_admin_tmpl,
        )
        assert m, "no aws/* path block in platform-admin policy"
        assert "update" in m.group(1)

    def test_explicit_database_rotate_root_block_with_update(self, platform_admin_tmpl):
        # explicit least-surprise block spelled out (Req 8.1, 8.6)
        m = re.search(
            r'path\s+"database/rotate-root/\*"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]',
            platform_admin_tmpl,
        )
        assert m, "no explicit database/rotate-root/* block in platform-admin policy"
        assert "update" in m.group(1)

    def test_explicit_aws_config_rotate_root_block_with_update(self, platform_admin_tmpl):
        m = re.search(
            r'path\s+"aws/config/rotate-root"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]',
            platform_admin_tmpl,
        )
        assert m, "no explicit aws/config/rotate-root block in platform-admin policy"
        assert "update" in m.group(1)

    def test_no_explicit_deny_rule_for_rotate_root(self, platform_admin_tmpl):
        """Req 8.5 is the policy default-deny; a hard `deny` would break superset.

        A ``"deny"`` capability on a rotate-root path would prevent a legitimate
        superset policy from ever authorising it, contradicting Req 8.5's
        "the platform-admin policy (or a superset thereof)". Denial of a
        non-platform-admin caller must come from the ABSENCE of a grant, not an
        explicit deny.
        """
        # find every rotate-root path block and confirm none carries a deny cap
        for block in re.finditer(
            r'path\s+"(?:database/rotate-root/\*|aws/config/rotate-root)"\s*\{([^}]*)\}',
            platform_admin_tmpl,
        ):
            assert "deny" not in block.group(1)


class TestSection73ReadsBackAndAsserts:
    """§7.3 reads the live policy and fail-closed asserts the rotate-root grant."""

    def test_reads_live_platform_admin_policy(self, section_73_tasks):
        t = _task(section_73_tasks, "Read the live platform-admin policy back")
        argv = _argv(t)
        tokens = _argv_tokens(argv)
        raw = _argv_raw(argv)
        assert "policy" in tokens and "read" in tokens
        assert "-format=json" in tokens
        # the policy name is a BARE (unquoted) positional var in the concat
        # expression, so assert it via the raw expression string.
        assert "openbao_platform_admin_policy_name" in raw
        # authenticates with the Root_Token in the exec env: `'BAO_TOKEN=' ~
        # openbao_root_token` — literal prefix token + the var spliced in.
        assert "BAO_TOKEN=" in tokens
        assert "openbao_root_token" in raw
        # the -tls-skip-verify conditional splice is present and gated
        _asserts_tls_skip_splice(argv)
        # a read never reports a change (idempotent, Req 13)
        assert t.get("changed_when") is False

    def test_extracts_policy_body(self, section_73_tasks):
        t = _task(section_73_tasks, "Extract the platform-admin policy body")
        body = yaml.safe_dump(t)
        assert "openbao_platform_admin_policy_raw" in body
        assert "from_json" in body
        assert t.get("changed_when") is False

    def test_asserts_both_rotate_root_paths_granted(self, section_73_tasks):
        t = _task(section_73_tasks, "Assert platform-admin grants database/rotate-root")
        that = t["ansible.builtin.assert"]["that"]
        joined = " ".join(that)
        # asserts against the LIVE policy body, tolerating the covering wildcard
        assert "openbao_platform_admin_policy_body" in joined
        assert "database/(rotate-root" in joined or "database/" in joined
        assert "aws/(config/rotate-root" in joined or "aws/" in joined
        # fail-closed: names Req 8.6 in the failure message
        assert "8.6" in yaml.safe_dump(t["ansible.builtin.assert"])

    def test_assert_regex_matches_the_actual_policy(self, section_73_tasks, platform_admin_tmpl):
        """The §7.3 assertion's regexes must actually match the rendered policy.

        This guards against the two implementations (the policy template and the
        read-back assertion) silently drifting apart — a green assertion that can
        never match the real policy would be worthless.
        """
        t = _task(section_73_tasks, "Assert platform-admin grants database/rotate-root")
        that = t["ansible.builtin.assert"]["that"]
        # pull the `is search('<regex>')` argument out of each clause
        patterns = re.findall(r"search\('([^']+)'\)", " ".join(that))
        assert len(patterns) >= 2, f"expected >=2 search() regexes, got {patterns}"
        for pat in patterns:
            assert re.search(pat, platform_admin_tmpl), (
                f"§7.3 assertion regex {pat!r} does not match the rendered "
                f"platform-admin policy — template/assertion drift"
            )


class TestSection73IsReadOnlyAndDocumentsNativeBehaviour:
    """§7.3 performs NO rotation/writes at bootstrap; native behaviour documented."""

    def test_no_rotate_write_performed_at_bootstrap(self, section_73_tasks):
        """No §7.3 command actually invokes a rotate-root (day-2 op, not bootstrap)."""
        for t in section_73_tasks:
            cmd = t.get("ansible.builtin.command")
            if not cmd:
                continue
            # `_argv_raw` handles both the plain-list and Jinja-expression-string
            # argv shapes; a naive `" ".join(str)` on a string would spread it
            # char-by-char and break the `rotate-root` substring detection.
            joined = _argv_raw(cmd.get("argv", []))
            assert "rotate-root" not in joined, (
                f"§7.3 must not perform a rotation at bootstrap: {t.get('name')}"
            )

    def test_section_73_commands_are_read_only(self, section_73_tasks):
        """Every §7.3 command is a read (policy read); none is a write/rotate."""
        write_verbs = {"write", "put", "enable", "disable", "delete", "revoke"}
        for t in section_73_tasks:
            cmd = t.get("ansible.builtin.command")
            if not cmd:
                continue
            # `_argv_tokens` yields the real argv tokens for both shapes; a
            # `set(str)` on the Jinja-expression string would be a set of single
            # chars and never match a multi-char write verb.
            tokens = set(_argv_tokens(cmd.get("argv", [])))
            assert not (write_verbs & tokens), (
                f"§7.3 command performs a write verb (must be read-only): "
                f"{t.get('name')} {tokens}"
            )

    def test_native_behaviours_documented_not_overridden(self):
        """Req 8.1–8.4 native behaviours are documented as preserved, not re-done."""
        text = _ENGINES.read_text(encoding="utf-8")
        section = text[text.index("§7.3 — ROOT-CREDENTIAL ROTATION WIRING"):]
        low = section.lower()
        # lease-preservation / next-issuance / retain-on-failure / audit noted native
        assert "native" in low
        assert "lease" in low
        assert "8.5" in section and "8.6" in section
        # explicitly states no rotation is performed at bootstrap
        assert "does not perform a rotation" in low or "no rotation is performed" in low
        # points at the live tests that actually exercise rotate-root
        assert "7.4" in section or "12.3" in section


class TestSecrecyAndIdempotency:
    """Secrets discipline + idempotency for the §7.3 additions."""

    def test_root_token_carrying_tasks_set_no_log(self, section_73_tasks):
        offenders = []
        for t in section_73_tasks:
            body = yaml.safe_dump({k: v for k, v in t.items() if k != "no_log"})
            if any(ref in body for ref in _SECRET_REFS) and not _no_log_hidden(t):
                offenders.append(t.get("name"))
        assert not offenders, f"§7.3 root-token-carrying tasks missing no_log:true: {offenders}"

    def test_section_73_reports_no_change(self, section_73_tasks):
        """Read-only + assert + debug => every §7.3 task is changed_when False or a no-change type."""
        for t in section_73_tasks:
            # command/set_fact tasks pin changed_when: false; assert/debug never change
            if "ansible.builtin.command" in t or "ansible.builtin.set_fact" in t:
                assert t.get("changed_when") is False, t.get("name")

    def test_no_secret_literal_in_role_tree(self):
        pat = re.compile(
            r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
        )
        for path in _ROLE_DIR.rglob("*"):
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".md"}:
                assert not pat.search(path.read_text(encoding="utf-8")), (
                    f"real-looking secret literal in {path}"
                )
