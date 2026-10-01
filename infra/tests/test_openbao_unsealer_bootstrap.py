"""Offline (Tier-1) regression guards for the SVC-07 unsealer-first bootstrap gap.

Spec: fix-openbao-unsealer-bootstrap (bugfix) — tasks 5 (regression guard (a),
automation-reachability) and 6 (regression guard (b), docs-presence).

=== WHY THESE EXIST ===

The SVC-07 OpenBao bring-up cannot be completed by an operator following the
documented command (`ansible/playbooks/openbao.yml --tags "install,init"`,
INSTALL-RUNBOOK §2). Two coupled halves strand the operator, and NO offline check
caught either — which is exactly why the bug hid until a live bring-up hit the
primary's fail-closed token assertion:

  * **Gap 1 (automation).** `openbao.yml` binds all three roles to a single
    `hosts: openbao` play, and the generated inventory places ONLY the primary
    (`svc07-20-01`) in group `openbao` — the unsealer (`svc07-unsealer-20-01`) is
    in a SEPARATE `openbao-unsealer` group. Combined with `openbao_role` defaulting
    to `"primary"` everywhere and NO `ansible/group_vars/` setting it to
    `"unsealer"`, the `when: openbao_role == "unsealer"` Transit-init tasks are
    UNREACHABLE via the runbook command.
  * **Gap 2 (documentation).** The runbook §2 defers the token lifecycle to a
    "Runbook 1" that never documents how the Bootstrap_Transit_Token is *produced*
    (unsealer `operator init`/unseal → confirm engine + key → mint a scoped token →
    store in the never-committed env source → run the primary bootstrap).

These two guards are the durable regression gate for that bug class. Both are
**offline / Tier-1** (they parse committed repo artifacts with PyYAML + text
matching; no live OpenBao/Docker/Terraform). By construction they are RED on the
unfixed tree and GREEN once the fix (group_vars + ordered two-play + runbook
walkthrough) lands. They are NOT property-based tests (PBT is not applicable to an
Ansible-config + docs fix — see design.md "PBT applicability note").

  * ``TestAutomationReachability`` (task 5, guard (a)) — asserts the OpenBao
    provisioning actually reaches the unsealer host with ``openbao_role: unsealer``,
    ordered BEFORE the primary bootstrap play.
  * ``TestDocsPresence`` (task 6, guard (b)) — asserts the install runbook carries
    the ordered provision-then-bootstrap walkthrough that MINTS the token before the
    primary ``--tags install,init`` step, with doctest ``requires-infra`` tiers on
    the live/mint steps.

Run:
  PYTHONPATH=infra/platform-foundation/scripts \
    ~/venv/devinfra/bin/pytest infra/tests/test_openbao_unsealer_bootstrap.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PLAYBOOK = _REPO_ROOT / "ansible" / "playbooks" / "openbao.yml"
_INVENTORY = (
    _REPO_ROOT / "ansible" / "inventory" / "svc-07-secrets-manager.generated.yml"
)
_GROUP_VARS_DIR = _REPO_ROOT / "ansible" / "group_vars"
_RUNBOOK = (
    _REPO_ROOT / "infra" / "projects" / "svc-07-secrets-manager" / "INSTALL-RUNBOOK.md"
)

#: The inventory group holding the unsealer host, and the unsealer host itself.
_UNSEALER_GROUP = "openbao-unsealer"
_UNSEALER_HOST = "svc07-unsealer-20-01"
#: The primary group the bring-up play historically (and still) targets.
_PRIMARY_GROUP = "openbao"
#: The role whose ``when: openbao_role == "unsealer"`` Transit-init tasks must be
#: reached for the unsealer.
_INIT_ROLE = "openbao_init_unseal"
#: The env-var NAME the primary's "6.2-STEP-0" reads; the walkthrough must mint a
#: value for it. Referenced by NAME only — never a value (secrets discipline).
_TOKEN_ENV_VAR = "OPENBAO_BOOTSTRAP_TRANSIT_TOKEN"


# --------------------------------------------------------------------------- #
# Parsing helpers.
# --------------------------------------------------------------------------- #
def _load_plays() -> list[dict]:
    """Parse ``openbao.yml`` into a list of play dicts."""
    plays = yaml.safe_load(_PLAYBOOK.read_text(encoding="utf-8"))
    assert isinstance(plays, list) and plays, (
        f"{_PLAYBOOK} must be a non-empty list of plays"
    )
    return plays


def _play_hosts(play: dict) -> str:
    """The ``hosts:`` value of a play, as a string (empty if absent)."""
    return str(play.get("hosts", "") or "")


def _play_targets_group(play: dict, group: str) -> bool:
    """True if the play's ``hosts:`` selector includes ``group``.

    Handles the plain ``hosts: <group>`` form and colon/comma-separated multi-group
    selectors (``openbao:openbao-unsealer``) via whole-token matching, so
    ``openbao`` does NOT spuriously match inside ``openbao-unsealer``.
    """
    hosts = _play_hosts(play)
    tokens = re.split(r"[:,\s]+", hosts.strip())
    return group in tokens


def _iter_tasks(tasks):
    """Yield every task dict recursively, descending block/rescue/always."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _iter_tasks(task[key])


