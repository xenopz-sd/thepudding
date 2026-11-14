"""Guards for WS-C46: the suite names every failure and no test depends on wall-clock speed.

THE BUG: twice a single `tests/installer` failure appeared on the first local run after edits and vanished on every
re-run; the summary never named the test, so it could not be fixed. The most likely cause is Hypothesis' default
200 ms per-example deadline tripping on a cold or loaded machine (testing-strategy.md: time-dependent flakes are not
accepted).
THE FIX: `-rf` in the root pytest.ini so every failure is named in the summary, and a shared Hypothesis profile with
no deadline loaded by every test root's conftest (examples still run; only the timing check goes).
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CONFTESTS = [
    _REPO / "tests/installer/conftest.py",
    _REPO / "infra/tests/conftest.py",
    _REPO / "ansible/inventory/tests/conftest.py",
    _REPO / "infra/platform-foundation/scripts/tests/conftest.py",
]


def test_root_pytest_ini_names_every_failure_and_stays_offline():
    cfg = configparser.ConfigParser()
    cfg.read(_REPO / "pytest.ini")
    addopts = cfg["pytest"]["addopts"].split()
    assert "-rf" in addopts
    assert '"not' in " ".join(addopts) and "requires_infra\"" in " ".join(addopts), "offline default preserved"


@pytest.mark.parametrize("conftest", _CONFTESTS, ids=lambda p: str(p.relative_to(_REPO)))
def test_every_root_loads_the_no_deadline_hypothesis_profile(conftest):
    text = conftest.read_text(encoding="utf-8")
    assert 'register_profile("devinfra", deadline=None)' in text
    assert 'load_profile("devinfra")' in text


def test_the_profile_is_active_here():
    hypothesis = pytest.importorskip("hypothesis")
    assert hypothesis.settings().deadline is None
