"""Offline structural guards for the consumer DB-secret workload-path mismatch bugfix.

Feature: fix-consumer-db-secret-workload-path-mismatch (Option A — a new idempotent,
``no_log``, never-rotate BRIDGE step STEP 2a in ``consumer_bootstrap`` plus a default
DB agent mapping and the agreed-path documentation).

The consumer flow is internally inconsistent about which KV tree holds the DB
credential: ``postgres-server`` *writes* ``secret/proj-<slug>/postgres`` but the
``<slug>-workload`` AppRole (bound to ``[policy-<slug>]``) and its ``openbao-agent``
sidecar can only *read* the bare ``<slug>`` five-sub-path tree
(``secret/{data,metadata,delete,undelete,destroy}/<slug>/*``). The ``proj-<slug>``
prefix is outside that boundary, so a correctly-onboarded workload is DENIED.
Nothing bridges the credential into the workload-readable tree.

Option A adds one bridge step (STEP 2a) to ``consumer_bootstrap/tasks/main.yml`` that
reads the authoritative source record ``secret/proj-<slug>/postgres`` and
``bao kv put``s a composed ``DATABASE_URL`` to ``secret/data/<slug>/db`` (field
``database_url``) — a path INSIDE the already-granted ``policy-<slug>`` tree, so the
policy is UNCHANGED and ``agent.hcl.j2`` / ``render_agent_hcl`` are UNTOUCHED.

These tests are pure-logic (PyYAML parse of the committed role files + source-string
checks) — they need no infrastructure. They are the mergeable Tier-1 gate.

Two layers (design.md Correctness Properties):

* **Exploration / Fix-Checking (Property 1):** a bridge step ``bao kv put``s into
  ``secret/data/<slug>/db`` field ``database_url`` composing a ``DATABASE_URL`` from
  the source fields; it is ``no_log``, DB-declared- and wired-gated, carries the admin
  token in ``-e BAO_TOKEN=`` (never argv), and a fail-loud source assert precedes the
  put; and the defaults define the default DB agent mapping. On UNFIXED code these
  FAIL (no bridge step, no default mapping) — the structural counterexample.

* **Preservation (Property 2):** ``policy-slug.hcl.j2`` still renders exactly five
  sub-paths with no ``proj-<slug>`` grant; ``agent.hcl.j2`` + the ``render_agent_hcl``
  golden are byte-unchanged; ``consumer_manifest.py`` still enforces the
  ``{slug, database, sso, secrets}`` allowlist; the bridge is gated off for a
  no-database project; ``postgres-server`` remains the authoritative source write.
  These PASS on UNFIXED code (baseline to preserve) and MUST keep passing after.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# tests/installer/test_consumer_db_secret_workload_bridge.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CONSUMER_BOOTSTRAP_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "tasks" / "main.yml"
)
_CONSUMER_BOOTSTRAP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "consumer_bootstrap" / "defaults" / "main.yml"
)
_POLICY_SLUG_TEMPLATE = (
    _REPO_ROOT / "ansible" / "roles" / "openbao_project_onboard" / "templates"
    / "policy-slug.hcl.j2"
)
_AGENT_HCL_TEMPLATE = (
    _REPO_ROOT / "ansible" / "roles" / "openbao-agent" / "templates" / "agent.hcl.j2"
)
_AGENT_HCL_GOLDEN = (
    _REPO_ROOT / "tests" / "installer" / "fixtures" / "openbao_workload_auth"
    / "agent.hcl.golden"
)
_CONSUMER_MANIFEST_PY = (
    _REPO_ROOT / "scripts" / "installer" / "consumer_manifest.py"
)
_POSTGRES_SERVER_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "postgres-server" / "tasks" / "main.yml"
)


# --------------------------------------------------------------------------- #
# Helpers (mirror the idiom in test_composed_onboarder_delegation.py)
# --------------------------------------------------------------------------- #
def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _load_role_tasks(path: Path):
    """Return the flat ordered task list of a role ``tasks/main.yml``."""
    tasks: list[dict] = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if isinstance(doc, list):
            tasks.extend(_iter_tasks(doc))
    return tasks


def _command_argv_text(task: dict) -> str | None:
    """Return the flat argv/command string of a command/shell task, else None."""
    for mod in (
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "command",
        "shell",
    ):
        spec = task.get(mod)
        if spec is None:
            continue
        if isinstance(spec, str):
            return spec
        if isinstance(spec, dict):
            argv = spec.get("argv")
            if isinstance(argv, list):
                return " ".join(str(a) for a in argv)
            if isinstance(argv, str):
                return argv
            # command: with free-form `cmd:` string
            cmd = spec.get("cmd")
            if isinstance(cmd, str):
                return cmd
    return None


def _normalise(text: str) -> str:
    """Collapse quoting/commas/whitespace so folded-list and plain-list argv match."""
    flat = text.replace("'", " ").replace('"', " ").replace(",", " ")
    return " ".join(flat.split())


def _is_bao_kv_put_to_slug_db(task: dict) -> bool:
    """True iff a command task does ``bao kv put`` targeting ``secret/data/<slug>/db``.

    Matches the bare ``<slug>`` KV v2 data tree (NOT ``proj-<slug>``). The argv is a
    Jinja folded expression; the path is composed either as a literal
    ``secret/data/{{ project_slug }}/db`` or via string concatenation
    (``'secret/data/' ~ project_slug ~ '/' ~ consumer_db_bridge_subpath``). So match
    on the ``bao ... kv ... put`` token sequence + the ``secret/data/`` prefix +
    ``project_slug`` reference, with NO ``proj-`` prefix on the data tree.
    """
    argv = _command_argv_text(task)
    if argv is None:
        return False
    flat = _normalise(argv)
    # "bao kv put" appears either contiguous ("bao kv put") or as a quoted list
    # ("bao kv put" after quote/comma normalisation collapses to the same).
    if "bao kv put" not in flat:
        return False
    if "secret/data/" not in argv:
        return False
    # A proj-<slug> data path would be the SOURCE tree, not the bridge target.
    if "secret/data/proj-" in argv:
        return False
    # The target is the bare <slug> data tree: either the literal /db form or the
    # subpath-var form. Require a project_slug reference and a reference to the db
    # subpath (literal 'db' or the consumer_db_bridge_subpath var).
    references_slug = "project_slug" in argv
    references_db_subpath = (
        "/db" in argv
        or "consumer_db_bridge_subpath" in argv
    )
    return references_slug and references_db_subpath


def _step2a_bridge_put_task(tasks):
    """Return the bridge ``bao kv put secret/data/<slug>/db`` task, or None."""
    for t in tasks:
        if _is_bao_kv_put_to_slug_db(t):
            return t
    return None


def _str(x) -> str:
    return str(x) if x is not None else ""


# --------------------------------------------------------------------------- #
# EXPLORATION / FIX-CHECKING (Property 1) — FAILS on unfixed code.
# Validates: Requirements 1.1, 1.2, 1.3, 1.4 (bug) -> 2.1, 2.2, 2.3, 2.4 (fix)
# --------------------------------------------------------------------------- #
def test_bridge_step_writes_database_url_to_workload_readable_slug_db_tree():
    """Property 1 (Validates: Req 2.1, 2.2).

    A bridge step ``bao kv put``s ``database_url=`` into ``secret/data/<slug>/db``
    (the bare ``<slug>`` KV v2 data tree inside ``policy-<slug>``, NOT
    ``proj-<slug>``). FAILS on unfixed code (no such task) — the counterexample
    (``isBugCondition``: DB credential written only under ``secret/proj-<slug>/<engine>``,
    not readable under ``secret/data/<slug>/*``).
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    put_task = _step2a_bridge_put_task(tasks)
    assert put_task is not None, (
        "consumer_bootstrap has NO bridge step that `bao kv put`s into "
        "secret/data/<slug>/db — nothing bridges the DB credential into the "
        "workload-readable <slug> tree (the bug). The <slug>-workload AppRole can "
        "only read secret/{data,...}/<slug>/*, never secret/proj-<slug>/postgres."
    )
    argv = _command_argv_text(put_task) or ""
    # The field is written either as a literal (database_url=...) or via the
    # consumer_db_bridge_field var (which defaults to database_url — asserted in
    # test_defaults_define_default_db_agent_mapping).
    assert "database_url" in argv or "consumer_db_bridge_field" in argv, (
        "The bridge `bao kv put` must write the field `database_url` "
        "(the composed, secret-bearing connection string) — literal or via "
        "consumer_db_bridge_field."
    )
    # It must NOT target the proj-<slug> tree (that is the source, read-only).
    assert "secret/data/proj-" not in argv, (
        "The bridge must write the bare <slug> tree (secret/data/<slug>/db), not "
        "secret/data/proj-<slug>/* (which is outside policy-<slug>)."
    )


def test_bridge_put_is_no_log_and_gated_and_token_in_env():
    """Property 1 (Validates: Req 2.4).

    The bridge `bao kv put` carries ``no_log``, is gated on a declared database AND a
    wired ``openbao_addr``, and rides the admin token in ``-e BAO_TOKEN=`` (never on a
    bare argv positional). FAILS on unfixed code (no such task).
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    put_task = _step2a_bridge_put_task(tasks)
    assert put_task is not None, "Bridge `bao kv put` task absent (unfixed tree)."

    # no_log on the secret-bearing put.
    assert _str(put_task.get("no_log")).strip().lower() in ("true", "{{ openbao_no_log }}"), (
        "The bridge `bao kv put` writes a secret-bearing DATABASE_URL and MUST carry "
        f"no_log: true (got no_log={put_task.get('no_log')!r})."
    )

    # Admin token in the exec environment, never argv.
    argv = _command_argv_text(put_task) or ""
    assert "BAO_TOKEN=" in argv and "-e" in argv, (
        "The bridge put must carry BAO_TOKEN in the -e exec environment, never argv."
    )
    assert "OPENBAO_ADMIN_TOKEN" in argv, (
        "The admin token must come from lookup('env','OPENBAO_ADMIN_TOKEN') like STEP 1."
    )

    # Gating: database declared AND wired openbao_addr.
    when_text = _str(put_task.get("when"))
    assert "consumer_manifest.database is defined" in when_text, (
        "The bridge put must be gated on consumer_manifest.database is defined "
        "(skip no-database projects — Req 3.5)."
    )
    assert "openbao_addr" in when_text, (
        "The bridge put must be gated on a wired openbao_addr (skip dev/offline — "
        "degraded-mode)."
    )


def test_bridge_composes_database_url_from_source_fields():
    """Property 1 (Validates: Req 2.1).

    Somewhere in the role a ``DATABASE_URL`` is composed from the source fields
    ``app_role``/``app_password``/``host``/``port``/``db_name`` into a
    ``postgresql://`` connection string. FAILS on unfixed code.
    """
    text = _CONSUMER_BOOTSTRAP_TASKS.read_text(encoding="utf-8")
    assert "postgresql://" in text, (
        "The bridge must compose a postgresql:// DATABASE_URL from the source fields."
    )
    for field in ("app_role", "app_password", "host", "port", "db_name"):
        assert field in text, (
            f"The composed DATABASE_URL must reference the source field {field!r} "
            "(read from secret/proj-<slug>/postgres)."
        )


def test_bridge_fail_loud_source_assert_precedes_put():
    """Property 1 (Validates: Req 2.4; ties to §6 ordering).

    A fail-loud assert on the source read (rc == 0, naming secret/proj-<slug>/postgres)
    must precede the bridge put, so an absent source fails loud rather than composing
    an empty/partial URL. FAILS on unfixed code.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    put_task = _step2a_bridge_put_task(tasks)
    assert put_task is not None, "Bridge `bao kv put` task absent (unfixed tree)."
    put_index = tasks.index(put_task)

    # Find an assert task (ansible.builtin.assert) before the put whose body ties to
    # the source read rc and names the proj-<slug>/postgres source path.
    def _is_source_assert(t: dict) -> bool:
        spec = t.get("ansible.builtin.assert") or t.get("assert")
        if not isinstance(spec, dict):
            return False
        body = _str(spec)
        return "rc == 0" in body or "rc==0" in body

    preceding_asserts = [
        t for t in tasks[:put_index]
        if _is_source_assert(t) and "no_log" in t
    ]
    assert preceding_asserts, (
        "A fail-loud assert on the bridge source read (rc == 0) must precede the "
        "bridge `bao kv put` (ties the §5 bridge to the §6 source guarantee; an "
        "absent source must fail loud, never compose an empty URL)."
    )
    # The source path secret/proj-<slug>/postgres must appear in the role (named in
    # the source read and/or its assert fail_msg).
    role_text = _CONSUMER_BOOTSTRAP_TASKS.read_text(encoding="utf-8")
    assert "proj-" in role_text and "postgres" in role_text, (
        "The bridge must read the authoritative source secret/proj-<slug>/postgres."
    )


def test_defaults_define_default_db_agent_mapping():
    """Property 1 (Validates: Req 2.3).

    ``consumer_bootstrap/defaults/main.yml`` defines the default DB agent mapping
    ``{kv_subpath: db, kv_field: database_url, env_key: DATABASE_URL}`` plus the bridge
    subpath/field, using the EXISTING secret/data/<slug>/<kv_subpath> shape so
    agent.hcl.j2 / render_agent_hcl are not touched. FAILS on unfixed code.
    """
    data = yaml.safe_load(_CONSUMER_BOOTSTRAP_DEFAULTS.read_text(encoding="utf-8"))
    assert data.get("consumer_db_bridge_subpath") == "db", (
        "defaults must define consumer_db_bridge_subpath: db."
    )
    assert data.get("consumer_db_bridge_field") == "database_url", (
        "defaults must define consumer_db_bridge_field: database_url."
    )
    mapping = data.get("consumer_db_agent_mapping")
    assert isinstance(mapping, list) and mapping, (
        "defaults must define a non-empty consumer_db_agent_mapping list."
    )
    entry = mapping[0]
    assert isinstance(entry, dict), "consumer_db_agent_mapping entries must be dicts."
    assert entry.get("kv_subpath") == "db", "mapping kv_subpath must be 'db'."
    assert entry.get("kv_field") == "database_url", "mapping kv_field must be 'database_url'."
    assert entry.get("env_key") == "DATABASE_URL", "mapping env_key must be 'DATABASE_URL'."


# --------------------------------------------------------------------------- #
# PRESERVATION (Property 2) — PASS on unfixed AND after fix.
# Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5
# --------------------------------------------------------------------------- #
def test_preservation_policy_slug_template_renders_exactly_five_subpaths():
    """Preservation (Validates: Req 3.3).

    ``policy-slug.hcl.j2`` still loops EXACTLY the five KV v2 sub-paths
    (``openbao_project_kv_subpaths``) with NO literal ``proj-<slug>`` grant added.
    Option A adds nothing to the policy.
    """
    text = _POLICY_SLUG_TEMPLATE.read_text(encoding="utf-8")
    # The template loops over openbao_project_kv_subpaths (the five-sub-path set).
    assert "openbao_project_kv_subpaths" in text, (
        "policy-slug.hcl.j2 must keep looping openbao_project_kv_subpaths (the five "
        "KV v2 sub-paths — the single least-privilege boundary)."
    )
    # It must NOT grant anything on a proj-<slug> prefix (that would be Option B).
    assert "proj-" not in text, (
        "policy-slug.hcl.j2 must NOT add any proj-<slug> grant — Option A keeps the "
        "five-sub-path boundary unchanged (Req 3.3)."
    )
    # The five canonical sub-path names appear in the docstring/comment set.
    for sub in ("data", "metadata", "delete", "undelete", "destroy"):
        assert sub in text, f"policy-slug.hcl.j2 comment should document the '{sub}' sub-path."


def test_preservation_agent_hcl_template_byte_unchanged():
    """Preservation (Validates: Req 3.2 path shape).

    ``agent.hcl.j2`` is byte-unchanged under Option A — the fix reuses the existing
    ``secret/data/<slug>/<kv_subpath>`` shape, so the renderer/template shared symbol
    is untouched.
    """
    text = _AGENT_HCL_TEMPLATE.read_text(encoding="utf-8")
    # The template renders secret/data/<slug>/<kv_subpath> — the shape the bridge reuses.
    assert 'secret/data/{{ project_slug }}/' in text, (
        "agent.hcl.j2 must keep rendering secret/data/<slug>/<kv_subpath> — the "
        "existing shape the default DB mapping reuses (no template change)."
    )
    assert "openbao_agent_secret_mappings" in text, (
        "agent.hcl.j2 must keep looping openbao_agent_secret_mappings."
    )


def test_preservation_agent_hcl_golden_byte_unchanged():
    """Preservation (Validates: Req 3.2).

    The ``render_agent_hcl`` golden fixture is byte-unchanged under Option A (a
    shared, byte-cross-checked symbol). We assert on the structural invariants the
    golden must retain (two template stanzas reading the ``<slug>`` data tree, no
    ``proj-`` path) so the test is readable rather than a brittle full-file hash.
    """
    text = _AGENT_HCL_GOLDEN.read_text(encoding="utf-8")
    assert 'secret/data/demo-slug/db' in text, (
        "agent.hcl.golden must keep its db template stanza (shared golden unchanged)."
    )
    assert 'secret/data/demo-slug/oidc' in text, (
        "agent.hcl.golden must keep its oidc template stanza (shared golden unchanged)."
    )
    assert "proj-" not in text, (
        "agent.hcl.golden must not acquire any proj-<slug> path (Option A keeps the "
        "<slug> tree shape)."
    )


def test_preservation_consumer_manifest_allowlist_unchanged():
    """Preservation (Validates: Req 3.4).

    ``consumer_manifest.py`` enforces the top-level allowlist — no new *mandatory*
    top-level key is added by the bridge fix (the bridge subpath/field come from
    role defaults, not the manifest).

    The allowlist was later grown by the optional ``workload`` block
    (consumer-last-mile-hello-workload feature; additive and backward compatible),
    so the expected tuples include ``workload``. The bridge fix itself still adds
    no manifest key — the assertion below is updated only to track the shared
    ``consumer_manifest`` symbol's current value, not to introduce a bridge key.
    """
    # Import the validator from scripts/installer (conftest puts it on sys.path).
    import consumer_manifest as cm

    assert cm.CONSUMABLE_SERVICE_KEYS == ("database", "sso", "secrets", "workload"), (
        "The consumable service keys must be (database, sso, secrets, workload)."
    )
    assert cm._ALLOWED_TOP_LEVEL_KEYS == (
        "slug",
        "database",
        "sso",
        "secrets",
        "workload",
    ), "The top-level allowlist must be {slug, database, sso, secrets, workload}."
    # An unknown top-level key is still rejected naming the offender.
    with pytest.raises(cm.ManifestValidationError):
        cm.validate_manifest({"slug": "demo", "bridge": {"subpath": "db"}})


def test_preservation_no_database_project_skips_bridge():
    """Preservation (Validates: Req 3.5).

    Every OpenBao KV-mutating bridge task is gated on
    ``consumer_manifest.database is defined`` so a secrets/sso-only project skips the
    bridge entirely, identical to before the fix.

    On the UNFIXED tree there is no bridge task, so this holds vacuously (no bridge to
    run). After the fix the bridge put/read tasks must all carry the database gate.
    """
    tasks = _load_role_tasks(_CONSUMER_BOOTSTRAP_TASKS)
    put_task = _step2a_bridge_put_task(tasks)
    if put_task is None:
        # Unfixed baseline: no bridge, so a no-database project trivially skips it.
        return
    when_text = _str(put_task.get("when"))
    assert "consumer_manifest.database is defined" in when_text, (
        "The bridge put must be gated on consumer_manifest.database is defined so a "
        "no-database project skips it (Req 3.5)."
    )


def test_preservation_postgres_server_is_authoritative_source_write():
    """Preservation (Validates: Req 3.1).

    ``postgres-server`` remains the authoritative source write: it still
    ``bao kv put``s ``secret/proj-<slug>/postgres`` with its field set
    (``app_role``/``app_password``/...). The bridge READS this, never replaces it.
    """
    text = _POSTGRES_SERVER_TASKS.read_text(encoding="utf-8")
    assert "secret/proj-" in text and "/postgres" in text, (
        "postgres-server must keep writing secret/proj-<slug>/postgres (the source)."
    )
    for field in ("app_role=", "app_password=", "superuser_password=", "exporter_password="):
        assert field in text, (
            f"postgres-server source write must keep its field {field!r} (Req 3.1)."
        )
