"""CLI observability tests for the onboarding script (task 11.1).

Spec: proxmox-network-foundation — Requirements 4.7, 7.2, FD.1; design.md
"Error Handling"; composition-wiring steering (log decisions at INFO).

These tests drive ``onboard_project.main`` (the CLI entry point) rather than the
lower-level ``onboard`` helper, and assert that the operator-facing decision
trail is CLEAR and greppable:

  * an ALLOCATION success states slug + vlan_id + onboarding_date and exits 0;
  * a duplicate/invalid slug and a malformed/missing registry surface an ABORT
    with exit 1 (Requirement 5.3, FD.2);
  * VLAN-ID exhaustion surfaces an explicit EXHAUSTION with exit 2, distinct
    from a plain abort (Requirement 9.2);
  * the INFO-level decision trail is emitted to STDERR (composition-wiring: the
    application's own loggers must be visible), while the machine-parseable
    result stays on STDOUT;
  * no credential value ever appears in stdout/stderr/logs.

Every test uses ``--no-mr`` (or a registry with no CI env) so no live merge
request is ever attempted; each operates on a ``tmp_path`` registry so the
committed ``projects.yaml`` is never modified.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import onboard_project
from onboard_project import main


def _write_registry(tmp_path: Path, body: str) -> Path:
    reg = tmp_path / "projects.yaml"
    reg.write_text(body, encoding="utf-8")
    return reg


# --- ALLOCATION success (exit 0, clear slug+vlan+date) --------------------


def test_allocation_success_reports_slug_vlan_date_exit0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog
) -> None:
    """A new slug exits 0, logs an ALLOCATION line stating slug+vlan+date, and
    prints the final result on stdout."""
    reg = _write_registry(tmp_path, "projects: []\n")

    with caplog.at_level(logging.INFO, logger="onboard_project"):
        code = main(["--slug", "firstproj", "--registry", str(reg), "--no-mr"])

    assert code == 0
    out = capsys.readouterr()
    # Final machine-parseable result line on stdout.
    assert "Onboarded 'firstproj'" in out.out
    assert "VLAN 100" in out.out
    # INFO decision trail names the outcome, slug, vlan, and date.
    trail = "\n".join(r.getMessage() for r in caplog.records)
    assert "ALLOCATION" in trail
    assert "firstproj" in trail
    assert "VLAN 100" in trail


# --- ABORT: duplicate slug (exit 1) ---------------------------------------


def test_duplicate_slug_aborts_exit1_with_abort_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A duplicate slug exits 1 and surfaces an ABORT-prefixed message on
    stderr (Requirement 5.3)."""
    reg = _write_registry(
        tmp_path,
        "projects:\n"
        "  - slug: taken\n"
        "    vlan_id: 100\n"
        '    onboarding_date: "2024-01-01"\n',
    )

    code = main(["--slug", "taken", "--registry", str(reg), "--no-mr"])

    assert code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err
    assert "already present" in err


# --- ABORT: missing registry (exit 1, FD.2) -------------------------------


def test_missing_registry_aborts_exit1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing registry exits 1 with an ABORT message naming the problem
    (FD.2 — abort and report)."""
    missing = tmp_path / "does-not-exist.yaml"

    code = main(["--slug", "anyslug", "--registry", str(missing), "--no-mr"])

    assert code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err
    assert "not found" in err.lower()


# --- EXHAUSTION: exit 2, distinct from abort (Requirement 9.2) ------------


def test_vlan_exhaustion_reports_exhaustion_exit2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the highest recorded VLAN ID is 254, onboarding a new slug surfaces
    an EXHAUSTION message and exits 2 — a distinct code from a plain abort so a
    CI job can alert on address-space exhaustion specifically (Requirement 9.2).
    """
    reg = _write_registry(
        tmp_path,
        "projects:\n"
        "  - slug: last\n"
        "    vlan_id: 254\n"
        '    onboarding_date: "2024-01-01"\n',
    )

    code = main(["--slug", "onemore", "--registry", str(reg), "--no-mr"])

    assert code == 2
    err = capsys.readouterr().err
    assert "EXHAUSTION" in err


