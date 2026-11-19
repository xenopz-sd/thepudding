"""Child-run `changed` detection must work for multi-host children (found 2026-11-18, WS-C58).

`'changed=0' not in stdout` reports UNCHANGED as soon as ANY host in the child's recap shows changed=0, so a
fleet-wide child that changed 14 guests but left one alone was reported changed=0. The correct test is whether
any recap line reports changes: `stdout is search('changed=[1-9]')`.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_no_child_run_uses_the_any_host_unchanged_test():
    bad = [str(f.relative_to(REPO)) for f in REPO.glob("ansible/**/*.yml")
           if "'changed=0' not in" in f.read_text(encoding="utf-8")]
    assert not bad, f"use \"(<reg>.stdout | default('')) is search('changed=[1-9]')\" instead: {bad}"


def test_the_replacement_is_used():
    hits = sum(f.read_text(encoding="utf-8").count("is search('changed=[1-9]')") for f in REPO.glob("ansible/**/*.yml"))
    assert hits >= 30, hits
