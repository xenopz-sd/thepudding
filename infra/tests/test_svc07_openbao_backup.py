"""Offline structural-contract tests for the task-11.1 nightly Raft snapshot job.

Task 11.1 (spec: svc-07-secrets-manager) — requirements.md Requirements 11.1,
11.2, 11.3, 11.5, 11.6, FD.6; design.md "Error Handling (snapshot rows)".

These are OFFLINE, dependency-light assertions over the ``openbao_install``
role's task-11.1 backup wiring:

  * ``tasks/backup.yml`` — deploys the snapshot dir, the templated script, the
    systemd service + timer, and enables the timer; wired into ``tasks/main.yml``
    gated on ``openbao_role == "primary"`` AND ``openbao_backup_enabled``;
  * ``templates/openbao-snapshot.sh.j2`` — the snapshot script, RENDERED here
    with the role defaults (Jinja2, offline) so the gate/failure/retention logic
    is asserted against the concrete shell that would run;
  * ``templates/openbao-raft-snapshot.service.j2`` / ``.timer.j2`` — the systemd
    oneshot service + 02:00-UTC timer;
  * ``defaults/main.yml`` — the task-11.1 var block;
  * ``handlers/main.yml`` — the systemd ``daemon_reload`` handler the render
    tasks notify.

They are NOT property-based tests and NOT ``requires_infra``: there is no live
OpenBao / Docker / PBS here. They pin the snapshot contract the task's brief and
the backup/DR requirements demand:

  * the timer fires nightly at 02:00 UTC (Req 11.1);
  * the script GATES the PBS hand-off on BOTH rc == 0 AND a non-empty snapshot
    file (Req 11.1);
  * on failure it aborts the transfer, logs WITH the affected date, and emits the
    Loki-alertable failure marker (Req 11.2, 11.5, FD.6);
  * the snapshot runs online so OpenBao keeps serving (Req 11.6);
  * PBS + local retention keep-daily >= 7 (Req 11.3);
  * ``backup.yml`` is wired into ``main.yml`` gated primary + backup-enabled;
  * NO secret literal — the scoped snapshot token and PBS credential are
    referenced by FILE PATH / ENV-VAR NAME only, with root-only file perms.

The whole suite is offline and always runs (no gating). It adds only OFFLINE
tests (no ``requires_infra``), so it does not touch the offline-plan-preservation
skip baseline.

Run:  ~/venv/devinfra/bin/pytest infra/tests/test_svc07_openbao_backup.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ROLE_DIR = _REPO_ROOT / "ansible" / "roles" / "openbao_install"
_DEFAULTS = _ROLE_DIR / "defaults" / "main.yml"
_BACKUP_TASKS = _ROLE_DIR / "tasks" / "backup.yml"
_MAIN_TASKS = _ROLE_DIR / "tasks" / "main.yml"
_HANDLERS = _ROLE_DIR / "handlers" / "main.yml"
_SCRIPT_TMPL = _ROLE_DIR / "templates" / "openbao-snapshot.sh.j2"
_SERVICE_TMPL = _ROLE_DIR / "templates" / "openbao-raft-snapshot.service.j2"
_TIMER_TMPL = _ROLE_DIR / "templates" / "openbao-raft-snapshot.timer.j2"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def defaults() -> dict:
    raw = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    assert isinstance(raw, dict) and raw
    return raw


@pytest.fixture(scope="module")
def backup_tasks() -> list[dict]:
    tasks = yaml.safe_load(_BACKUP_TASKS.read_text(encoding="utf-8"))
    assert isinstance(tasks, list) and tasks, "backup.yml must be a task list"
    return tasks


@pytest.fixture(scope="module")
def handlers() -> list[dict]:
    hs = yaml.safe_load(_HANDLERS.read_text(encoding="utf-8"))
    assert isinstance(hs, list) and hs, "handlers/main.yml must be a task list"
    return hs


def _task(tasks: list[dict], needle: str) -> dict:
    for t in tasks:
        if needle in t.get("name", ""):
            return t
    names = [t.get("name", "") for t in tasks]
    raise AssertionError(f"no task whose name contains {needle!r}; names={names}")


@pytest.fixture(scope="module")
def rendered_script(defaults) -> str:
    """Render openbao-snapshot.sh.j2 with the role defaults and return the shell.

    Resolves the chained default references the template needs (mirroring the
    role's own default chaining: stack name -> container name -> backup container
    name; backup dir -> log file) so the render is offline and self-contained —
    the same approach the sibling config-render tests use.
    """
    jinja2 = pytest.importorskip("jinja2")
    ctx = dict(defaults)
    ctx["openbao_primary_container_name"] = f"{defaults['openbao_stack_name']}-openbao-1"
    ctx["openbao_backup_container_name"] = ctx["openbao_primary_container_name"]
    ctx["openbao_backup_log_file"] = f"{defaults['openbao_backup_dir']}/snapshot.log"

    # A few passes resolve the remaining simple {{ }} references among defaults.
    def _resolve(val):
        if isinstance(val, str) and "{{" in val:
            try:
                return jinja2.Template(val).render(**ctx)
            except Exception:
                return val
        return val

    for _ in range(6):
        for k in list(ctx.keys()):
            ctx[k] = _resolve(ctx[k])

    return jinja2.Template(_SCRIPT_TMPL.read_text(encoding="utf-8")).render(**ctx)


# --------------------------------------------------------------------------- #
# Req 11.1 — nightly 02:00 UTC timer
# --------------------------------------------------------------------------- #
class TestTimerSchedule:
    """Req 11.1 — the systemd timer fires nightly at 02:00 UTC."""

    def test_oncalendar_is_0200_utc(self, defaults):
        oncal = defaults["openbao_backup_oncalendar"]
        # `*-*-* 02:00:00 UTC` — 02:00 every day, timezone pinned to UTC.
        assert "02:00:00" in oncal
        assert oncal.strip().endswith("UTC"), f"OnCalendar must pin UTC, got {oncal!r}"
        assert oncal.strip() == "*-*-* 02:00:00 UTC"

    def test_timer_template_renders_oncalendar_and_persistent(self):
        text = _TIMER_TMPL.read_text(encoding="utf-8")
        assert "OnCalendar={{ openbao_backup_oncalendar }}" in text
        # Persistent=true so a missed run fires on next boot, not skipped.
        assert "Persistent={{ openbao_backup_persistent | lower }}" in text
        assert "Unit={{ openbao_backup_service_name }}.service" in text
        assert "WantedBy=timers.target" in text

    def test_persistent_default_true(self, defaults):
        assert defaults["openbao_backup_persistent"] is True


# --------------------------------------------------------------------------- #
# Req 11.1 — the gate: PBS hand-off requires rc == 0 AND a non-empty file
# --------------------------------------------------------------------------- #
class TestGateLogic:
    """Req 11.1 — the PBS hand-off is gated on rc == 0 AND a non-empty file."""

    def test_gate_checks_nonzero_exit(self, rendered_script):
        # a non-zero save rc must fail the run before any PBS transfer.
        assert 'if [ "${save_rc}" -ne 0 ]; then' in rendered_script
        assert "exited non-zero" in rendered_script

    def test_gate_checks_nonempty_file(self, rendered_script):
        # `-s` is true only when the file exists AND has size > 0 (Req 11.1).
        assert 'if [ ! -s "${SNAPSHOT_FILE}" ]; then' in rendered_script
        assert "missing or empty" in rendered_script

    def test_pbs_handoff_is_after_the_gate(self, rendered_script):
        # The PBS transfer invocation must appear textually AFTER both gate
        # checks — the gate calls fail()/exit before the transfer is reached.
        empty_gate = rendered_script.index('if [ ! -s "${SNAPSHOT_FILE}" ]; then')
        rc_gate = rendered_script.index('if [ "${save_rc}" -ne 0 ]; then')
        transfer = rendered_script.index("proxmox-backup-client backup")
        assert rc_gate < transfer, "rc gate must precede the PBS transfer"
        assert empty_gate < transfer, "non-empty gate must precede the PBS transfer"

    def test_pbs_transfer_guarded_by_pbs_enabled(self, rendered_script):
        assert 'if [ "${PBS_ENABLED}" = "true" ]; then' in rendered_script


# --------------------------------------------------------------------------- #
# Req 11.2 / 11.5 / FD.6 — failure aborts, logs with the affected date, emits marker
# --------------------------------------------------------------------------- #
class TestFailureBehaviour:
    """Req 11.2/11.5/FD.6 — on failure: abort, log with the date, emit the marker."""

    def test_failure_marker_default(self, defaults):
        assert defaults["openbao_backup_failure_marker"] == "OPENBAO_SNAPSHOT_FAILURE"

    def test_fail_helper_emits_marker_and_exits_nonzero(self, rendered_script):
        # the fail() helper emits the FAILURE_MARKER line and exits 1.
        assert "fail() {" in rendered_script
        assert 'log_line "${FAILURE_MARKER}"' in rendered_script
        # fail() exits non-zero so systemd records a failed run.
        fail_body = rendered_script[rendered_script.index("fail() {"):]
        fail_body = fail_body[: fail_body.index("\n}")]
        assert "exit 1" in fail_body

    def test_failure_line_carries_the_affected_date(self, rendered_script):
        # the log line embeds date=${RUN_DATE}, and RUN_DATE is a UTC date so the
        # failure identifies the affected date (Req 11.2).
        assert "date=${RUN_DATE}" in rendered_script
        assert 'RUN_DATE="$(date -u +%Y-%m-%d)"' in rendered_script

    def test_failure_marker_rendered_as_literal_in_script(self, rendered_script):
        # the marker is baked into the rendered script so Promtail/Loki can match
        # it (Req 11.5 -> task-10.2 alert within 15 min).
        assert "OPENBAO_SNAPSHOT_FAILURE" in rendered_script

    def test_partial_snapshot_removed_on_failure(self, rendered_script):
        # fail() removes any partial/empty snapshot so a later run/operator does
        # not mistake it for a good backup.
        assert 'rm -f "${SNAPSHOT_FILE}"' in rendered_script


# --------------------------------------------------------------------------- #
# Req 11.6 — snapshot is online (OpenBao keeps serving)
# --------------------------------------------------------------------------- #
class TestOnlineSnapshot:
    """Req 11.6 — the snapshot runs online via docker exec; OpenBao keeps serving."""

    def test_snapshot_via_docker_exec(self, rendered_script):
        assert "docker exec" in rendered_script
        assert "operator raft snapshot save" in rendered_script

    def test_no_stop_or_seal_before_snapshot(self, rendered_script):
        # the script must NOT stop the container or seal OpenBao to snapshot —
        # `raft snapshot save` is online/non-blocking (Req 11.6).
        assert "docker stop" not in rendered_script
        assert "operator seal" not in rendered_script


# --------------------------------------------------------------------------- #
# Req 11.3 — retention keep-daily >= 7 (PBS + local), refused below 7
# --------------------------------------------------------------------------- #
class TestRetention:
    """Req 11.3 — PBS and local retention keep >= 7 nightly snapshots."""

    def test_pbs_keep_daily_at_least_7(self, defaults):
        assert int(defaults["openbao_backup_pbs_keep_daily"]) >= 7

    def test_local_keep_daily_at_least_7(self, defaults):
        assert int(defaults["openbao_backup_local_keep_daily"]) >= 7

    def test_script_refuses_keep_daily_below_7(self, rendered_script):
        # preflight refuses a keep-daily < 7 rather than under-retaining silently.
        assert 'if [ "${PBS_KEEP_DAILY}" -lt 7 ]; then' in rendered_script

    def test_script_passes_keep_daily_to_pbs_prune(self, rendered_script):
        assert 'proxmox-backup-client prune' in rendered_script
        assert '--keep-daily "${PBS_KEEP_DAILY}"' in rendered_script

    def test_local_prune_keeps_configured_count(self, rendered_script):
        # local prune keeps LOCAL_KEEP_DAILY newest, deletes only older ones.
        assert 'tail -n +"$((LOCAL_KEEP_DAILY + 1))"' in rendered_script


# --------------------------------------------------------------------------- #
# Wiring — backup.yml deploys units and is included gated primary + enabled
# --------------------------------------------------------------------------- #
class TestWiring:
    """backup.yml deploys the units/timer and is wired into main.yml, gated."""

    def test_backup_included_in_main_gated_primary_and_enabled(self):
        tasks = yaml.safe_load(_MAIN_TASKS.read_text(encoding="utf-8"))
        include = next(
            (t for t in tasks if t.get("ansible.builtin.include_tasks") == "backup.yml"),
            None,
        )
        assert include is not None, "tasks/main.yml must include backup.yml"
        gate = yaml.safe_dump(include["when"])
        assert 'openbao_role == "primary"' in gate
        assert "openbao_backup_enabled" in gate

    def test_backup_enabled_default_true(self, defaults):
        assert defaults["openbao_backup_enabled"] is True

    def test_backup_deploys_script_service_timer(self, backup_tasks):
        script = _task(backup_tasks, "Render the nightly Raft snapshot script")
        assert script["ansible.builtin.template"]["src"] == "openbao-snapshot.sh.j2"
        assert script["ansible.builtin.template"]["mode"] == "0700"

        svc = _task(backup_tasks, "Render the snapshot systemd service unit")
        assert svc["ansible.builtin.template"]["src"] == "openbao-raft-snapshot.service.j2"

        timer = _task(backup_tasks, "Render the snapshot systemd timer unit")
        assert timer["ansible.builtin.template"]["src"] == "openbao-raft-snapshot.timer.j2"

    def test_unit_renders_notify_daemon_reload_handler(self, backup_tasks):
        for needle in (
            "Render the snapshot systemd service unit",
            "Render the snapshot systemd timer unit",
        ):
            t = _task(backup_tasks, needle)
            assert t.get("notify") == "Reload systemd for snapshot units", needle

    def test_timer_enabled_and_started(self, backup_tasks):
        t = _task(backup_tasks, "Enable + start the nightly snapshot timer")
        systemd = t["ansible.builtin.systemd"]
        assert systemd["name"] == "{{ openbao_backup_service_name }}.timer"
        assert systemd["enabled"] is True
        assert systemd["state"] == "started"

    def test_daemon_reload_handler_exists(self, handlers):
        h = _task(handlers, "Reload systemd for snapshot units")
        assert h["ansible.builtin.systemd"]["daemon_reload"] is True

    def test_snapshot_dir_is_root_only_0700(self, backup_tasks):
        t = _task(backup_tasks, "Ensure the snapshot output directory exists")
        f = t["ansible.builtin.file"]
        assert f["state"] == "directory"
        assert f["owner"] == "root"
        assert f["mode"] == "0700"


# --------------------------------------------------------------------------- #
# Service unit — root, oneshot, tolerant EnvironmentFile
# --------------------------------------------------------------------------- #
class TestServiceUnit:
    """The systemd service is a root oneshot loading the credential EnvironmentFile."""

    def test_service_is_oneshot_root(self):
        text = _SERVICE_TMPL.read_text(encoding="utf-8")
        assert "Type=oneshot" in text
        assert "User=root" in text
        assert "ExecStart={{ openbao_backup_script_path }}" in text

    def test_service_environmentfile_is_tolerant_of_absence(self):
        text = _SERVICE_TMPL.read_text(encoding="utf-8")
        # "-" prefix => a missing EnvironmentFile does not abort the unit
        # (PBS-disabled case); the script fails closed if PBS is on but the
        # credential is absent.
        assert "EnvironmentFile=-{{ openbao_backup_env_file }}" in text


# --------------------------------------------------------------------------- #
# Secrecy — no secret literal; token/PBS credential referenced by path/env only
# --------------------------------------------------------------------------- #
class TestSecrecy:
    """No secret value defaulted or inlined; root-only credential files."""

    def test_token_and_pbs_credential_referenced_by_path_or_envvar_only(self, defaults):
        # the scoped snapshot token is a FILE PATH, never a token value.
        assert defaults["openbao_backup_token_file"].startswith("/")
        # the PBS credential is referenced by ENV-VAR NAME, never a value.
        assert defaults["openbao_backup_pbs_password_env_var"] == "PBS_PASSWORD"
        # the EnvironmentFile carrying the PBS credential is a path.
        assert defaults["openbao_backup_env_file"].startswith("/")

    def test_no_token_or_pbs_password_value_defaulted(self, defaults):
        # neither the snapshot token nor a PBS password value is defaulted.
        assert "openbao_backup_token" not in defaults  # only *_token_file exists
        assert "openbao_backup_pbs_password" not in defaults  # only *_env_var exists

    def test_script_reads_token_from_file_not_inline(self, rendered_script):
        # token read from the root-only file, never a literal; not traced.
        assert 'SNAPSHOT_TOKEN="$(cat "${TOKEN_FILE}")"' in rendered_script
        # token scrubbed after use; xtrace is never ENABLED so it is not traced.
        assert "unset SNAPSHOT_TOKEN" in rendered_script
        # no ACTIVE `set -x` / `set -o xtrace` line (a comment mentioning it is
        # fine) — check each non-comment line rather than a bare substring.
        for line in rendered_script.splitlines():
            code = line.split("#", 1)[0].strip()
            assert code not in ("set -x", "set -o xtrace"), (
                f"xtrace must never be enabled (would trace the token): {line!r}"
            )

    def test_script_reads_pbs_password_by_indirect_envvar(self, rendered_script):
        # PBS password referenced indirectly via the env var NAME, then scrubbed.
        assert "${!PBS_PASSWORD_ENV_VAR" in rendered_script
        assert "unset PBS_PASSWORD" in rendered_script

    def test_credential_dir_created_root_only(self, backup_tasks):
        t = _task(backup_tasks, "Ensure the backup credential/config directory exists")
        f = t["ansible.builtin.file"]
        assert f["owner"] == "root"
        assert f["mode"] == "0700"

    def test_no_secret_literal_in_backup_tree(self):
        pat = re.compile(
            r"(hvs\.[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9]{15,}|s\.[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
        )
        for path in (
            _BACKUP_TASKS,
            _SCRIPT_TMPL,
            _SERVICE_TMPL,
            _TIMER_TMPL,
            _DEFAULTS,
            _HANDLERS,
        ):
            assert not pat.search(path.read_text(encoding="utf-8")), (
                f"real-looking secret literal in {path}"
            )