def _task_role_name(task: dict) -> str | None:
    """Return the role name a task binds via import_role/include_role, else None.

    Ansible lets a play declare a role in its ``roles:`` list OR execute it as a
    task via ``ansible.builtin.import_role`` / ``import_role`` /
    ``ansible.builtin.include_role`` / ``include_role`` inside
    ``pre_tasks``/``tasks``/``post_tasks``. Both forms bind the role to the play's
    hosts; this recognises the task form. (Mirrors the include-extraction logic in
    ``tests/installer/test_unsealer_reseal_ordering.py::_play1_ordered_nodes`` —
    copied locally, not imported across test trees.)
    """
    for inc_key in (
        "ansible.builtin.import_role", "import_role",
        "ansible.builtin.include_role", "include_role",
    ):
        spec = task.get(inc_key)
        if isinstance(spec, dict):
            return str(spec.get("name", ""))
        if isinstance(spec, str):
            return spec
    return None


def _play_role_names(play: dict) -> list[str]:
    """The role names bound in a play, whether declared in the ``roles:`` list OR
    via ``import_role``/``include_role`` tasks in
    ``pre_tasks``/``tasks``/``post_tasks``.

    Supports the ``role:`` mapping form in ``roles:``. Post-bug-#9-fix, Play 1
    (``hosts: openbao-unsealer``) declares ``openbao_install`` and
    ``openbao_init_unseal`` via ``ansible.builtin.import_role`` inside ``tasks:``
    (with a ``meta: flush_handlers`` seam between them), so a ``roles:``-only read
    would miss them — this collects both forms.
    """
    names: list[str] = []
    for entry in play.get("roles", []) or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            names.append(str(entry.get("role", "")))
    for section in ("pre_tasks", "tasks", "post_tasks"):
        for task in _iter_tasks(play.get(section)):
            role_name = _task_role_name(task)
            if role_name:
                names.append(role_name)
    return names


def _play_sets_unsealer_role(play: dict) -> bool:
    """True if the play itself sets ``openbao_role: unsealer`` as a play var."""
    return str((play.get("vars", {}) or {}).get("openbao_role", "")) == "unsealer"


