"""Bug-condition exploration test — SVC-06 ZITADEL <-> backing-Postgres
`shared_zitadel_app` password mismatch.

Spec: fix-svc06-zitadel-db-app-password-mismatch (bugfix) — Task 1, design.md
"Correctness Properties / Property 1: Bug Condition — credential-chain
consistency for `shared_zitadel_app`".

=== WHAT THIS TEST IS ===

This is the *exploration* test of the bugfix workflow. It encodes the Bug
Condition from design.md/bugfix.md and asserts the Expected Behavior
(Property 1). It is DELIBERATELY written to FAIL on the UNFIXED code — that
failure is the success signal that confirms the bug exists. Once the fix
(task 3.x) lands, this SAME test is re-run (task 3.2) and MUST pass.

=== THE CONFIRMED ROOT CAUSE (reproduced live on the `shrimp` throwaway cluster) ===

On a from-scratch SVC-06 bring-up, ZITADEL crash-loops with
`password authentication failed for user "shared_zitadel_app" (SQLSTATE 28P01)`.
The mechanism (hypothesis #4 "asymmetric/empty OpenBao gate", with hypothesis
#2's silent-empty-fallback masking it):

  1. `svc06_openbao_addr` defaults to "" in BOTH the orchestrator
     (`ansible/playbooks/svc-06-bootstrap.yml`) AND the two Phase roles
     (`svc06_provision`, `svc06_bootstrap`). A standard SVC-06 bring-up never
     sets it.
  2. With it empty, `svc06_provision` passes `openbao_addr=''` to the backing-DB
     child `svc-01-bootstrap`, and `svc06_bootstrap` passes `zitadel_openbao_addr=''`
     to the `zitadel.yml` run — the WHOLE handoff is disabled.
  3. In `postgres-server` (SVC-01, the writer): the OpenBao READ (reuse) and
     WRITE (`bao kv put ... app_password=`) are BOTH gated on
     `openbao_addr | length > 0`, so with it empty they are SKIPPED — nothing is
     stored in OpenBao. BUT the `ALTER ROLE shared_zitadel_app ... PASSWORD`
     task runs UNCONDITIONALLY, setting the live role to a freshly-generated
     random 32-char password.
  4. In `zitadel-server` (SVC-06, the reader): the `app_password` read from
     `secret/proj-shared-zitadel/postgres` is gated on
     `zitadel_openbao_addr | length > 0`, so with it empty it is SKIPPED;
     `zitadel_db_password` binds to the empty `zitadel_db_password_override`
     default (the `failed_when: false` + empty-override fallthrough). ZITADEL
     then presents an EMPTY password -> SASL 28P01.

LIVE COUNTEREXAMPLE captured read-only on `shrimp` (VMIDs 1070/1071/1102/1103 up):
  * Backing DB (10.0.20.14) `postgres-postgres-1`:
      SELECT rolname,(rolpassword IS NOT NULL) FROM pg_authid
        WHERE rolname='shared_zitadel_app'  ->  shared_zitadel_app|t
    (role exists WITH a non-empty password — the random one SVC-01 ALTERed).
  * ZITADEL (10.0.20.15) 0600 secret env:
      ZITADEL_DATABASE_POSTGRES_USER_PASSWORD  length=0
      ZITADEL_DATABASE_POSTGRES_ADMIN_PASSWORD length=0
    (ZITADEL presents an EMPTY password — the read was skipped, override empty).
  * ZITADEL container `zitadel-zitadel-1`: Restarting (1); logs:
      failed SASL auth: FATAL: password authentication failed for user
      "shared_zitadel_app" (SQLSTATE 28P01)
  -> role_password=<random32> != bao_app_password=<absent> != zitadel_presented=<empty>
     => isBugCondition(X) == True.  Confirms hypothesis #4 (+ #2 masking).

=== WHY THIS IS AN OFFLINE STRUCTURAL TEST ===

The live reproduction above is already captured (and is recorded in
validation evidence). The deterministic root cause is a *static configuration*
fact — the empty default and the silent-fallback read — so it is cheaply and
reproducibly encoded OFFLINE by inspecting the Ansible config layer, making the
regression CI-visible without a live cluster. The two offline checks mirror the
two fix parts:

  Part A (handoff enabled by default): on a bring-up where OpenBao is present,
  the SVC-01 write-gate (`openbao_addr`) and the ZITADEL read-gate
  (`zitadel_openbao_addr`) MUST resolve to the SAME non-empty OpenBao address,
  so the same secret flows end-to-end. Encoded here as: the SVC-06 defaults that
  feed those gates (`svc06_openbao_addr`) MUST default to a non-empty OpenBao
  primary address, overridable to "" only for a deliberate offline path.

  Part B (fail-fast reader): when `zitadel_openbao_addr` is set (OpenBao wired)
  but the `app_password` read is empty/failed, the `zitadel-server` role MUST
  fail the play with a diagnostic naming the expected KV path+field, instead of
  silently binding an empty `zitadel_db_password` via the `failed_when: false` +
  empty-override fallthrough.

=== EXPECTED OUTCOME ===

- On the UNFIXED tree: FAIL — `svc06_openbao_addr` defaults to "" (handoff
  disabled), and the reader has no fail-fast for the empty-read-while-wired case
  (silent empty fallthrough). Both confirm the bug.
- On the FIXED tree (task 3.x): PASS — `svc06_openbao_addr` defaults to the
  OpenBao primary address (handoff on by default; both gates satisfied by the
  same source) and the reader fails fast naming the KV path+field when wired but
  the read is empty.

Run:  ~/venv/devinfra/bin/pytest \\
        infra/tests/test_svc06_zitadel_db_password_bug_condition.py -v
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO_ROOT / "ansible" / "playbooks" / "svc-06-bootstrap.yml"
_PROVISION_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_provision" / "defaults" / "main.yml"
)
_BOOTSTRAP_DEFAULTS = (
    _REPO_ROOT / "ansible" / "roles" / "svc06_bootstrap" / "defaults" / "main.yml"
)
_ZITADEL_TASKS = (
    _REPO_ROOT / "ansible" / "roles" / "zitadel-server" / "tasks" / "main.yml"
)

#: The OpenBao primary's VLAN-20 host_index-0 address (NET-00: 10.0.20.10). The
#: SVC-06 bring-up must default its OpenBao address to this (or another non-empty
#: resolved OpenBao address) so the credential handoff is wired by default. This
#: matches the svc07-bootstrap primary IP + the zitadel-server
#: `zitadel_openbao_cli_host` default.
_OPENBAO_PRIMARY_IP = "10.0.20.10"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _play_vars(path: Path) -> dict:
    """Return the top-level play's `vars:` block from a one-play playbook."""
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    assert docs, f"{path} parsed to no documents"
    play = docs[0][0] if isinstance(docs[0], list) else docs[0]
    return (play or {}).get("vars", {}) or {}


