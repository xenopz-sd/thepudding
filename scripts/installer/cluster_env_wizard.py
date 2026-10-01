#!/usr/bin/env python3
"""Guided, up-front, NON-cluster-mutating cluster-env authoring wizard.

Feature: platform-install-simplification (Req 2.1, 2.3; design Component 2
"cluster_env_wizard.py — the guided env-fill"; Correctness Properties 1 and 2).

This is the guided ``Guided_Env_Fill`` config-authoring step of the install-
simplification milestone. It removes the single most error-prone step in a
from-nothing platform bring-up — hand-editing ``cluster.env`` / ``cluster.dev.env``
— by interactively prompting for the cluster parameters, writing the NON-secret
ones to the operator's gitignored env file, validating the result, and then
STOPPING. It is invoked by absolute path, e.g.::

    ~/venv/devinfra/bin/python scripts/installer/cluster_env_wizard.py \
        --env-file cluster.dev.env

Design boundaries honoured here (design Component 2 "What the wizard is NOT";
service-installer.md §2.2 as scoped by ADR-0012; security-standards.md):

* **Config-authoring only, NEVER cluster-mutating (Req 2.3, Property 1).** The
  wizard's ONLY effect is to produce and/or validate the ``Cluster_Env_File``. It
  imports NO Ansible/Terraform, spawns NO subprocess, reads NO ``.tfstate``,
  queries NO Proxmox API, and calls NO throwaway guard (it is non-mutating, so no
  guard is needed). It NEVER invokes ``ansible-playbook`` / ``terraform`` / any
  bring-up: it is the author half of the ``Author_Then_Apply`` split — prompt →
  write file → STOP. The operator runs the two bring-up commands separately.

* **Schema reuse — no duplication (Req 2.1, Property 2).** The wizard imports the
  existing :mod:`cluster_env` module and drives its :data:`cluster_env.SCHEMA`
  tuple (the :class:`cluster_env.LogicalVar` rows) together with its
  :func:`cluster_env.load_env_file` / :func:`cluster_env.parse_env` /
  :func:`cluster_env.validate` functions. It does NOT redefine the variable set,
  the alias convention, or the validation rules — ``cluster_env.SCHEMA`` stays the
  single source of truth, so adding a var to ``cluster_env.py`` automatically
  flows into this wizard.

* **Admin-credential hygiene (Req 3, filled in by sub-task 2.3).** The two
  ``Admin_Bootstrap_Credential`` rows are prompted NO-ECHO, held in memory only,
  and NEVER written to any file. Only ordinary non-secret vars are written (at
  mode 0600), consistent with security-standards.md.

* **Single source of truth; non-interactive path first-class (Req 2.6,
  Property 2).** The wizard is ONE way to populate the ``Cluster_Env_File``; a
  hand-edited or pre-provisioned file remains fully usable with no wizard, and the
  automation phase still reads ONLY the file and fails closed at preflight.

Scope note (Task 2.1 — SCAFFOLD): this module establishes the docstring, the CLI,
the ``cluster_env`` import, and the helper signatures. The prompt-and-write body
(2.2), the identity-mint opt-in + no-echo admin handling (2.3), and the
validate-then-STOP + summary flow (2.4) are filled in by their respective
sub-tasks. Helpers whose bodies belong to a later sub-task raise
:class:`NotImplementedError` with a pointer to that sub-task; the trivial parts
(arg parsing, the ``cluster_env`` import, and the seed-load) are implemented now.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

# Schema reuse (Req 2.1): drive the EXISTING cluster_env schema + validation, never
# a second schema. ``cluster_env.py`` is a sibling module in this same package
# directory (``scripts/installer/``). When this file is run as a script, its own
# directory is ``sys.path[0]`` so the bare import resolves; the offline tests add
# ``scripts/installer/`` to ``sys.path`` via ``tests/installer/conftest.py`` (the
# repo's established conftest idiom), so ``import cluster_env`` resolves there too.
import cluster_env

# Logical names (from ``cluster_env.SCHEMA``) of the two Admin_Bootstrap_Credential
# rows. These are the ONLY rows prompted NO-ECHO and NEVER written to disk (Req 3).
# Located by logical name — never by position — so reordering/adding schema rows
# cannot silently repoint this at the wrong var (mirrors cluster_env's own
# ``_DATASTORE_SPEC`` lookup-by-logical-name discipline).
ADMIN_CREDENTIAL_LOGICAL_NAMES: tuple[str, ...] = ("Admin password", "Admin token")

# Mode for the written Cluster_Env_File: owner read/write only (Req 3.3 / 2.2).
ENV_FILE_MODE = 0o600

# Prompt-grouping sections, mirroring the comment sections in ``cluster_env.SCHEMA``
# ("SDN / API", "Host prep", "Gateway", "Dev machine"). Each row's PROXMOX_/PLATFORM_/
# ADMIN_ alias is matched to a section header purely for readability of the prompts
# (design Component 2 step 2: "Group the prompts by the schema's existing comment
# sections … for readability"). The grouping is display-only — it never changes which
# rows are prompted or how they resolve; a row not matched here falls under "Other".
# Located by the row's PROXMOX_ alias (never by position) so schema reordering cannot
# silently mis-group a prompt.
_PROMPT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "SDN / API",
        (
            "PROXMOX_ENDPOINT",
            "PROXMOX_API_TOKEN",
            "PROXMOX_NODE_NAME",
            "PROXMOX_LXC_TEMPLATE",
            "PROXMOX_DATASTORE_ID",
            "PROXMOX_SSH_PUBLIC_KEY_FILE",
        ),
    ),
    (
        "Host prep",
        (
            "PROXMOX_HOST_LAN_IP",
            "PROXMOX_TERRAFORM_ROLE_ID",
            "PROXMOX_TERRAFORM_TOKEN_ID",
            "PROXMOX_TEMPLATE_STORAGE",
        ),
    ),
    (
        "Gateway",
        ("ADMIN_SOURCE_CIDR",),
    ),
    (
        "Dev machine",
        ("PLATFORM_SERVED_VLAN_IDS",),
    ),
)


def _section_for(proxmox_alias: str) -> str:
    """Return the display section header for a row, by its PROXMOX_/PLATFORM_ alias."""
    for header, aliases in _PROMPT_SECTIONS:
        if proxmox_alias in aliases:
            return header
    return "Other"


def seed_defaults(env_file: str) -> dict[str, str]:
    """Seed prompt defaults from an existing ``Cluster_Env_File`` (or start empty).

    If ``env_file`` already exists, load + parse it with
    :func:`cluster_env.load_env_file` so a re-run of the wizard is an EDIT (the
    operator's current values become the prompt defaults) rather than a WIPE. If
    the file is absent, start from an empty mapping.

    Returns the parsed ``KEY=VALUE`` mapping as written on disk (alias keys, not
    yet alias-resolved) — the caller reads a row's current value by either alias.
    This helper is READ-ONLY (it never writes and never mutates the cluster); a
    parse error or a missing file simply yields an empty seed.

    Implemented in Task 2.1: the seed-load is a trivial, safe reuse of
    ``cluster_env.load_env_file`` and is needed by the 2.2 prompt loop.
    """
    try:
        return cluster_env.load_env_file(env_file)
    except cluster_env.EnvFileNotFoundError:
        # Absent/unreadable file -> start from empty defaults. This is the normal
        # first-run case, not an error: the wizard exists precisely to author a
        # file that does not yet exist.
        return {}


def prompt_non_secret_vars(seed: dict[str, str]) -> dict[str, str]:
    """Iterate ``cluster_env.SCHEMA`` and prompt for the NON-secret vars.

    Prompts for every schema row EXCEPT the two ``Admin_Bootstrap_Credential`` rows
    (see :data:`ADMIN_CREDENTIAL_LOGICAL_NAMES`), using the row's ``logical`` name,
    its ``mandatory`` flag, and any seeded default from ``seed``. Returns a mapping
    of the operator-supplied non-secret values keyed by the row's canonical alias,
    ready to be serialized to the env file.

    Behaviour (Task 2.2; design Component 2 step 2; Data Models):

    * Iterate ``cluster_env.SCHEMA`` in order, SKIPPING the two
      ``Admin_Bootstrap_Credential`` rows (located by logical name via
      :data:`ADMIN_CREDENTIAL_LOGICAL_NAMES` — never by position). Those rows are
      NO-ECHO / in-memory-only and are handled in Task 2.3; this helper never sees
      or writes them.
    * Prompts are grouped by the schema's comment sections (SDN/API, host-prep,
      gateway, devmachine) for readability: a section header is printed before the
      first row of each group.
    * Each prompt shows the row's ``logical`` name, whether it is mandatory
      (``spec.mandatory``), and any seeded default resolved from ``seed`` by EITHER
      alias (``spec.proxmox_alias`` first, then ``spec.tfvar_alias``) — so a file
      authored in either alias form re-runs as an edit, not a wipe.
    * Empty input keeps the seeded default (if any); if there is no seeded default,
      the var is simply left unset — an empty value is NOT written, because
      ``cluster_env`` treats empty/whitespace-only as absent (``_present``), so
      emitting ``KEY=`` would be indistinguishable from omission and only clutters
      the file.
    * The ``API token`` row is treated like any other optional-to-leave-blank row
      here: when identity-mint is opted in the bring-up MINTS the token, so the
      operator may leave it blank ("will be minted"); the mint-mode messaging is
      wired by ``run_wizard`` in Task 2.4 — this helper does not force it.

    Returns a mapping keyed by each row's **PROXMOX_ alias** (``spec.proxmox_alias``
    — the operator-facing key form used in ``cluster.env.example`` and the existing
    ``cluster.dev.env``). ``cluster_env.resolve_one`` accepts either alias, so a
    PROXMOX_-keyed file round-trips through :func:`seed_defaults` and validates
    identically to a TF_VAR_-keyed one; PROXMOX_ is chosen for consistency with how
    operators actually author these files. :func:`write_env_file` serializes this
    mapping verbatim.
    """
    collected: dict[str, str] = {}
    current_section: str | None = None

    for spec in cluster_env.SCHEMA:
        if spec.logical in ADMIN_CREDENTIAL_LOGICAL_NAMES:
            # Admin_Bootstrap_Credential rows: never prompted or written here
            # (Task 2.3 owns them, NO-ECHO / in-memory-only).
            continue

        section = _section_for(spec.proxmox_alias)
        if section != current_section:
            print(f"\n--- {section} ---")
            current_section = section

        # Seeded default: prefer the PROXMOX_ alias, fall back to the TF_VAR_ alias.
        seeded = seed.get(spec.proxmox_alias) or seed.get(spec.tfvar_alias) or ""
        seeded = seeded.strip()

        req = "mandatory" if spec.mandatory else "optional"
        if seeded:
            prompt = f"{spec.logical} ({req}) [{seeded}]: "
        else:
            prompt = f"{spec.logical} ({req}) []: "

        entered = input(prompt).strip()
        value = entered if entered else seeded

        # Empty stays unset — cluster_env treats empty as absent, so skip writing it.
        if value:
            collected[spec.proxmox_alias] = value

    return collected


def prompt_identity_mint_opt_in() -> bool:
    """Ask the single explicit identity-mint opt-in question (default NO).

    Asks whether the upcoming bring-up will MINT the ``terraform@pve`` token (which
    requires a Proxmox admin credential). Returns ``True`` only if the operator
    opts in. On ``True`` the caller flags that identity minting will occur and
    collects the admin credential NO-ECHO; on ``False`` the admin credential is
    neither prompted for nor collected (Req 3.4).

    Behaviour (Task 2.3; design Component 2 "Identity-mint opt-in"; Req 3.4, 3.5):

    * Asks exactly ONE explicit yes/no question via ``builtins.input``, DEFAULT NO.
      Only an explicit affirmative (``y`` / ``yes``, case-insensitive, trimmed)
      returns ``True``; anything else — including an empty answer — returns
      ``False`` (Identity_Mint_Opt_In stays opt-in, default OFF).
    * On ``True``, prints (right after the decision, so the flag is adjacent to it)
      a clear line that identity minting WILL occur and that a Proxmox admin
      credential will be requested (Req 3.5 — flag BEFORE the credential is
      collected), plus a reminder to pass ``-e bootstrap_pve_identity=true`` to the
      Platform_Prerequisites_Command.
    * NEVER echoes any secret — it only asks a yes/no and prints the flag/reminder.
    """
    answer = input(
        "Will this bring-up MINT the terraform@pve token "
        "(requires a Proxmox admin credential)? [y/N]: "
    ).strip().lower()

    opted_in = answer in ("y", "yes")

    if opted_in:
        # Flag adjacent to the decision, BEFORE any credential is collected (Req 3.5).
        print(
            "\n[identity mint] Identity minting WILL occur: a Proxmox admin "
            "credential will now be requested (no-echo, held in memory only, "
            "NEVER written to disk)."
        )
        print(
            "[identity mint] Remember to pass '-e bootstrap_pve_identity=true' to "
            "the platform-bootstrap command so the bring-up actually runs the mint."
        )

    return opted_in


def prompt_admin_credential() -> dict[str, str]:
    """Prompt the Admin_Bootstrap_Credential rows NO-ECHO, held in memory only.

    Called ONLY when the identity-mint opt-in is taken. Prompts the
    ``"Admin password"`` / ``"Admin token"`` rows (located by logical name) via
    :func:`getpass.getpass` (NO-ECHO), returning them in a local mapping that the
    caller holds in memory for the duration of the process. These values are NEVER
    written to the env file or any file and are NEVER echoed; any summary refers to
    them by logical name only (Req 3.1, 3.2).

    Behaviour (Task 2.3; design Component 2 "Secret handling summary"; Req 3.1,
    3.2):

    * For each of the two Admin_Bootstrap_Credential rows — located in
      ``cluster_env.SCHEMA`` by logical name via
      :data:`ADMIN_CREDENTIAL_LOGICAL_NAMES`, NEVER by position — prompt NO-ECHO
      with :func:`getpass.getpass`.
    * The returned mapping is keyed by the row's **logical name** (``spec.logical``
      — e.g. ``"Admin password"`` / ``"Admin token"``), matching the logical-name
      contract used everywhere else in this module and in any summary line. The
      value is the entered secret.
    * An EMPTY entry means "skip" gracefully — that logical name is simply omitted
      from the returned mapping rather than crashing. The values are NOT validated
      here (they are secrets, not schema-validated).
    * CRITICAL hygiene: the entered values are NEVER printed and NEVER written to
      any file. The caller (``run_wizard``, Task 2.4) holds the returned dict in
      memory only to confirm intent, then discards it; the wizard does NOT actually
      mint (the mint happens at bring-up time from ``-e``/env, as today).
    """
    collected: dict[str, str] = {}

    for logical in ADMIN_CREDENTIAL_LOGICAL_NAMES:
        # Locate the schema row by logical name (never by position) so reordering
        # or adding schema rows cannot silently repoint this at the wrong var.
        spec = next(v for v in cluster_env.SCHEMA if v.logical == logical)
        entered = getpass.getpass(f"{spec.logical} (no-echo): ")
        # Empty => skip gracefully; do not store an empty value.
        if entered:
            collected[spec.logical] = entered

    return collected


def write_env_file(env_file: str, non_secret_vars: dict[str, str]) -> None:
    """Write the NON-secret vars to ``env_file`` as ``KEY=VALUE`` text at 0600.

    Serializes ONLY the non-secret vars (the admin credential is never passed here)
    and writes them to ``env_file`` at mode :data:`ENV_FILE_MODE` (0600) via
    ``os.open(..., 0o600)`` / ``os.chmod`` so the file is owner-only from creation.
    An unwritable path is surfaced as an error naming the path, with nothing
    written (Req 2.2, 3.3; Error Handling table).

    Behaviour (Task 2.2; Error Handling table "``--env-file`` unwritable"):

    * Serializes ``non_secret_vars`` to ``KEY=VALUE\\n`` text, one line per entry,
      in the mapping's iteration order (which follows schema order as produced by
      :func:`prompt_non_secret_vars`).
    * Writes via ``os.open(env_file, O_WRONLY|O_CREAT|O_TRUNC, 0o600)`` so a NEWLY
      created file is owner-only from the moment it exists, then ``os.chmod`` at
      0600 defensively so an already-existing file is tightened too.
    * NEVER receives or writes the ``Admin_Bootstrap_Credential`` — the caller only
      passes the non-secret mapping. (Defence-in-depth: even if an admin alias key
      were mistakenly present, it would be a non-secret PROXMOX_ key; the admin
      values are collected separately in Task 2.3 and never reach this function.)
    * On an unwritable path (``OSError``), re-raises a clear ``OSError`` naming the
      path. Because the ``os.open`` either succeeds (then we write) or fails before
      any content is written, an open failure leaves nothing partially written;
      exit-code handling is the caller's (Task 2.4 / ``main``) concern.
    """
    text = "".join(f"{key}={value}\n" for key, value in non_secret_vars.items())

    try:
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ENV_FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
        finally:
            # Defensive tighten so an EXISTING file (whose mode os.open leaves
            # unchanged) is also brought to 0600. os.fdopen closed the fd already,
            # so chmod by path.
            os.chmod(env_file, ENV_FILE_MODE)
    except OSError as exc:
        raise OSError(
            f"Cluster env file not writable: {env_file} ({exc.strerror})."
        ) from exc


# Logical name (from ``cluster_env.SCHEMA``) of the day-2 terraform@pve service
# token row. This is the ONE mandatory var the wizard tolerates as blank in
# mint-mode, because the bring-up MINTS it (see the mint-mode exception in
# ``validate_and_report``). Located by logical name — confirmed against
# ``cluster_env.SCHEMA`` (``LogicalVar("API token", "PROXMOX_API_TOKEN", ...)``).
_API_TOKEN_LOGICAL_NAME = "API token"


def validate_and_report(env_file: str, *, mint_opted_in: bool) -> None:
    """Re-validate the written file via ``cluster_env`` and report failures.

    Loads + validates ``env_file`` with :func:`cluster_env.load_env_file` +
    :func:`cluster_env.validate`. On a :class:`cluster_env.ValidationError`, the
    single all-problems-together message (``cluster_env.validate`` already collects
    EVERY missing-mandatory + conflicting-alias problem into one message, never
    one-at-a-time) is surfaced and the wizard exits non-zero WITHOUT triggering any
    bring-up (Req 2.4, 2.5; Author_Then_Apply). On success the caller prints a
    summary and EXITS.

    **Mint-mode validation reconciliation (design "Data Models" → "Mint-mode
    validation reconciliation"; Req 2.4, 2.5, 3.5; Properties 1, 4).**
    ``cluster_env.SCHEMA`` marks the ``API token`` row MANDATORY, which is correct
    for the non-mint path (an api-only run needs an existing token). But the live
    ``ansible/roles/platform_preflight/tasks/main.yml`` DELIBERATELY tolerates a
    blank ``PROXMOX_API_TOKEN`` when the host bucket runs first to mint it
    (``-e bootstrap_scope=host,api -e bootstrap_pve_identity=true``): its api-token
    precondition hard-fails ONLY when the token is absent AND the host bucket is
    NOT selected. To keep the wizard's mint-mode happy path consistent with that
    real bring-up behavior WITHOUT changing ``cluster_env``'s contract (whose
    "mandatory" is right for the non-mint path), this function applies a narrow,
    mint-mode-only exception: when ``mint_opted_in`` is True AND the ONLY problem
    the pure detection reports is the ``API token`` mandatory var missing (no other
    missing-mandatory var, no alias conflict), authoring is treated as SUCCESS —
    the token will be MINTED at bring-up by the host bucket's PVE-identity step.
    This mirrors ``platform_preflight``'s own api-token precondition exactly.

    The exception is detected by reusing ``cluster_env``'s PURE detection
    (:func:`cluster_env.load_env_file` + :func:`cluster_env.resolve`, which returns
    ``(resolved, missing, conflicts)``) — the schema and message-building are NOT
    duplicated. Only when that exact single-problem shape holds is authoring
    short-circuited to success; ANY other shape (a second missing var, a conflict,
    or the token missing when ``mint_opted_in`` is False) falls through to the
    normal :func:`cluster_env.validate`, which raises the single all-problems-
    together message and fails closed.

    Exit-code contract (Task 2.4): this function RAISES on failure and returns
    ``None`` on success. It does NOT itself print a non-zero code — ``run_wizard``
    catches the failure and maps it to a non-zero process exit. Concretely:

    * Mint-mode token-only exception (``mint_opted_in`` True, ``conflicts == []``,
      ``missing == ["API token"]``): print a clear note that PROXMOX_API_TOKEN is
      intentionally blank and will be MINTED at bring-up, then return cleanly.
    * On :class:`cluster_env.ValidationError`, print the single all-problems-
      together message to STDERR (``cluster_env.validate`` already collects EVERY
      missing-mandatory + conflicting-alias problem into one message) and re-raise
      so ``run_wizard`` returns non-zero WITHOUT triggering any bring-up (Req 2.4,
      2.5; Author_Then_Apply).
    * On :class:`cluster_env.EnvFileNotFoundError` (should not happen right after a
      successful write, but handled defensively), print the path-naming message to
      STDERR and re-raise — treated as a failure, no bring-up.
    * On success, return cleanly (``None``); ``run_wizard`` then prints the summary
      and exits 0.

    NEVER echoes any secret: it only loads + validates the NON-secret env file the
    wizard just wrote (the Admin_Bootstrap_Credential is never on disk), and the
    ``ValidationError`` message from ``cluster_env`` names variables, not values.
    """
    try:
        env = cluster_env.load_env_file(env_file)

        # Mint-mode token-only exception: reuse cluster_env's PURE detection to
        # check for the exact "only the API token is missing" shape, WITHOUT
        # reimplementing validation or the message. resolve() returns
        # (resolved, missing, conflicts) and never raises.
        if mint_opted_in:
            _resolved, missing, conflicts = cluster_env.resolve(env)
            if not conflicts and missing == [_API_TOKEN_LOGICAL_NAME]:
                print(
                    "[identity mint] PROXMOX_API_TOKEN is intentionally blank — the "
                    "terraform@pve token will be MINTED at bring-up by the host "
                    "bucket's PVE-identity step (mirrors platform_preflight's "
                    "api-token precondition). Authoring OK."
                )
                return

        # Not the mint-mode exception (mint not opted in, another var missing, or a
        # conflict): fall through to the normal all-problems-together validation.
        cluster_env.validate(env)
    except cluster_env.ValidationError as exc:
        print(f"cluster env validation failed: {exc}", file=sys.stderr)
        raise
    except cluster_env.EnvFileNotFoundError as exc:
        print(f"cluster env validation failed: {exc}", file=sys.stderr)
        raise


def run_wizard(env_file: str) -> int:
    """Drive the full author-then-STOP flow for ``env_file``. Returns an exit code.

    Orchestrates the helpers above: seed defaults, prompt non-secret vars, handle
    the identity-mint opt-in (+ NO-ECHO admin credential when taken), write the
    non-secret vars at 0600, then validate-then-STOP with a summary. Performs NO
    cluster mutation and NEVER triggers a bring-up.

    Exit-code contract (Task 2.4): returns a process exit code — ``0`` on success,
    non-zero on a write error or a validation failure. It NEVER invokes
    ``ansible-playbook`` / ``terraform`` / any subprocess and performs NO cluster
    mutation (Property 1). The Admin_Bootstrap_Credential, when collected, is held
    in a LOCAL variable only to confirm mint intent — never written, never echoed,
    discarded when this function returns (Property 3).

    Flow (design Component 2 steps 1-5; Author_Then_Apply STOP):

    1. ``seed_defaults`` — seed prompt defaults from any existing env file.
    2. ``prompt_non_secret_vars`` — collect the NON-secret vars.
    3. ``prompt_identity_mint_opt_in`` — the single opt-in; on YES,
       ``prompt_admin_credential`` (NO-ECHO, in-memory-only). The API token row is
       minted at bring-up when minting is opted in, so a blank token is expected.
    4. ``write_env_file`` — write the non-secret vars at 0600; on ``OSError`` print
       the path error to STDERR and return non-zero (nothing partially written).
    5. ``validate_and_report`` — passing the mint opt-in so a blank API token is
       tolerated ONLY in mint-mode (design "Mint-mode validation reconciliation";
       mirrors ``platform_preflight``'s api-token precondition). On failure return
       non-zero WITHOUT any bring-up.
    6. On success print a summary (KEY names only, never values) and return 0.
    """
    # 1. Seed defaults from any existing file (read-only; empty on first run).
    seed = seed_defaults(env_file)

    # 2. Prompt the non-secret vars (admin rows are skipped inside this helper).
    non_secret = prompt_non_secret_vars(seed)

    # 3. Identity-mint opt-in. On YES, collect the admin credential NO-ECHO into a
    #    LOCAL var used only to confirm intent — NEVER written, NEVER passed to
    #    write_env_file, discarded when this function returns.
    mint = prompt_identity_mint_opt_in()
    admin_credential: dict[str, str] = {}
    if mint:
        admin_credential = prompt_admin_credential()  # noqa: F841 (intentional: never used beyond intent)
        # Per Data Models: when minting, the terraform@pve token is MINTED by the
        # bring-up, so a blank API token here is expected. Any supplied token is
        # left as-is (already collected into `non_secret`); do not force-remove it.
        if not (non_secret.get("PROXMOX_API_TOKEN") or "").strip():
            print(
                "[identity mint] No API token supplied — the terraform@pve token "
                "will be MINTED at bring-up (this is expected when minting)."
            )

    # 4. Write the non-secret vars at 0600. A write failure is fatal and leaves
    #    nothing partially written (write_env_file opens-then-writes atomically).
    try:
        write_env_file(env_file, non_secret)
    except OSError as exc:
        print(f"failed to write cluster env file: {exc}", file=sys.stderr)
        return 1

    # 5. Validate the WRITTEN file, then STOP. On failure (message already printed
    #    to stderr by validate_and_report), return non-zero WITHOUT any bring-up.
    try:
        validate_and_report(env_file, mint_opted_in=mint)
    except (cluster_env.ValidationError, cluster_env.EnvFileNotFoundError):
        return 1

    # 6. Success summary — KEY names only, never values (a written value could be a
    #    service credential like the API token). Then the explicit STOP message.
    written_keys = sorted(non_secret.keys())
    print("\n=== Author step complete ===")
    print(f"Cluster env file written: {env_file}")
    print(
        f"Non-secret variables written ({len(written_keys)}): "
        + (", ".join(written_keys) if written_keys else "(none)")
    )
    if mint:
        print("Identity-mint opted in: yes")
        print(
            "  - Admin bootstrap credential was collected in-memory only and was "
            "NEVER written to disk."
        )
        print(
            "  - Pass '-e bootstrap_pve_identity=true' to the platform-bootstrap "
            "command so the mint runs."
        )
        print(
            "  - The terraform@pve token will be MINTED at bring-up (not authored "
            "here)."
        )
    else:
        print("Identity-mint opted in: no")
    print(
        "\nAuthor step complete — NOTHING was mutated. Next: run the two bring-up "
        "commands (see the README platform-user Quickstart / INSTALL-RUNBOOK)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Thin CLI wrapper over :func:`run_wizard`. Returns a process exit code.

    Parses ``--env-file <path>`` (required) and dispatches to :func:`run_wizard`.
    Structured as a thin wrapper over the helper functions so the offline tests can
    drive it directly (monkeypatching ``builtins.input`` / ``getpass.getpass``).

    Implemented in Task 2.1: arg parsing is trivial and establishes the CLI surface
    (the ``--help`` output and the ``--env-file`` contract) that the docs and
    doctests reference. The dispatch calls :func:`run_wizard`, whose body arrives in
    Tasks 2.2-2.4.
    """
    parser = argparse.ArgumentParser(
        prog="cluster_env_wizard.py",
        description=(
            "Guided, non-cluster-mutating cluster-env authoring wizard. Prompts for "
            "the cluster parameters, writes the non-secret ones to the target env "
            "file (mode 0600), validates the result, and STOPS. It reuses "
            "cluster_env.py's schema + validation and never triggers a bring-up: it "
            "performs no host/API/Terraform mutation and never persists the admin "
            "bootstrap credential."
        ),
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help=(
            "Path to the Cluster_Env_File to author (e.g. cluster.dev.env). If it "
            "already exists, its current values seed the prompt defaults (re-run = "
            "edit, not wipe). Stays gitignored; never committed."
        ),
    )
    args = parser.parse_args(argv)

    return run_wizard(args.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
