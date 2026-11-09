"""WS-C20 — the sdn_gateway role must not leave a persistent interface stanza for a bridge that does not exist.

Bug (found live on a clean cluster, WS-C17/WS-C19): ``platform-bootstrap.yml -e bootstrap_scope=host,api``
serves every registry-ACTIVE project VLAN (100, 101, 102), but their SDN bridges ``p100`` ... do not exist
yet. ``roles/sdn_gateway/tasks/configure.yml`` wrote ``/etc/network/interfaces.d/sdn-gw-100.cfg`` (and
notified the networking-reload handler) BEFORE asserting that the bridge exists, then failed closed. The
run stopped correctly, but the host kept a PERSISTENT stanza that describes an address on a nonexistent
bridge, which the next ``ifreload -a`` or reboot trips over.

The fix is an ordering one: the read-only probe and the fail-closed assertion run FIRST, so a missing bridge
leaves the host exactly as it was. Pure offline: the task list is parsed, nothing is executed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONFIGURE = _ROOT / "ansible" / "roles" / "sdn_gateway" / "tasks" / "configure.yml"

_ASSERT_FRAGMENT = "Assert the SDN VNet bridge"
_PROBE_FRAGMENT = "Read current addresses on the SDN VNet bridge"
_STANZA_FRAGMENT = "Ensure persistent L3 gateway address stanza"
_ATTACH_FRAGMENT = "Attach the guarded gateway address"

# Modules that change host state. The read-only probe uses ``command`` with ``changed_when: false`` so it is
# recognised separately below.
_WRITING_MODULES = ("ansible.builtin.template", "ansible.builtin.copy", "ansible.builtin.lineinfile",
                    "ansible.builtin.file", "ansible.builtin.blockinfile")


def _tasks() -> list[dict]:
    return [t for t in yaml.safe_load(_CONFIGURE.read_text(encoding="utf-8")) if isinstance(t, dict)]


def _index(fragment: str) -> int:
    for i, t in enumerate(_tasks()):
        if fragment in str(t.get("name")):
            return i
    raise AssertionError(f"no task named like {fragment!r} in configure.yml")


def test_non_vacuity_the_tasks_under_test_exist():
    for frag in (_PROBE_FRAGMENT, _ASSERT_FRAGMENT, _STANZA_FRAGMENT, _ATTACH_FRAGMENT):
        _index(frag)


def test_bridge_assertion_runs_before_the_persistent_stanza_is_written():
    """Bug condition: the stanza task precedes the assertion, so a missing bridge leaves a dangling file."""
    assert _index(_PROBE_FRAGMENT) < _index(_ASSERT_FRAGMENT) < _index(_STANZA_FRAGMENT), (
        "the read-only probe and the fail-closed assert must precede the interfaces.d write")


def test_nothing_that_mutates_the_host_runs_before_the_assertion():
    """Stronger than ordering two named tasks: EVERY task ahead of the assertion must be a read."""
    tasks = _tasks()
    for t in tasks[:_index(_ASSERT_FRAGMENT)]:
        name = str(t.get("name"))
        mutating = [m for m in _WRITING_MODULES if m in t]
        assert not mutating, f"{name!r} writes host state ({mutating}) before the bridge is proven to exist"
        assert "notify" not in t, f"{name!r} notifies a handler before the bridge is proven to exist"
        cmd = t.get("ansible.builtin.command")
        if cmd is not None:
            assert t.get("changed_when") is False, f"{name!r} is a command that is not declared read-only"


def test_the_assertion_still_fails_closed_naming_the_vlan_and_the_remedy():
    a = next(t for t in _tasks() if _ASSERT_FRAGMENT in str(t.get("name")))
    body = a["ansible.builtin.assert"]
    assert any("rc == 0" in c for c in body["that"])
    msg = " ".join(str(body["fail_msg"]).split())
    assert "sdn_gateway_vlan_id" in msg and "No gateway/NAT was bound" in msg
    assert "platform-foundation" in msg, "the remedy (create the fabric first) must still be named"


def test_stanza_is_still_written_after_the_assertion_and_still_reloads_networking():
    stanza = next(t for t in _tasks() if _STANZA_FRAGMENT in str(t.get("name")))
    assert stanza["notify"] == "Reload host networking"
    assert stanza["ansible.builtin.template"]["dest"] == "/etc/network/interfaces.d/sdn-gw-{{ sdn_gateway_vlan_id }}.cfg"
    assert stanza["ansible.builtin.template"]["mode"] == "0644"
    # the live attach, masquerade and firewall steps are unchanged and still follow the stanza
    assert _index(_STANZA_FRAGMENT) < _index(_ATTACH_FRAGMENT)


@pytest.mark.parametrize("bridge_present", [True, False])
def test_simulated_run_writes_the_stanza_only_when_the_bridge_exists(bridge_present):
    """Execute the task order symbolically: walk the tasks, stop at the first failing assertion, and record
    what reached the host. With the bridge absent the host must be untouched."""
    host_writes: list[str] = []
    for t in _tasks():
        name = str(t.get("name"))
        if _ASSERT_FRAGMENT in name and not bridge_present:
            break  # the assertion fails the play here
        if any(m in t for m in _WRITING_MODULES) or "notify" in t:
            host_writes.append(name)
    if bridge_present:
        assert any(_STANZA_FRAGMENT in w for w in host_writes), "a present bridge still gets its stanza"
    else:
        assert host_writes == [], f"bridge absent but the host was modified by: {host_writes}"