def _role_defaults(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


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


def _resolved_openbao_default(value) -> str:
    """Return a string rendering of an `svc06_openbao_addr` default.

    The default may be a literal string, or a Jinja expression that resolves to
    the OpenBao primary address. We only need to know whether the *committed
    default* is empty (handoff disabled) or carries the OpenBao primary address
    (handoff enabled). An empty string literal renders empty; anything mentioning
    the primary IP renders non-empty.
    """
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- #
# Part A — handoff enabled by default: both gates resolve to the SAME non-empty
# OpenBao address so the same secret flows writer -> OpenBao -> reader.
# --------------------------------------------------------------------------- #
class TestHandoffEnabledByDefault:
    """Property 1 (Part A): a standard SVC-06 bring-up must wire the OpenBao
    credential handoff by default — `svc06_openbao_addr` must default to a
    non-empty OpenBao primary address at EVERY layer that forwards it (the
    orchestrator play + the two Phase roles), so the SVC-01 write-gate
    (`openbao_addr`) and the ZITADEL read-gate (`zitadel_openbao_addr`) are both
    satisfied by the SAME source.

    On the UNFIXED tree every one of these defaults is "" (handoff disabled), so
    these assertions FAIL — the intended exploration outcome confirming the bug.
    """

    def test_orchestrator_default_openbao_addr_is_non_empty(self):
        v = _play_vars(_ORCHESTRATOR).get("svc06_openbao_addr", None)
        resolved = _resolved_openbao_default(v)
        assert resolved.strip() != "", (
            "svc-06-bootstrap.yml must default `svc06_openbao_addr` to a "
            "non-empty OpenBao primary address so a standard bring-up wires the "
            "credential handoff (both the SVC-01 write-gate and the ZITADEL "
            "read-gate are satisfied). On the unfixed tree it defaults to '' "
            "which DISABLES the whole handoff -> SVC-01 ALTERs a random password "
            "that ZITADEL never receives (SQLSTATE 28P01). "
            f"Got: {v!r}"
        )

    def test_provision_role_default_openbao_addr_is_non_empty(self):
        # svc06_provision forwards svc06_openbao_addr as openbao_addr to the
        # child svc-01-bootstrap (the writer's gate). Its default (or the play's
        # default it inherits) must be non-empty.
        prov = _role_defaults(_PROVISION_DEFAULTS)
        play = _play_vars(_ORCHESTRATOR)
        # The provision role may rely on the play-level default; accept either a
        # non-empty role default OR a non-empty play default (the role has no
        # own override that re-empties it).
        role_val = prov.get("svc06_openbao_addr", None)
        play_val = play.get("svc06_openbao_addr", None)
        effective = (
            _resolved_openbao_default(role_val)
            if role_val is not None
            else _resolved_openbao_default(play_val)
        )
        assert effective.strip() != "", (
            "The svc06_provision path (which forwards `openbao_addr` to the "
            "backing-DB child svc-01-bootstrap — the writer's OpenBao gate) must "
            "resolve `svc06_openbao_addr` to a non-empty OpenBao address by "
            "default. On the unfixed tree it is '' so SVC-01's OpenBao write is "
            "skipped while its ALTER ROLE still sets a random password. "
            f"role default={role_val!r}, play default={play_val!r}"
        )

    def test_bootstrap_role_default_openbao_addr_is_non_empty(self):
        # svc06_bootstrap forwards svc06_openbao_addr as zitadel_openbao_addr to
        # the zitadel.yml run (the reader's gate). Its default must be non-empty.
        boot = _role_defaults(_BOOTSTRAP_DEFAULTS)
        play = _play_vars(_ORCHESTRATOR)
        role_val = boot.get("svc06_openbao_addr", None)
        play_val = play.get("svc06_openbao_addr", None)
        effective = (
            _resolved_openbao_default(role_val)
            if role_val is not None
            else _resolved_openbao_default(play_val)
        )
        assert effective.strip() != "", (
            "The svc06_bootstrap path (which forwards `zitadel_openbao_addr` to "
            "the zitadel.yml run — the reader's OpenBao gate) must resolve "
            "`svc06_openbao_addr` to a non-empty OpenBao address by default. On "
            "the unfixed tree it is '' so ZITADEL's OpenBao read is skipped and "
            "it binds the empty override password. "
            f"role default={role_val!r}, play default={play_val!r}"
        )

    def test_both_gates_resolve_to_the_same_openbao_source(self):
        """The writer-gate value (forwarded as openbao_addr) and the reader-gate
        value (forwarded as zitadel_openbao_addr) both derive from the SAME
        `svc06_openbao_addr`, so when non-empty they are identical — the same
        OpenBao source end-to-end.
        """
        prov = _PROVISION_DEFAULTS  # noqa: F841 (documented linkage)
        prov_tasks = _load_tasks(
            _REPO_ROOT
            / "ansible"
            / "roles"
            / "svc06_provision"
            / "tasks"
            / "main.yml"
        )
        boot_tasks = _load_tasks(
            _REPO_ROOT
            / "ansible"
            / "roles"
            / "svc06_bootstrap"
            / "tasks"
            / "main.yml"
        )
        prov_text = str(prov_tasks).lower()
        boot_text = str(boot_tasks).lower()
        # The writer gate is fed from svc06_openbao_addr via `openbao_addr=...`.
        assert "openbao_addr={{ svc06_openbao_addr" in prov_text, (
            "svc06_provision must forward svc06_openbao_addr as openbao_addr to "
            "the backing-DB child (the writer's gate)"
        )
        # The reader gate is fed from the same svc06_openbao_addr via
        # `zitadel_openbao_addr=...`.
        assert "zitadel_openbao_addr={{ svc06_openbao_addr" in boot_text, (
            "svc06_bootstrap must forward svc06_openbao_addr as "
            "zitadel_openbao_addr to the zitadel.yml run (the reader's gate)"
        )


# --------------------------------------------------------------------------- #
# Part B — fail-fast reader: wired-but-empty read must HALT, not bind empty.
# --------------------------------------------------------------------------- #
class TestReaderFailsFastOnEmptyWiredRead:
    """Property 1 (Part B): when OpenBao is wired (`zitadel_openbao_addr` set)
    but the `app_password` read returns empty/failed, the `zitadel-server` role
    must FAIL the play with a diagnostic naming the expected KV path+field,
    instead of silently binding an empty `zitadel_db_password` via the
    `failed_when: false` + empty-override fallthrough.

    On the UNFIXED tree there is NO such fail-fast assert — the read is
    `failed_when: false` and the bind falls through to the empty override — so
    this assertion FAILS, confirming the silent-masking half of the bug
    (requirement 1.3 / 2.3).
    """

    def test_reader_has_a_fail_fast_for_empty_wired_read(self):
        tasks = _load_tasks(_ZITADEL_TASKS)
        text = _ZITADEL_TASKS.read_text(encoding="utf-8")

        # There must be a fail-fast task (ansible.builtin.fail or an assert) that
        # fires when zitadel_openbao_addr is set but the app_password read is
        # empty/failed, and whose message names the KV path+field.
        has_fail_fast = False
        for t in tasks:
            module = next(
                (
                    k
                    for k in (
                        "ansible.builtin.fail",
                        "ansible.builtin.assert",
                        "fail",
                        "assert",
                    )
                    if k in t
                ),
                None,
            )
            if not module:
                continue
            body = str(t).lower()
            # Must be about the backing-DB password read being empty while wired.
            mentions_db_password_read = (
                "zitadel_db_password_read" in body or "app_password" in body
            )
            mentions_wired = "zitadel_openbao_addr" in body
            names_path_field = "app_password" in body and (
                "proj-" in body
                or "postgres" in body
                or "zitadel_openbao_kv_mount" in body
                or "kv path" in body
            )
            if mentions_db_password_read and mentions_wired and names_path_field:
                has_fail_fast = True
                break

        assert has_fail_fast, (
            "zitadel-server must FAIL FAST when zitadel_openbao_addr is set "
            "(OpenBao wired) but the app_password read is empty/failed — with a "
            "diagnostic naming the expected KV path+field "
            "(secret/proj-<slug>/postgres field app_password). On the unfixed "
            "tree the read is `failed_when: false` and `zitadel_db_password` "
            "silently falls through to the empty `zitadel_db_password_override`, "
            "so ZITADEL presents an EMPTY password and Postgres rejects it with "
            "SQLSTATE 28P01 (the crash is deferred + the real cause masked). "
            "No fail-fast task matching (wired + empty app_password read + "
            "names the KV path/field) was found in zitadel-server/tasks/main.yml."
        )

    def test_offline_override_path_is_preserved(self):
        """The deliberate OFFLINE/dev path must still work: when
        `zitadel_openbao_addr` is empty AND an explicit non-empty
        `zitadel_db_password_override` is supplied, the role uses the override
        (no fail-fast). We assert the role still references the override var so
        the offline path is not removed by the fix.
        """
        text = _ZITADEL_TASKS.read_text(encoding="utf-8")
        assert "zitadel_db_password_override" in text, (
            "the deliberate offline/dev override path "
            "(`zitadel_db_password_override`) must be preserved by the fix"
        )