def _group_vars_sets_unsealer_role() -> bool:
    """True if a committed ``group_vars`` file sets ``openbao_role: unsealer`` for
    the ``openbao-unsealer`` group (``openbao-unsealer.yml`` or ``.../main.yml``).
    """
    if not _GROUP_VARS_DIR.is_dir():
        return False
    candidates = [
        _GROUP_VARS_DIR / f"{_UNSEALER_GROUP}.yml",
        _GROUP_VARS_DIR / f"{_UNSEALER_GROUP}.yaml",
        _GROUP_VARS_DIR / _UNSEALER_GROUP / "main.yml",
        _GROUP_VARS_DIR / _UNSEALER_GROUP / "main.yaml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict) and str(data.get("openbao_role", "")) == "unsealer":
            return True
    return False


def _inventory_group_has_host(group: str, host: str) -> bool:
    """True if ``host`` appears under ``group`` in the generated inventory."""
    data = yaml.safe_load(_INVENTORY.read_text(encoding="utf-8")) or {}
    children = (data.get("all", {}) or {}).get("children", {}) or {}
    grp = children.get(group, {}) or {}
    return host in (grp.get("hosts", {}) or {})


# --------------------------------------------------------------------------- #
# Task 5 — Regression guard (a): offline automation-reachability.
# --------------------------------------------------------------------------- #
class TestAutomationReachability:
    """The unsealer host is reached with ``openbao_role: unsealer``, before primary.

    RED on the unfixed tree (single ``hosts: openbao`` play, no ``group_vars``);
    GREEN once C1a (``group_vars/openbao-unsealer.yml``) + C1b (ordered two-play
    ``openbao.yml``) land. _Requirements: 2.1; bugfix Regression Test Req (a)._
    """

    def test_inventory_places_unsealer_in_its_own_group(self):
        """Sanity anchor: the generated inventory really does split the two hosts.

        This is the precondition that makes the bug possible — a single
        ``hosts: openbao`` play cannot reach the unsealer because it lives in the
        separate ``openbao-unsealer`` group. (Stays green before and after the fix;
        it pins the inventory shape the reachability assertions rely on.)
        """
        assert _inventory_group_has_host(_UNSEALER_GROUP, _UNSEALER_HOST), (
            f"expected {_UNSEALER_HOST!r} under inventory group "
            f"{_UNSEALER_GROUP!r} in {_INVENTORY}"
        )

    def test_a_play_reaches_the_unsealer_with_openbao_role_unsealer(self):
        """A play must bind ``openbao_init_unseal`` to the ``openbao-unsealer`` group
        with ``openbao_role: unsealer`` in effect (via group_vars OR a play var).

        FAILS on the unfixed tree: the only play targets ``hosts: openbao`` (the
        primary group), and no ``group_vars`` / play var sets
        ``openbao_role: unsealer`` — so the ``when: openbao_role == "unsealer"``
        Transit-init path is unreachable via the documented command.
        """
        plays = _load_plays()

        unsealer_plays = [
            p for p in plays
            if _play_targets_group(p, _UNSEALER_GROUP)
            and _INIT_ROLE in _play_role_names(p)
        ]
        assert unsealer_plays, (
            "no play binds "
            f"{_INIT_ROLE!r} to the {_UNSEALER_GROUP!r} group — the unsealer host "
            f"{_UNSEALER_HOST!r} is never reached by the bring-up command, so its "
            "`when: openbao_role == \"unsealer\"` Transit-init path cannot run. "
            f"plays target: {[_play_hosts(p) for p in plays]}"
        )

        role_is_unsealer = _group_vars_sets_unsealer_role() or any(
            _play_sets_unsealer_role(p) for p in unsealer_plays
        )
        assert role_is_unsealer, (
            "a play reaches the "
            f"{_UNSEALER_GROUP!r} group, but nothing sets `openbao_role: unsealer` "
            f"for it (no committed group_vars/{_UNSEALER_GROUP}.yml and no play "
            "var) — so `openbao_role` stays at its \"primary\" default and the "
            "unsealer Transit-init tasks are still skipped. group_vars dir exists: "
            f"{_GROUP_VARS_DIR.is_dir()}"
        )

    def test_unsealer_segment_is_ordered_before_primary_bootstrap(self):
        """The unsealer play must be ordered BEFORE the primary (`hosts: openbao`)
        bootstrap play.

        Guards against a combined / misordered structure that could run the
        primary's "6.2-STEP-0" before the unsealer's Transit-init completes.
        FAILS on the unfixed tree because there is no unsealer-targeting play at
        all (so no such play precedes the primary).
        """
        plays = _load_plays()

        first_unsealer_idx = next(
            (i for i, p in enumerate(plays) if _play_targets_group(p, _UNSEALER_GROUP)),
            None,
        )
        first_primary_idx = next(
            (
                i for i, p in enumerate(plays)
                if _play_targets_group(p, _PRIMARY_GROUP)
                and not _play_targets_group(p, _UNSEALER_GROUP)
            ),
            None,
        )

        assert first_unsealer_idx is not None, (
            "no play targets the "
            f"{_UNSEALER_GROUP!r} group at all — the unsealer bootstrap has no home "
            "and cannot precede the primary. "
            f"plays target: {[_play_hosts(p) for p in plays]}"
        )
        assert first_primary_idx is not None, (
            "no dedicated primary "
            f"({_PRIMARY_GROUP!r}-only) play found; expected the existing primary "
            "bootstrap play to remain. "
            f"plays target: {[_play_hosts(p) for p in plays]}"
        )
        assert first_unsealer_idx < first_primary_idx, (
            "the unsealer play (index "
            f"{first_unsealer_idx}) must be ordered BEFORE the primary bootstrap "
            f"play (index {first_primary_idx}) so unsealer-init completes first — "
            "an ordered two-play structure makes 'unsealer first, then primary' a "
            "structural guarantee."
        )


# --------------------------------------------------------------------------- #
# Task 6 — Regression guard (b): offline docs-presence.
# --------------------------------------------------------------------------- #
def _section_2_text() -> str:
    """Extract the runbook's '## 2. ...' section body (up to the next '## ')."""
    text = _RUNBOOK.read_text(encoding="utf-8")
    # Match a level-2 heading beginning with "2." (e.g. "## 2. Install & bring up
    # OpenBao"), capture until the next level-2 heading or EOF.
    m = re.search(
        r"^##\s+2\.[^\n]*\n(.*?)(?=^##\s+\d|\Z)",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert m, f"could not locate the '## 2.' section in {_RUNBOOK}"
    return m.group(1)


class TestDocsPresence:
    """The install runbook §2 carries an ordered provision-then-bootstrap
    walkthrough that MINTS the token before the primary ``--tags install,init``.

    RED on the unfixed §2 (which defers the token lifecycle to a "Runbook 1" that
    never states how the value is produced); GREEN once C4 lands the walkthrough.
    _Requirements: 2.3; bugfix Regression Test Req (b)._
    """

    def test_runbook_exists(self):
        assert _RUNBOOK.is_file(), f"expected the SVC-07 install runbook at {_RUNBOOK}"

    def test_section_2_documents_unsealer_operator_init_and_unseal(self):
        """§2 must document initialising and unsealing the UNSEALER first.

        FAILS on the unfixed §2: it only says 'unsealer first, then primary Raft
        init' in prose and defers to Runbook 1 — it never shows the unsealer's own
        ``bao operator init`` / ``bao operator unseal`` step.
        """
        body = _section_2_text().lower()
        assert "operator init" in body, (
            "§2 does not document `bao operator init` on the unsealer — the "
            "Shamir-sealed unsealer's own init/unseal precondition is missing "
            "(design C2)."
        )
        assert "operator unseal" in body or "unseal" in body, (
            "§2 does not document unsealing the unsealer (`bao operator unseal`)."
        )

    def test_section_2_documents_minting_the_scoped_token(self):
        """§2 must document MINTING the Bootstrap_Transit_Token (not just naming it).

        The mint is the content the unfixed runbook lacks: a `bao token create`
        (scoped to the transit unseal key) that PRODUCES the value for
        OPENBAO_BOOTSTRAP_TRANSIT_TOKEN. FAILS today because §2 only references the
        env-var NAME and defers production to a Runbook 1 that never mints it.
        """
        body = _section_2_text()
        # The env-var name must be present (it is, even on the unfixed tree)...
        assert _TOKEN_ENV_VAR in body, (
            f"§2 does not reference {_TOKEN_ENV_VAR} at all"
        )
        # ...but there must ALSO be an actual mint command producing the value.
        assert re.search(r"\bbao\s+token\s+create\b", body), (
            "§2 does not document a `bao token create` step that MINTS the "
            f"{_TOKEN_ENV_VAR} value — the token-production procedure is still "
            "missing (design C3, Option A). The env-var name alone is not a "
            "production procedure."
        )
        # The mint must be scoped against the transit unseal key (encrypt/decrypt),
        # never a root token — matched loosely on the transit key path / policy.
        assert re.search(r"transit/(encrypt|decrypt)/openbao-unseal", body) or (
            "openbao-unseal" in body and "policy" in body.lower()
        ), (
            "§2 mints a token but does not scope it to the transit unseal key "
            "(`transit/{encrypt,decrypt}/openbao-unseal`) — a scoped, non-root "
            "seal token is required (design C3)."
        )

    def test_section_2_mint_is_ordered_before_the_primary_bootstrap(self):
        """The token-mint must appear BEFORE the primary ``--tags install,init``.

        A walkthrough that mints AFTER running the primary would not satisfy the
        primary's "6.2-STEP-0" read. FAILS today because there is no mint step at
        all preceding the primary command.
        """
        body = _section_2_text()

        mint_m = re.search(r"\bbao\s+token\s+create\b", body)
        # Match the ACTUAL primary bring-up COMMAND — the `ansible-playbook ...
        # openbao.yml --tags "install,init"` invocation — not a prose backtick
        # mention of `--tags "install,init"`. The runbook now carries several
        # narrative callouts that reference `--tags "install,init"` inline BEFORE
        # the mint step; a bare `--tags install,init` regex matched the first such
        # prose mention and mis-detected the ordering. Anchoring on the playbook
        # path (`openbao.yml`) preceding the flag pins the real command line (only
        # the fenced `ansible-playbook` invocation has the playbook path there).
        primary_m = re.search(
            r'openbao\.yml\s+--tags\s+["\']?install,init["\']?', body
        )
        assert mint_m, (
            "no `bao token create` mint step found in §2 (see the mint-presence "
            "test) — cannot be ordered before the primary."
        )
        assert primary_m, (
            "no primary `ansible-playbook ... openbao.yml --tags install,init` "
            "bring-up command found in §2."
        )
        assert mint_m.start() < primary_m.start(), (
            "the `bao token create` mint step must appear BEFORE the primary "
            "`--tags install,init` command in §2 so the token exists when "
            "'6.2-STEP-0' reads it (ordered provision-then-bootstrap walkthrough)."
        )

    def test_section_2_stores_the_token_in_a_never_committed_env_source(self):
        """§2 must document storing the minted value in a gitignored `.env` or a
        GitLab CI protected+masked variable (env-var NAME only)."""
        body = _section_2_text().lower()
        assert (".env" in body) or ("gitlab" in body), (
            "§2 does not document placing the minted token into the never-committed "
            "env source (gitignored `.env` or a GitLab CI protected+masked variable)."
        )

    def test_section_2_mint_steps_are_requires_infra_annotated(self):
        """The live/mint steps in §2 must carry `<!-- doctest: requires-infra -->`.

        Per documentation-testing.md, the unsealer init/unseal + mint + primary
        bring-up commands need a live dependency, so they are `requires-infra`.
        FAILS today because there is no mint step to annotate (the assertion binds
        the annotation to the presence of the mint command).
        """
        body = _section_2_text()

        mint_m = re.search(r"\bbao\s+token\s+create\b", body)
        assert mint_m, (
            "no `bao token create` mint step found in §2 — nothing to tier-annotate "
            "(see the mint-presence test)."
        )
        # A requires-infra annotation must appear in the window preceding the mint
        # command (the fenced block the annotation guards). Look back a bounded
        # distance for the nearest tier marker.
        preceding = body[: mint_m.start()]
        window = preceding[-600:]
        assert "<!-- doctest: requires-infra -->" in window, (
            "the `bao token create` mint step in §2 is not preceded by a "
            "`<!-- doctest: requires-infra -->` tier annotation within its code "
            "block — live/mint commands must be tier-annotated (documentation-"
            "testing.md)."
        )
