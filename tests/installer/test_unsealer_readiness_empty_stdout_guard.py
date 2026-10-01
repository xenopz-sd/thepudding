"""Offline regression guard — the unsealer-bootstrap readiness/JSON-probe
expressions must be EMPTY-STDOUT SAFE.

Feature: svc07-unsealer-bootstrap-delegation (bug surfaced during the
terraform-ansible-handoff live run, 2026-09-30).

THE BUG
-------
The readiness poll in ``openbao_init_unseal/tasks/unsealer_bootstrap.yml`` tested
the first character of ``bao status`` stdout with the Jinja idiom::

    (some.stdout | default('') | trim | first) in ['{', '[']

``| first`` on an EMPTY string raises ``No first item, sequence was empty``. A
just-starting / sealed / uninitialised backend legitimately returns empty stdout
on ``bao status`` — exactly the transient the ``until:`` poll is meant to RETRY
through. Instead the empty-stdout case raised a hard Jinja exception INSIDE the
``until`` expression, which aborts the task immediately rather than retrying. The
live from-scratch handoff run failed at::

    Unsealer bootstrap — readiness wait: bao status answers before init (Req 1.1)
    An 'until' expression failed: ... No first item, sequence was empty.

THE FIX
-------
Replace ``| trim | first`` with the empty-safe slice ``(... | trim)[:1]``: a
slice of an empty string is ``''`` (no exception), and ``'' in ['{', '[']`` is
cleanly ``False`` -> retry / named-timeout, never a crash. Non-empty JSON still
matches on its first character, so behaviour is preserved for every real body.

WHY THIS GUARD
--------------
Pure text parse of the committed role file — no infrastructure. The authoritative
behavioural acceptance is the ``requires-infra`` from-scratch bring-up; this guard
pins the STRUCTURE so the fragile ``| first`` idiom cannot silently return to the
readiness/JSON-probe expressions in this file.
"""

from __future__ import annotations

import re
from pathlib import Path

# tests/installer/<this> -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]
_UNSEALER_BOOTSTRAP = (
    _REPO_ROOT
    / "ansible"
    / "roles"
    / "openbao_init_unseal"
    / "tasks"
    / "unsealer_bootstrap.yml"
)

# The fragile idiom: ``... | trim | first`` (any whitespace between filters).
_FRAGILE_FIRST = re.compile(r"\|\s*trim\s*\|\s*first\b")
# The empty-safe replacement: ``... | trim)[:1]``.
_SAFE_SLICE = re.compile(r"\|\s*trim\s*\)\s*\[\s*:\s*1\s*\]")


def _text() -> str:
    return _UNSEALER_BOOTSTRAP.read_text(encoding="utf-8")


def test_unsealer_bootstrap_file_exists():
    assert _UNSEALER_BOOTSTRAP.is_file(), (
        f"expected the unsealer bootstrap task file at {_UNSEALER_BOOTSTRAP}"
    )


def test_no_fragile_trim_first_idiom_remains():
    """No ``| trim | first`` may survive — it crashes on empty stdout."""
    offenders = [
        (i, line.strip())
        for i, line in enumerate(_text().splitlines(), start=1)
        if _FRAGILE_FIRST.search(line)
    ]
    assert not offenders, (
        "The empty-stdout-unsafe `| trim | first` idiom must not appear in "
        "unsealer_bootstrap.yml (it raises 'No first item, sequence was empty' "
        "on a transient empty `bao status`, aborting the until-poll instead of "
        "retrying). Use the empty-safe slice `(... | trim)[:1]` instead. "
        f"Offending lines: {offenders}"
    )


def test_empty_safe_slice_is_used_for_the_readiness_poll():
    """The readiness poll must use the empty-safe slice form on its stdout."""
    text = _text()
    assert _SAFE_SLICE.search(text), (
        "Expected the empty-safe `(... | trim)[:1]` slice form to guard the "
        "first-character JSON probe(s) in unsealer_bootstrap.yml."
    )
    # Specifically pin the readiness `until:` line — the exact expression the
    # live run crashed on.
    readiness_until = [
        line.strip()
        for line in text.splitlines()
        if "openbao_unsealer_readiness.stdout" in line and "until:" in line
    ]
    assert readiness_until, (
        "Expected an `until:` readiness poll referencing "
        "openbao_unsealer_readiness.stdout in unsealer_bootstrap.yml."
    )
    for line in readiness_until:
        assert _SAFE_SLICE.search(line) and not _FRAGILE_FIRST.search(line), (
            f"Readiness until-poll must use the empty-safe slice: {line!r}"
        )
