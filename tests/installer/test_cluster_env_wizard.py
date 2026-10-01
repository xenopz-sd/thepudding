"""Offline behavioral tests for the guided cluster-env authoring wizard.

Spec: .kiro/specs/platform-install-simplification/ (requirements.md, design.md,
tasks.md). Implements SPEC TASK 3 — the offline test file pinning the design's
Correctness Properties P1-P4 for ``scripts/installer/cluster_env_wizard.py``.

WHAT THIS PINS — the wizard is the ``Guided_Env_Fill`` config-authoring step: it
prompts for cluster parameters, writes the NON-secret ones to a gitignored env
file at 0600, validates the result via ``cluster_env``, and STOPS. It never
mutates the cluster. Generative PBT does NOT apply (the wizard is an IO/prompt-
sequencing tool, not a pure input->output invariant); exactly as
``platform-clean-slate`` did, the properties are pinned by example-based
behavioral tests driving the wizard with a scripted ``input`` sequence
(monkeypatched ``builtins.input`` + ``getpass.getpass``) over a ``tmp_path`` env
file:

  * P1 (author-then-apply / no mutation) — the wizard never imports/spawns a
    subprocess and never invokes ``ansible-playbook``/``terraform``; ``run_wizard``
    returns after ``validate`` without any bring-up.
  * P2 (schema reuse, no duplication; non-interactive path first-class) — the
    wizard drives ``cluster_env.SCHEMA`` (every non-admin logical name is prompted;
    no divergent hardcoded schema), and ``cluster_env`` validation on a
    hand-written complete file passes with no wizard involved.
  * P3 (Admin_Bootstrap_Credential never persisted) — with identity-mint opted in
    and admin values entered via patched ``getpass``, the written file contains
    NEITHER the admin alias keys NOR the sentinel value, and the sentinel never
    reaches stdout/stderr either.
  * P4 (non-secret authoring complete + validated + 0600, or fail-closed) — a
    happy-path run writes the supplied non-secret KEY=VALUEs at mode 0o600 and
    ``cluster_env.validate`` passes; an incomplete run reports ALL missing vars in
    one message and returns non-zero; the mint-mode token-only exception is pinned.

HOW IT STAYS OFFLINE — it imports ``cluster_env_wizard`` + ``cluster_env`` via the
``tests/installer/conftest.py`` idiom (which puts ``scripts/installer/`` on
``sys.path``), drives them with monkeypatched ``input``/``getpass``, and writes
only into ``tmp_path``. No ``ansible-playbook`` binary, no live Proxmox; always
runs, no ``requires-infra`` gate. It never embeds or prints a real secret.

Validates: Requirements 6.2, 6.3 / Properties 1, 2, 3, 4.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Resolved onto sys.path by tests/installer/conftest.py (scripts/installer/).
import cluster_env
import cluster_env_wizard


# A distinctive admin secret sentinel used across the P3 tests. If this value
# EVER appears in the written env file or in stdout/stderr, the admin-credential-
# never-persisted invariant is broken.
_ADMIN_SENTINEL = "SENTINEL_ADMIN_SECRET"


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #


def _scripted_input(answers):
    """Return an ``input``-compatible callable yielding successive ``answers``.

    Each call pops the next scripted answer (ignoring the prompt string). Running
    out of scripted answers is a test bug, surfaced as an explicit assertion.
    """
    queue = list(answers)

    def fake_input(prompt=""):  # noqa: ARG001 (prompt intentionally ignored)
        assert queue, "wizard asked for more input than the test scripted"
        return queue.pop(0)

    return fake_input


def _scripted_getpass(secrets):
    """Return a ``getpass.getpass``-compatible callable yielding ``secrets``.

    Used to feed the two Admin_Bootstrap_Credential rows without echoing.
    """
    queue = list(secrets)

    def fake_getpass(prompt=""):  # noqa: ARG001
        assert queue, "wizard asked for more no-echo input than the test scripted"
        return queue.pop(0)

    return fake_getpass


# The complete set of mandatory non-admin logical names, resolved from the
# schema itself (not hardcoded) so this test tracks the schema.
def _mandatory_logical_names():
    return [s.logical for s in cluster_env.SCHEMA if s.mandatory]


def _nonadmin_logical_names():
    admin = set(cluster_env_wizard.ADMIN_CREDENTIAL_LOGICAL_NAMES)
    return [s.logical for s in cluster_env.SCHEMA if s.logical not in admin]


# A full, valid answer set for the mandatory non-admin rows, keyed by logical
# name. Non-mandatory rows are answered empty (skipped). Built so a happy-path
# run produces a file that passes cluster_env.validate().
_VALID_ANSWERS = {
    "Proxmox endpoint": "https://pve.example.com:8006/",
    "API token": "root@pam!tf=deadbeef-0000-0000-0000-000000000000",
    "Node name": "pve-node-1",
    "LXC template": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
}


def _answers_for(overrides=None, *, mint="n"):
    """Build the scripted ``input`` list for a full non-secret prompt pass.

    Walks the non-admin schema rows in order (the exact order the wizard prompts
    them), supplying ``_VALID_ANSWERS`` for mandatory rows and blank for the rest.
    ``overrides`` (logical name -> answer) lets a test blank/override specific
    rows. The final element is the identity-mint opt-in answer.
    """
    overrides = overrides or {}
    answers = []
    for spec in cluster_env.SCHEMA:
        if spec.logical in cluster_env_wizard.ADMIN_CREDENTIAL_LOGICAL_NAMES:
            continue  # admin rows are getpass, not input
        if spec.logical in overrides:
            answers.append(overrides[spec.logical])
        elif spec.logical in _VALID_ANSWERS:
            answers.append(_VALID_ANSWERS[spec.logical])
        else:
            answers.append("")  # optional row -> leave unset
    answers.append(mint)  # the single identity-mint opt-in question
    return answers


# --------------------------------------------------------------------------- #
# Property 1 — author-then-apply: the wizard never mutates the cluster           #
# --------------------------------------------------------------------------- #


def test_wizard_module_never_shells_out():
    """P1: the wizard module imports no subprocess and invokes no bring-up.

    Validates: Requirements 4.1, 4.3, 2.4 / Property 1.
    """
    # No subprocess machinery is bound on the module (it must never spawn a
    # process). The bare-word "subprocess" appears legitimately in PROSE docstrings
    # ("spawns NO subprocess", "NEVER invokes ansible-playbook / terraform"), so
    # instead of a fragile whole-file text scan we inspect the AST for EXECUTABLE
    # invocations: no import of a process/shell module, and no call to one.
    assert not hasattr(cluster_env_wizard, "subprocess")

    module = ast.parse(Path(cluster_env_wizard.__file__).read_text(encoding="utf-8"))

    forbidden_imports = {"subprocess", "shlex", "pty"}
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    leaked = forbidden_imports & imported
    assert not leaked, f"wizard must not import process/shell modules: {sorted(leaked)}"

    # No os.system / os.popen call (os IS imported for os.open/os.chmod, so scope
    # the ban to the process-spawning attributes, not the module).
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("system", "popen", "execv", "execvp", "spawnv"):
                pytest.fail(f"wizard must not call os.{node.func.attr} (non-mutating)")


def test_run_wizard_returns_after_validate_without_bringup(tmp_path, monkeypatch, capsys):
    """P1: a happy-path run returns 0 having only authored/validated the file.

    Validates: Requirements 2.4, 4.3 / Property 1.
    """
    env_file = tmp_path / "cluster.dev.env"
    monkeypatch.setattr("builtins.input", _scripted_input(_answers_for(mint="n")))
    # No getpass should be needed (mint=n); patch it to blow up if called.
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a, **k: pytest.fail("getpass called when mint not opted in"),
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))

    assert rc == 0
    assert env_file.exists()
    out = capsys.readouterr().out
    # The wizard explicitly announces it mutated nothing.
    assert "NOTHING was mutated" in out


# --------------------------------------------------------------------------- #
# Property 2 — schema reuse (no duplication) + non-interactive path first-class  #
# --------------------------------------------------------------------------- #


def test_wizard_references_cluster_env_schema_not_a_local_one():
    """P2: the wizard drives cluster_env.SCHEMA, not a hardcoded second schema.

    Validates: Requirement 2.1 / Property 2.
    """
    # The module object it uses for the schema is the shared cluster_env module.
    assert cluster_env_wizard.cluster_env is cluster_env
    # And it does not define its own SCHEMA tuple of LogicalVar rows.
    assert not hasattr(cluster_env_wizard, "SCHEMA")

    src = Path(cluster_env_wizard.__file__).read_text(encoding="utf-8")
    assert "cluster_env.SCHEMA" in src


def test_prompt_non_secret_vars_prompts_every_nonadmin_logical_name(monkeypatch):
    """P2: every non-admin cluster_env.SCHEMA logical name appears in a prompt.

    Bites if a divergent hardcoded schema (missing or renamed rows) appears.

    Validates: Requirement 2.1 / Property 2.
    """
    seen_prompts = []

    def capturing_input(prompt=""):
        seen_prompts.append(prompt)
        return ""  # answer everything empty; we only care about the prompt text

    monkeypatch.setattr("builtins.input", capturing_input)

    cluster_env_wizard.prompt_non_secret_vars({})

    joined = "\n".join(seen_prompts)
    for logical in _nonadmin_logical_names():
        assert logical in joined, f"non-admin schema row {logical!r} was not prompted"

    # And the admin rows must NOT be prompted via input() here (they are getpass).
    for admin in cluster_env_wizard.ADMIN_CREDENTIAL_LOGICAL_NAMES:
        assert admin not in joined, f"admin row {admin!r} must not be an input() prompt"


def test_non_interactive_path_validates_without_the_wizard(tmp_path):
    """P2: a hand-written complete file passes cluster_env validation, no wizard.

    Validates: Requirements 2.6, 4.2 / Property 2.
    """
    env_file = tmp_path / "cluster.hand.env"
    env_file.write_text(
        "\n".join(
            [
                "PROXMOX_ENDPOINT=https://pve.example.com:8006/",
                "PROXMOX_API_TOKEN=root@pam!tf=deadbeef",
                "PROXMOX_NODE_NAME=pve-node-1",
                "PROXMOX_LXC_TEMPLATE=local:vztmpl/debian-12-standard.tar.zst",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # cluster_env's own validate on the hand-written file passes — the wizard's
    # existence does not change the file-driven path.
    resolved = cluster_env.validate(cluster_env.load_env_file(str(env_file)))
    assert resolved["TF_VAR_proxmox_endpoint"] == "https://pve.example.com:8006/"


# --------------------------------------------------------------------------- #
# Property 3 — the Admin_Bootstrap_Credential is never persisted                 #
# --------------------------------------------------------------------------- #


def test_admin_credential_never_persisted_or_echoed(tmp_path, monkeypatch, capsys):
    """P3: mint opted in + admin values entered -> neither keys nor value on disk.

    The written file must contain NEITHER the admin alias keys NOR the sentinel
    admin value; the sentinel must not appear in stdout/stderr either.

    Validates: Requirements 3.1, 3.2, 3.3 / Property 3.
    """
    env_file = tmp_path / "cluster.dev.env"

    # mint=y; the API token row is left blank (will be minted). All other
    # mandatory non-admin rows supplied so validation succeeds via the mint-mode
    # token-only exception.
    answers = _answers_for(overrides={"API token": ""}, mint="y")
    monkeypatch.setattr("builtins.input", _scripted_input(answers))
    monkeypatch.setattr(
        "getpass.getpass", _scripted_getpass([_ADMIN_SENTINEL, _ADMIN_SENTINEL])
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc == 0

    file_text = env_file.read_text(encoding="utf-8")
    # Admin alias keys must be absent from the written file.
    assert "PROXMOX_ADMIN_PASSWORD" not in file_text
    assert "PROXMOX_ADMIN_TOKEN" not in file_text
    assert "TF_VAR_proxmox_admin_password" not in file_text
    assert "TF_VAR_proxmox_admin_token" not in file_text
    # The sentinel admin value must never be written.
    assert _ADMIN_SENTINEL not in file_text

    # And it must never be echoed to stdout/stderr.
    captured = capsys.readouterr()
    assert _ADMIN_SENTINEL not in captured.out
    assert _ADMIN_SENTINEL not in captured.err


def test_admin_credential_not_collected_when_mint_declined(tmp_path, monkeypatch):
    """P3: when mint is NOT opted in, the admin rows are never prompted.

    Validates: Requirement 3.4 / Property 3.
    """
    env_file = tmp_path / "cluster.dev.env"
    monkeypatch.setattr("builtins.input", _scripted_input(_answers_for(mint="n")))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a, **k: pytest.fail("admin credential prompted when mint declined"),
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc == 0


# --------------------------------------------------------------------------- #
# Property 4 — non-secret authoring complete + validated + 0600, or fail-closed  #
# --------------------------------------------------------------------------- #


def test_happy_path_writes_nonsecret_vars_valid_and_0600(tmp_path, monkeypatch):
    """P4: happy path (mint=n) -> rc 0, file 0o600, values written, validate passes.

    Validates: Requirements 2.2, 2.5, 3.6 / Property 4.
    """
    env_file = tmp_path / "cluster.dev.env"
    monkeypatch.setattr("builtins.input", _scripted_input(_answers_for(mint="n")))
    monkeypatch.setattr(
        "getpass.getpass",
        lambda *a, **k: pytest.fail("getpass called on mint=n happy path"),
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc == 0

    # File exists at mode 0o600.
    assert env_file.exists()
    mode = env_file.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    # The supplied non-secret KEY=VALUEs are present.
    file_text = env_file.read_text(encoding="utf-8")
    assert "PROXMOX_ENDPOINT=https://pve.example.com:8006/" in file_text
    assert "PROXMOX_NODE_NAME=pve-node-1" in file_text
    assert f"PROXMOX_API_TOKEN={_VALID_ANSWERS['API token']}" in file_text

    # And cluster_env.validate passes on what was written.
    resolved = cluster_env.validate(cluster_env.load_env_file(str(env_file)))
    assert resolved["TF_VAR_proxmox_node_name"] == "pve-node-1"


def test_incomplete_run_reports_all_missing_vars_together(tmp_path, monkeypatch, capsys):
    """P4: omitting two mandatory vars -> rc != 0, one message naming BOTH.

    Validates: Requirements 2.5, 6.3 / Property 4.
    """
    env_file = tmp_path / "cluster.dev.env"
    # Omit Node name AND LXC template; mint=n (normal fail-closed).
    answers = _answers_for(
        overrides={"Node name": "", "LXC template": ""}, mint="n"
    )
    monkeypatch.setattr("builtins.input", _scripted_input(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "")

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc != 0

    err = capsys.readouterr().err
    # Both missing vars named in the SAME message (all-problems-together).
    assert "Node name" in err
    assert "LXC template" in err


# --------------------------------------------------------------------------- #
# Mint-mode token-only exception (Data Models reconciliation)                    #
# --------------------------------------------------------------------------- #


def test_mint_mode_tolerates_blank_api_token(tmp_path, monkeypatch):
    """Mint=y with only the API token blank -> rc 0, token NOT written.

    The token will be minted at bring-up, mirroring platform_preflight.

    Validates: Requirements 2.4, 3.5 / Properties 1, 4.
    """
    env_file = tmp_path / "cluster.dev.env"
    answers = _answers_for(overrides={"API token": ""}, mint="y")
    monkeypatch.setattr("builtins.input", _scripted_input(answers))
    monkeypatch.setattr(
        "getpass.getpass", _scripted_getpass([_ADMIN_SENTINEL, _ADMIN_SENTINEL])
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc == 0

    file_text = env_file.read_text(encoding="utf-8")
    assert "PROXMOX_API_TOKEN" not in file_text


def test_mint_mode_still_fails_on_other_missing_var(tmp_path, monkeypatch, capsys):
    """Mint=y with API token blank AND Node name missing -> rc != 0 naming Node name.

    The token-only exception must NOT swallow other missing mandatory vars.

    Validates: Requirements 2.5, 3.5 / Property 4.
    """
    env_file = tmp_path / "cluster.dev.env"
    answers = _answers_for(
        overrides={"API token": "", "Node name": ""}, mint="y"
    )
    monkeypatch.setattr("builtins.input", _scripted_input(answers))
    monkeypatch.setattr(
        "getpass.getpass", _scripted_getpass([_ADMIN_SENTINEL, _ADMIN_SENTINEL])
    )

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc != 0

    err = capsys.readouterr().err
    assert "Node name" in err


def test_non_mint_run_fails_closed_on_blank_api_token(tmp_path, monkeypatch, capsys):
    """Mint=n with API token blank -> rc != 0 (normal fail-closed, no exception).

    Validates: Requirements 2.5 / Property 4.
    """
    env_file = tmp_path / "cluster.dev.env"
    answers = _answers_for(overrides={"API token": ""}, mint="n")
    monkeypatch.setattr("builtins.input", _scripted_input(answers))
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "")

    rc = cluster_env_wizard.run_wizard(str(env_file))
    assert rc != 0

    err = capsys.readouterr().err
    assert "API token" in err
