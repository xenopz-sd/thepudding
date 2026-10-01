#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Parse-time stub for ``community.docker.docker_compose_v2``.

Task 5.5 fixture (spec: svc-07-secrets-manager). This fixture ships a *scoped*
fake ``community.docker`` collection so the offline
``docker_container_info`` stub (in this same collection) can shadow the FQCN the
``openbao_install`` role probes with. But shadowing the collection masks the real
one wholesale, so any OTHER ``community.docker`` module the role references must
also exist here or Ansible fails at PARSE time.

The ``openbao_install`` role's restart handler references
``community.docker.docker_compose_v2``. In the "already-configured" idempotency
scenario the whole bring-up (and thus this handler) is GATED OFF, so this module
is never actually invoked — it only needs to RESOLVE at parse time. This stub
therefore exists purely to satisfy resolution; if it ever WERE invoked it fails
loudly rather than silently faking a compose reconcile, so it can never mask a
real bring-up in a scenario where one was expected.
"""

from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def main() -> None:
    module = AnsibleModule(argument_spec=dict(), supports_check_mode=True)
    module.fail_json(
        msg=(
            "docker_compose_v2 stub was invoked, but the openbao_install "
            "idempotency fixture only exercises the SKIP path where bring-up is "
            "gated off. Reaching this stub means the version-check gate did not "
            "engage — investigate the fixture, do not treat this as a pass."
        )
    )


if __name__ == "__main__":
    main()
