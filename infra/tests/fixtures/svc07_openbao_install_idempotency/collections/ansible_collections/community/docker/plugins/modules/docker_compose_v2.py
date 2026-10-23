#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Offline stub for ``community.docker.docker_compose_v2``.

Task 5.5 fixture (spec: svc-07-secrets-manager; extended by the
ci-offline-test-pipeline CI-failure fix, design §3.7). This fixture ships a
*scoped* fake ``community.docker`` collection so the offline
``docker_container_info`` stub can shadow the FQCN the ``openbao_install`` role
probes with. But shadowing the collection masks the real one wholesale, so EVERY
``community.docker`` module the role references must exist here — and, for the
ones the role actually INVOKES in the "already-configured" scenario, must accept
the role's real call signature and behave idempotently.

This stub was ORIGINALLY written fail-loud on the assumption that in the
already-configured idempotency scenario the whole bring-up (and thus every
``docker_compose_v2`` call) was gated off. That assumption was WRONG: the
version-check gate freezes only the PRIMARY stack's version-sensitive bring-up —
the generic ``docker-compose-app`` reconcile (and the TRANSIT UNSEALER stack it
builds) is deliberately NOT gated on the version check (openbao_install
tasks/main.yml §"NOT gated on the version check" note, Req 13.2), so it RUNS on
the already-configured path. The CI run proved this: the priming play reached
``docker-compose-app : Reconcile the Compose stack (declarative)`` and the
fail-loud stub aborted it with ``Unsupported parameters``.

So this module IS called. ``docker-compose-app/tasks/main.yml`` invokes it as
``docker_compose_v2: {project_src, project_name, state, recreate, pull}``. The
role consumes only the idempotency SEMANTICS of the result (changed=0 on a
converged stack), not any field of the returned object, so this stub declares
exactly that arg subset and returns ``changed=False`` — the "stack already
converged, nothing to do" result — keeping the downstream ``--check`` changed=0
assertion honest without a Docker daemon. This is the same functional-stub
approach as the sibling ``docker_volume`` / ``docker_volume_info`` stubs.
"""

from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def main() -> None:
    module = AnsibleModule(
        # Mirror the subset of the real module's args the docker-compose-app
        # role passes (tasks/main.yml "Reconcile the Compose stack").
        argument_spec=dict(
            project_src=dict(type="str"),
            project_name=dict(type="str"),
            state=dict(type="str", default="present"),
            recreate=dict(type="str", default="auto"),
            pull=dict(type="str", default="policy"),
        ),
        supports_check_mode=True,
    )
    # Converged-stack idempotent result: no change. changed=False keeps the
    # downstream `--check` changed=0 idempotency assertion honest offline.
    module.exit_json(changed=False, containers=[], services=[])


if __name__ == "__main__":
    main()
