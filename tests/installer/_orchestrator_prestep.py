"""The one task a thin bootstrap orchestrator may run before its phases (SVC-10 design D5).

Every bootstrap imports the ``platform_domain`` role as its FIRST task so that
``PLATFORM_DOMAIN`` in the cluster env file becomes the platform-domain default.
The thin-orchestrator guards pin the phase structure that follows it; they call
:func:`phase_tasks` to drop exactly that one pre-step, after asserting it is the
``platform_domain`` import and nothing else.
"""
from __future__ import annotations

PRESTEP_ROLE = "platform_domain"


def is_prestep(task: dict) -> bool:
    spec = task.get("ansible.builtin.import_role") or task.get("import_role")
    return isinstance(spec, dict) and spec.get("name") == PRESTEP_ROLE


def phase_tasks(tasks: list[dict] | None) -> list[dict]:
    """Top-level tasks without the leading platform_domain pre-step (which must be first, once)."""
    tasks = list(tasks or [])
    assert tasks and is_prestep(tasks[0]), "the first task must import the platform_domain role"
    rest = tasks[1:]
    assert not any(is_prestep(t) for t in rest), "platform_domain may be imported only once, first"
    assert set(tasks[0]) <= {"name", "ansible.builtin.import_role", "import_role", "vars"}, (
        "the platform_domain pre-step must carry no when/tags/block"
    )
    return rest
