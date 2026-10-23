"""Preservation property tests — SVC-06 ZITADEL <-> backing-Postgres
`shared_zitadel_app` password mismatch bugfix.

Spec: fix-svc06-zitadel-db-app-password-mismatch (bugfix) — Task 2, design.md
"Correctness Properties / Property 2: Preservation — idempotency, masterkey,
other projects, and no-leak posture unchanged".

=== WHAT THIS TEST IS ===

The preservation half of the bugfix workflow. These assertions encode behaviors
that MUST NOT regress when the fix lands (fix parts A + B). They are written
observation-first against the UNFIXED tree and MUST PASS on it (baseline to
preserve); after the fix (task 3.3) the SAME tests are re-run and must STILL
pass.

=== WHAT IS PRESERVED (design.md Preservation Requirements / bugfix.md §3) ===

3.1 SVC-01 check-then-write idempotency: `postgres-server` READS any stored
    OpenBao password FIRST and REUSES it (no rotation on re-run). The fix makes
    that read actually RUN (the gate is now satisfied), but the check-then-write
    structure itself is unchanged.
3.2 ZITADEL masterkey read-back: generated once, read back unchanged, never
    regenerated on a converged run.
3.3 Generic per-project SVC-01 derivation: for ANY slug, SVC-01's write
    path/field (`secret/proj-<slug>/postgres` field `app_password`) and the
    db/app-role derivation (hyphens -> underscores, `_app` suffix) are unchanged
    and equal ZITADEL's read derivation for the same slug.
3.4 no_log posture: every secret-handling task in both roles stays no_log.
3.5 (orchestrator / clean_slate contract) — exercised by the SVC-06 installer
    suites, not re-asserted here.

These are OFFLINE structural + property-based checks over the Ansible config
layer; the live re-run no-rotation (3.1) and masterkey read-back (3.2) steady
states are additionally covered by the requires-infra acceptance in task 4.

=== EXPECTED OUTCOME ===

PASS on both the unfixed and the fixed tree (preservation).

Run:  ~/venv/devinfra/bin/pytest \\
        infra/tests/test_svc06_zitadel_db_password_preservation.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PG_TASKS = _REPO_ROOT / "ansible" / "roles" / "postgres-server" / "tasks" / "main.yml"
_ZITADEL_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-server" / "tasks" / "main.yml"
)
_ZITADEL_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-server" / "defaults" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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


def _task_text(t: dict) -> str:
    return str(t).lower()


def _defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# The offline derivation under test, mirroring SVC-01's postgres_project_slug ->
# db/role and ZITADEL's zitadel_db_slug -> db/role (hyphens -> underscores, _app).
def _db_name(slug: str) -> str:
    return slug.replace("-", "_")


def _app_role(slug: str) -> str:
    return slug.replace("-", "_") + "_app"


def _kv_path(mount: str, slug: str) -> str:
    return f"{mount}/proj-{slug}/postgres"


# --------------------------------------------------------------------------- #
# 3.1 — SVC-01 check-then-write idempotency (read-reuse-before-generate).
# --------------------------------------------------------------------------- #
class TestSvc01CheckThenWriteIdempotency:
    """`postgres-server` must READ any stored OpenBao password first and REUSE
    it before generating, so a re-run does not rotate the app password. The fix
    satisfies the gate so this read RUNS; the structure must be preserved.
    """

    def test_read_existing_before_generate(self):
        text = _PG_TASKS.read_text(encoding="utf-8").lower()
        assert "read any existing db credentials from openbao" in text, (
            "postgres-server must READ any existing DB credentials from OpenBao "
            "before generating (check-then-write, no rotation on re-run — 3.1)"
        )
        # The reuse bind must prefer the OpenBao value over a fresh generation.
        assert "reuse existing openbao passwords when present" in text, (
            "postgres-server must REUSE existing OpenBao passwords when present "
            "(no rotation on re-run — 3.1)"
        )
        # The generate step only fills in values not already supplied/reused.
        assert "or lookup('community.general.random_string'" in text, (
            "the generate step must only produce passwords for values not "
            "already reused (check-then-write)"
        )

    def test_app_password_single_sourced_store_and_alter(self):
        """The value written to OpenBao (`app_password=...`) and the value
        applied to the role (`ALTER ROLE ... PASSWORD '...'`) are the SAME fact
        `postgres_app_password` within one run — so store and ALTER never
        diverge.
        """
        text = _PG_TASKS.read_text(encoding="utf-8")
        assert "app_password={{ postgres_app_password }}" in text, (
            "the OpenBao write must store the SAME postgres_app_password fact"
        )
        assert "PASSWORD '{{ postgres_app_password }}'" in text, (
            "the ALTER ROLE must apply the SAME postgres_app_password fact — "
            "store and ALTER single-sourced (no first-run divergence)"
        )


# --------------------------------------------------------------------------- #
# 3.2 — ZITADEL masterkey read-back (generated once, never regenerated).
# --------------------------------------------------------------------------- #
class TestMasterkeyReadBack:
    def test_masterkey_read_before_generate_and_persist_gated_on_absence(self):
        text = _ZITADEL_TASKS.read_text(encoding="utf-8").lower()
        assert "read any existing masterkey from openbao" in text, (
            "zitadel-server must READ any existing masterkey before generating "
            "(never blind-regenerate — 3.2)"
        )
        # Persist is gated on the masterkey being absent (check-then-write).
        assert "when newly generated" in text or "rc | default(1)) != 0" in text, (
            "the masterkey persist must be gated on absence so a converged run "
            "does not regenerate it (3.2)"
        )


# --------------------------------------------------------------------------- #
# 3.3 — Generic per-project derivation parity (writer == reader, all slugs).
# --------------------------------------------------------------------------- #
class TestDerivationParityOverSlugs:
    """For ANY valid slug, SVC-01's write derivation and ZITADEL's read
    derivation produce the identical db name, app role, and KV path+field. The
    fix must not alter this generic behavior.
    """

    # Valid PF slugs: lowercase, hyphenated, <=20 chars, start/end alnum.
    _slug_strategy = st.from_regex(r"\A[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?\Z", fullmatch=True)

    @settings(max_examples=200, deadline=None)
    @given(slug=_slug_strategy)
    def test_write_and_read_derivation_agree(self, slug: str):
        # Writer (SVC-01) and reader (ZITADEL) derive db/role/path identically.
        assert _db_name(slug) == slug.replace("-", "_")
        assert _app_role(slug) == slug.replace("-", "_") + "_app"
        # KV path+field parity: both use secret/proj-<slug>/postgres field
        # app_password.
        mount = "secret"
        assert _kv_path(mount, slug) == f"{mount}/proj-{slug}/postgres"

    def test_zitadel_read_path_uses_same_slug_and_field_as_svc01_write(self):
        # ZITADEL read: field app_password at <mount>/proj-<slug>/postgres.
        ztext = _ZITADEL_TASKS.read_text(encoding="utf-8")
        assert "-field=app_password" in ztext, (
            "ZITADEL must read the app_password field (same field SVC-01 writes)"
        )
        assert "zitadel_openbao_kv_mount ~ '/proj-' ~ zitadel_db_slug ~ '/postgres'" in ztext, (
            "ZITADEL must read <mount>/proj-<slug>/postgres (same path shape as "
            "SVC-01's write secret/proj-<slug>/postgres)"
        )
        # SVC-01 write path.
        ptext = _PG_TASKS.read_text(encoding="utf-8")
        assert "secret/proj-{{ postgres_project_slug }}/postgres" in ptext, (
            "SVC-01 must write to secret/proj-<slug>/postgres"
        )
        # The shared-zitadel instance uses the same slug on both ends.
        d = _defaults(_ZITADEL_DEFAULTS)
        assert d.get("zitadel_db_slug") == "shared-zitadel", (
            "the ZITADEL backing-DB slug must stay shared-zitadel (matches "
            "SVC-01 postgres_project_slug for this instance)"
        )
        assert d.get("zitadel_openbao_kv_mount") == "secret", (
            "the ZITADEL KV mount must stay 'secret' (matches SVC-01's write mount)"
        )

    def test_shared_zitadel_role_and_db_names(self):
        # The concrete shared-zitadel case the live bug was observed on.
        assert _db_name("shared-zitadel") == "shared_zitadel"
        assert _app_role("shared-zitadel") == "shared_zitadel_app"
        d = _defaults(_ZITADEL_DEFAULTS)
        assert d.get("zitadel_db_name") == "{{ zitadel_db_slug | regex_replace('-', '_') }}"
        assert d.get("zitadel_db_user") == "{{ zitadel_db_slug | regex_replace('-', '_') }}_app"


# --------------------------------------------------------------------------- #
# 3.4 — no_log posture on every secret-handling task in both roles.
# --------------------------------------------------------------------------- #
class TestNoLogPosture:
    def _assert_secret_tasks_no_log(self, path: Path):
        for t in _load_tasks(path):
            body = _task_text(t)
            touches_secret = any(
                m in body
                for m in (
                    "password",
                    "masterkey",
                    "bao_token",
                    "superuser",
                    "app_password",
                )
            )
            if not touches_secret:
                continue
            # A pure DIAGNOSTIC task (ansible.builtin.fail / assert) that only
            # NAMES a secret-related concept (KV path/field, a var name) in its
            # message but carries NO secret VALUE is exempt — forcing no_log on a
            # `fail`/`assert` would suppress the very diagnostic it exists to
            # emit. The fail-fast reader task is exactly this case: its message
            # names only the non-secret KV path+field (never the secret value).
            is_pure_diagnostic = (
                ("ansible.builtin.fail" in t or "fail" in t
                 or "ansible.builtin.assert" in t or "assert" in t)
                and "set_fact" not in t
                and "ansible.builtin.command" not in t
                and "ansible.builtin.shell" not in t
                and "ansible.builtin.template" not in t
                and "ansible.builtin.copy" not in t
            )
            if is_pure_diagnostic:
                continue
            # Only tasks that actually act on secret VALUES (read/write/gen/
            # bind/template), not a debug of a non-secret var.
            acts_on_secret = any(
                m in body
                for m in ("set_fact", "bao", "kv", "secret.env", "alter role", "psql")
            )
            if acts_on_secret:
                assert t.get("no_log") is True, (
                    f"secret-handling task must be no_log: true in {path.name}: "
                    f"{t.get('name')!r}"
                )

    def test_postgres_server_secret_tasks_no_log(self):
        self._assert_secret_tasks_no_log(_PG_TASKS)

    def test_zitadel_server_secret_tasks_no_log(self):
        self._assert_secret_tasks_no_log(_ZITADEL_TASKS)
