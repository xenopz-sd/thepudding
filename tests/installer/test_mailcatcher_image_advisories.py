"""WS-C22 — the pinned Mailpit image must be at or above the first patched version of every recorded advisory.

Mailpit has had a steady stream of advisories in the same classes (SSRF in the proxy, link check and HTML check;
unbounded buffering; WebSocket origin handling). WS-C17 pinned ``v1.33.0`` but could not tell whether that was above
the SSRF fix, because the advisory text read at the time named no versions. This binds the pinned tag to a recorded
table taken from the GitHub advisory API (``tests/fixtures/advisories/mailpit-advisories.json``): a downgrade, or a
new advisory added to the table with a higher fixed version, fails here at author time, offline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "fixtures" / "advisories" / "mailpit-advisories.json"
_DEFAULTS = _ROOT / "ansible" / "roles" / "mailcatcher_install" / "defaults" / "main.yml"


def _ver(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    assert m, f"not an exact x.y.z version: {text!r}"
    return int(m[1]), int(m[2]), int(m[3])


def _pinned_tag() -> str:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))["mailcatcher_image_tag"]


def _advisories() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["advisories"]


def violations(pin: str, advisories: list[dict]) -> list[str]:
    """Advisories whose first patched version is ABOVE the pin (i.e. the pin is still vulnerable)."""
    return [f"{a['cve']} ({a['ghsa']}): fixed in {a['first_patched']}, pin is {pin}"
            for a in advisories if _ver(pin) < _ver(a["first_patched"])]


def test_non_vacuity_the_fixture_is_loaded_and_well_formed():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["recorded_on"] and data["source"].startswith("GitHub advisory database")
    advs = data["advisories"]
    assert len(advs) >= 14, "the recorded table must not silently shrink"
    for a in advs:
        assert re.fullmatch(r"CVE-\d{4}-\d{4,}", a["cve"]), a
        assert re.fullmatch(r"GHSA(-[a-z0-9]{4}){3}", a["ghsa"]), a
        _ver(a["first_patched"])
    assert len({a["cve"] for a in advs}) == len(advs), "no duplicate CVE rows"
    assert any(a["cve"] == "CVE-2026-21859" for a in advs), "the advisory WS-C22 was opened for must be present"


def test_the_pinned_tag_is_above_every_recorded_first_patched_version():
    assert violations(_pinned_tag(), _advisories()) == []


def test_the_guard_has_teeth_an_old_pin_is_reported_with_the_advisory_named():
    old = violations("v1.28.0", _advisories())
    assert any("CVE-2026-21859" in v for v in old), "1.28.0 is exactly the last vulnerable /proxy version"
    assert len(old) >= 10
    assert violations("v1.30.5", _advisories()) == [
        "CVE-2026-67448 (GHSA-8r62-w5wh-fc5m): fixed in 1.30.6, pin is v1.30.5"]


def test_the_pin_is_an_exact_version_not_a_moving_tag():
    assert re.fullmatch(r"v\d+\.\d+\.\d+", _pinned_tag())
