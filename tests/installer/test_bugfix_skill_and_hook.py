"""WS-M6 (author-time half): structural guard for the bugfix-workflow skill and the
spec-completeness hook. Offline. The hook is author-time feedback, never a CI gate,
so it must only invoke the existing offline guards and must not mutate anything."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".kiro/skills/bugfix-workflow/SKILL.md"
HOOK = ROOT / ".kiro/hooks/spec-completeness-check.json"


def _hook():
    data = json.loads(HOOK.read_text())
    assert data["version"] == "v1"
    assert len(data["hooks"]) == 1
    return data["hooks"][0]


def test_skill_has_frontmatter_and_core_steps():
    text = SKILL.read_text()
    m = re.match(r"---\nname: bugfix-workflow\ndescription: .+\n---\n", text)
    assert m, "SKILL.md must start with name/description frontmatter"
    for needle in ("Re-verify the premise", "Bug-condition tests first",
                   "Four-root gate", "throwaway", "SEPARATE pytest"):
        assert needle in text, needle


def test_hook_is_save_triggered_and_scoped_to_specs():
    h = _hook()
    assert h["trigger"] == "PostFileSave"
    rx = re.compile(h["matcher"])
    assert rx.search(".kiro/specs/foo/requirements.md")
    assert rx.search(".kiro/specs/foo/validation.md")
    assert not rx.search(".kiro/specs/foo/design.md")
    assert not rx.search("README.md")


def test_hook_runs_only_the_offline_guards():
    cmd = _hook()["action"]["command"]
    assert "test_spec_completeness.py" in cmd
    assert "test_validation_completeness.py" in cmd
    for forbidden in ("ansible-playbook", "terraform", "git ", "ssh ", "rm "):
        assert forbidden not in cmd


def test_hook_is_not_a_ci_gate():
    ci = (ROOT / ".gitlab-ci.yml").read_text()
    assert "spec-completeness-check" not in ci
