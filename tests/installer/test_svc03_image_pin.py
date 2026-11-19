"""SVC-03 R8 — the Garage image is pinned exactly and is above every recorded security fix.

Binds ``garage_install/defaults/main.yml`` (repo, tag, digest) to the committed table
``tests/fixtures/advisories/garage-advisories.json`` (taken from the Security sections
of the upstream release notes). A downgrade, a moving tag, or a new row whose fix is
newer than the pin fails here, offline (the WS-C22 pattern).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "fixtures" / "advisories" / "garage-advisories.json"
_DEFAULTS = _ROOT / "ansible" / "roles" / "garage_install" / "defaults" / "main.yml"


def _ver(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    assert m, f"not an exact x.y.z version: {text!r}"
    return int(m[1]), int(m[2]), int(m[3])


def _defaults() -> dict:
    return yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))


def _advisories() -> list[dict]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["advisories"]


def violations(pin: str, advisories: list[dict]) -> list[str]:
    return [f"{a['ref']}: fixed in {a['first_patched']}, pin is {pin}"
            for a in advisories if _ver(pin) < _ver(a["first_patched"])]


def test_non_vacuity_the_fixture_is_loaded_and_well_formed():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["recorded_on"] and "git.deuxfleurs.fr" in data["source"]
    advs = data["advisories"]
    assert len(advs) >= 3, "the recorded table must not silently shrink"
    for a in advs:
        assert re.fullmatch(r"#\d+", a["ref"]), a
        _ver(a["first_patched"])
    assert len({a["ref"] for a in advs}) == len(advs)
    assert {"#1573", "#1581"} <= {a["ref"] for a in advs}, "the v2.5.0 fixes the pin was chosen for"


def test_the_pin_is_exact_and_digest_pinned():
    d = _defaults()
    assert d["garage_image_repo"] == "dxflrs/garage"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", d["garage_image_tag"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", d["garage_image_digest"])
    assert d["garage_image"] == "{{ garage_image_repo }}:{{ garage_image_tag }}@{{ garage_image_digest }}"


def test_the_pinned_tag_is_above_every_recorded_fix():
    assert violations(_defaults()["garage_image_tag"], _advisories()) == []


def test_the_guard_has_teeth():
    assert violations("v2.4.1", _advisories()) == [
        "#1573: fixed in 2.5.0, pin is v2.4.1",
        "#1581: fixed in 2.5.0, pin is v2.4.1",
    ]
    assert len(violations("v0.9.1", _advisories())) == 3