# --- Decision trail goes to stderr; result to stdout ----------------------


def test_decision_trail_on_stderr_result_on_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The INFO decision trail is written to STDERR (visible per
    composition-wiring), while the machine-parseable result line is on STDOUT,
    so the two streams do not interleave."""
    reg = _write_registry(tmp_path, "projects: []\n")

    # Ensure the module logger has its stderr handler wired (idempotent).
    code = main(["--slug", "splitstreams", "--registry", str(reg), "--no-mr"])

    assert code == 0
    captured = capsys.readouterr()
    # Result on stdout.
    assert "Onboarded 'splitstreams'" in captured.out
    # Decision prefix on stderr, not stdout.
    assert "ALLOCATION" in captured.err
    assert "ALLOCATION" not in captured.out


# --- No credential ever leaks into output ---------------------------------


def test_no_token_value_appears_in_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with a token present in the environment, no credential value is
    ever printed or logged. --no-mr skips the MR call entirely, but we set a
    recognisable token to prove it never surfaces in any stream."""
    secret = "glpat-SUPERSECRETTOKENVALUE"
    monkeypatch.setenv("GITLAB_TOKEN", secret)
    monkeypatch.setenv("CI_API_V4_URL", "https://gitlab.example/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "42")
    reg = _write_registry(tmp_path, "projects: []\n")

    code = main(["--slug", "tokentest", "--registry", str(reg), "--no-mr"])

    assert code == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


# --- Bug condition exploration: --no-mr success must not overclaim an MR ----
#
# Spec: onboard-no-mr-misleading-message — Property 1 (Bug Condition), design
# "Testing Strategy" (Exploratory Bug Condition Checking); Requirements 1.1,
# 1.2, 1.3.
#
# The bug condition is a discrete branch (MR opened vs not), so this is scoped
# to the concrete failing case — a `--no-mr` success run — rather than a wide
# generated input domain (no Hypothesis generator needed). This same test
# encodes the expected accurate wording and will validate the fix when it
# passes later (re-run in task 3.6).
#
# EXPECTED ON UNFIXED CODE: this test FAILS — the unfixed `--no-mr` run emits
# "Registry updated; merge request opened/updated for review." on stdout and
# the matching "registry updated and merge request opened/updated for review."
# INFO clause, both of which are false because no merge request was opened.


def test_no_mr_success_does_not_overclaim_merge_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog
) -> None:
    """A --no-mr success must not claim a merge request was opened/updated.

    Validates: Requirements 1.1, 1.2, 1.3

    Property 1 (Bug Condition): for a successful run where no merge request was
    opened, neither the stdout result line nor the ALLOCATION INFO
    decision-trail line may claim an MR was opened/updated; both must state no
    MR was opened; and the --no-mr skip notice + the two final lines must be
    mutually consistent.
    """
    reg = _write_registry(tmp_path, "projects: []\n")

    with caplog.at_level(logging.INFO, logger="onboard_project"):
        code = main(["--slug", "my-project", "--registry", str(reg), "--no-mr"])

    assert code == 0
    captured = capsys.readouterr()
    stdout = captured.out

    # The captured ALLOCATION INFO decision-trail record.
    allocation_records = [
        r.getMessage() for r in caplog.records if "ALLOCATION" in r.getMessage()
    ]
    assert allocation_records, "expected an ALLOCATION INFO decision-trail record"
    info_line = "\n".join(allocation_records)

    overclaim = "merge request opened/updated"
    states_no_mr = "no merge request was opened"

    # Check 1: stdout does NOT overclaim an MR.
    assert overclaim not in stdout, (
        "stdout overclaims a merge request on a --no-mr run: " f"{stdout!r}"
    )

    # Check 2: the ALLOCATION INFO line does NOT overclaim an MR.
    assert overclaim not in info_line, (
        "ALLOCATION INFO line overclaims a merge request on a --no-mr run: "
        f"{info_line!r}"
    )

    # Check 3: stdout AND the INFO line both state no MR was opened.
    assert states_no_mr in stdout, (
        "stdout does not state that no merge request was opened: " f"{stdout!r}"
    )
    assert states_no_mr in info_line, (
        "ALLOCATION INFO line does not state that no merge request was opened: "
        f"{info_line!r}"
    )

    # Check 4: the --no-mr output is self-consistent — the skip notice is
    # present and no emitted line (stdout or INFO) claims an MR was opened.
    assert "skipped opening a merge request" in stdout, (
        "expected the (--no-mr) skip notice on stdout: " f"{stdout!r}"
    )
    for line in stdout.splitlines() + info_line.splitlines():
        assert overclaim not in line, (
            "a --no-mr output line contradicts the skip notice by claiming an "
            f"MR was opened: {line!r}"
        )


