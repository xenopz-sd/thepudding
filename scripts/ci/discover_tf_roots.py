#!/usr/bin/env python3
"""Discover every Terraform_Root under ``infra/`` for the CI validate gate.

Feature: ci-offline-gate-hardening (design.md §Architecture 3.1, §Components 4.1).

WHY THIS EXISTS. The ``terraform:fmt-validate`` job in ``.gitlab-ci.yml`` used to
``terraform validate`` a HAND-MAINTAINED hardcoded list of roots. That list rots:
roots added later (``svc-17-18-observability`` in v0.10.0; ``dronefleet`` +
``dronefleet/db`` in v0.11.0) were silently never ``validate``-checked. This helper
replaces the list — it ENUMERATES every Terraform_Root under ``infra/`` by the one
marker that already distinguishes a root from a module in this repo, so a new root
is validated automatically with NO ``.gitlab-ci.yml`` edit.

THE MARKER (root vs module). A directory is a Terraform_Root iff it has ``*.tf``
files AND one of them declares, inside a top-level ``terraform { ... }`` block, a
``backend "<type>" {`` sub-block. A directory with ``*.tf`` files but no such
backend sub-block is a Terraform_Module (state lives in the calling root) and is
EXCLUDED. The sole module today, ``infra/modules/proxmox-compute``, deliberately
declares no backend block.

ROBUSTNESS. This is a lightweight lexical scan, NOT an HCL parser (pure stdlib, no
``python-hcl2`` dependency). Line comments (``#``, ``//``) and ``/* ... */`` block
comments are stripped before matching, so the literal word "backend" in a comment
(the ``versions.tf`` files carry explanatory comments mentioning backends) can
never false-positive. The match requires the ``backend "`` STANZA OPENER inside a
``terraform {`` block, which a module does not have.

The gitignored ``local_backend_override.tf`` (``backend "local" {}``) is present in
a developer tree but ABSENT in CI; classification keys off the committed
``versions.tf`` marker only, so the root set is identical in both.

NESTING. ``infra/projects/dronefleet`` and ``infra/projects/dronefleet/db`` are
BOTH roots; the walk classifies every directory independently and never prunes a
subtree on finding a root.

OUTPUT. Prints each discovered root, repo-relative with POSIX separators, one per
line, STABLE-SORTED, to stdout — the machine-consumable contract the CI loop reads.
FAIL-LOUD: exits non-zero with a stderr diagnostic if ZERO roots are found (a
broken marker or a run from the wrong CWD must never report a false green).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A ``backend "<type>" {`` stanza opener (the quoted type + an opening brace,
# possibly on the next line). Matched ONLY while inside a ``terraform { ... }``
# block (see _file_has_backend_block).
_BACKEND_OPENER = re.compile(r'\bbackend\s+"[^"]+"\s*\{', re.MULTILINE)
_TERRAFORM_OPENER = re.compile(r'\bterraform\s*\{')


def _strip_comments(text: str) -> str:
    """Remove ``#``/``//`` line comments and ``/* ... */`` block comments.

    So the literal word "backend" in a comment can never be matched as a stanza.
    Done before any structural scan; good enough for the single-top-level-block
    convention every ``versions.tf`` in this repo follows.
    """
    # Block comments first (non-greedy, across lines).
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    # Then line comments: strip from a '#' or '//' to end-of-line.
    out_lines = []
    for line in text.splitlines():
        # '#' line comment.
        hash_idx = line.find("#")
        # '//' line comment.
        slash_idx = line.find("//")
        cut = len(line)
        if hash_idx != -1:
            cut = min(cut, hash_idx)
        if slash_idx != -1:
            cut = min(cut, slash_idx)
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def _file_has_backend_block(tf_text: str) -> bool:
    """True iff a ``backend "..." {`` opener sits inside a ``terraform { ... }``
    block in ``tf_text`` (comments already stripped by the caller)."""
    for tmatch in _TERRAFORM_OPENER.finditer(tf_text):
        # Walk braces from the terraform block's opening '{' to find its matching
        # close, tracking the span of this terraform block.
        start = tmatch.end() - 1  # index of the '{'
        depth = 0
        i = start
        n = len(tf_text)
        block_end = None
        while i < n:
            c = tf_text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    block_end = i
                    break
            i += 1
        block_text = tf_text[start : (block_end + 1 if block_end is not None else n)]
        if _BACKEND_OPENER.search(block_text):
            return True
    return False


def _dir_is_root(directory: Path) -> bool:
    """True iff ``directory`` has ``*.tf`` files and one declares a backend block."""
    tf_files = sorted(directory.glob("*.tf"))
    if not tf_files:
        return False
    for tf in tf_files:
        try:
            raw = tf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _file_has_backend_block(_strip_comments(raw)):
            return True
    return False


def discover_roots(infra_dir: str | Path = "infra") -> list[str]:
    """Return every Terraform_Root under ``infra_dir``, repo-relative POSIX paths,
    stable-sorted. A root has ``*.tf`` files AND a backend block; a module (``*.tf``
    but no backend block) is excluded. Nested roots are both included."""
    base = Path(infra_dir)
    if not base.is_dir():
        raise NotADirectoryError(f"infra dir not found or not a directory: {infra_dir}")
    roots: list[str] = []
    # Walk every directory (do NOT prune on finding a root — nested roots exist).
    for directory in sorted(p for p in base.rglob("*") if p.is_dir()):
        if _dir_is_root(directory):
            roots.append(directory.as_posix())
    # base itself could be a root (not the case here, but be correct).
    if _dir_is_root(base):
        roots.append(base.as_posix())
    return sorted(set(roots))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    infra_dir = argv[0] if argv else "infra"
    try:
        roots = discover_roots(infra_dir)
    except NotADirectoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not roots:
        print(
            f"ERROR: discover_tf_roots found ZERO Terraform roots under '{infra_dir}'. "
            "A healthy tree always has at least one root (a dir with a backend block "
            "in versions.tf). Check the CWD and the marker — refusing to report a "
            "false-green empty root set.",
            file=sys.stderr,
        )
        return 1
    for root in roots:
        print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