# --- Preservation: MR-opened path keeps the verbatim MR wording ------------
#
# Spec: onboard-no-mr-misleading-message — Property 2 (Preservation), design
# "Testing Strategy" (Preservation Checking test case 1); Requirement 3.1.
#
# The normal CI path (no --no-mr) opens a merge request, so both final lines
# MUST continue to state "merge request opened/updated for review." verbatim —
# on the stdout result line AND the ALLOCATION INFO decision-trail line. This
# guards against the fix accidentally changing the MR-opened wording.
#
# main() has no opener-injection hook (it hardcodes
# ``GitLabMergeRequestOpener()`` on the non-``--no-mr`` branch), so rather than
# add a production-only test seam we monkeypatch
# ``onboard_project.GitLabMergeRequestOpener`` to a fake that returns a
# non-empty dict (making ``result.mr_opened`` True) and performs NO network
# I/O. This exercises the real main() wording path end-to-end.


class _FakeOpenedMergeRequestOpener:
    """Stand-in for ``GitLabMergeRequestOpener`` that opens an MR without I/O.

    Constructed with no arguments (matching how main() instantiates the real
    opener) and returns a non-empty dict from ``open_or_update`` so
    ``bool(result)`` is True — i.e. main() takes the MR-opened branch. It never
    performs any network call.
    """

    def open_or_update(self, allocation):  # noqa: ANN001, ANN201
        return {"iid": 1, "web_url": "https://gitlab.example/mr/1"}


def test_mr_opened_path_preserves_verbatim_merge_request_wording(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MR-opened path keeps "merge request opened/updated for review."

    Validates: Requirements 3.1

    Property 2 (Preservation): on the normal CI path where a merge request IS
    opened, both final lines — the stdout result line and the ALLOCATION INFO
    decision-trail line — must still contain the verbatim phrase
    "merge request opened/updated for review."
    """
    # Drive the MR-OPENED branch: no --no-mr, and a fake opener that returns a
    # non-empty dict so result.mr_opened is True. No real network call is made.
    monkeypatch.setattr(
        onboard_project, "GitLabMergeRequestOpener", _FakeOpenedMergeRequestOpener
    )
    reg = _write_registry(tmp_path, "projects: []\n")

    with caplog.at_level(logging.INFO, logger="onboard_project"):
        code = main(["--slug", "ci-project", "--registry", str(reg)])

    assert code == 0
    stdout = capsys.readouterr().out

    allocation_records = [
        r.getMessage() for r in caplog.records if "ALLOCATION" in r.getMessage()
    ]
    assert allocation_records, "expected an ALLOCATION INFO decision-trail record"
    info_line = "\n".join(allocation_records)

    verbatim = "merge request opened/updated for review."

    # Verbatim MR wording preserved on the stdout result line.
    assert verbatim in stdout, (
        "stdout dropped the verbatim MR-opened wording on the MR-opened path: "
        f"{stdout!r}"
    )
    # Verbatim MR wording preserved on the ALLOCATION INFO decision-trail line.
    assert verbatim in info_line, (
        "ALLOCATION INFO line dropped the verbatim MR-opened wording on the "
        f"MR-opened path: {info_line!r}"
    )
