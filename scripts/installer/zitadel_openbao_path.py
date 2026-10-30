#!/usr/bin/env python3
"""Pure per-app OpenBao KV path derivation for ZITADEL onboarding (SVC-06).

Feature: zitadel-spa-pkce-onboarding, design Component 6 (R5.1). The
``zitadel-org-provision`` Ansible role writes each newly-created confidential /
API client secret to a KV path that is UNIQUE PER APPLICATION within the
project, so two applications in the same project never resolve to the same path.

The role renders that path from the ``zop_openbao_app_secret_path_tmpl`` default
(``<mount>/proj-<slug>/zitadel/oidc/{app_name}``) via Jinja/``str.format``. This
module factors the identical derivation out as a pure, stdlib-only Python
function so the stability / collision-free property (design Property 7) is
unit-testable WITHOUT a live Ansible run or a live Proxmox cluster.

Stdlib only — no third-party dependency (mirrors ``zitadel_provision.py``).
"""

from __future__ import annotations

# The default KV mount and path shape, mirroring the role's
# `zop_openbao_kv_mount` + `zop_openbao_app_secret_path_tmpl` defaults. Kept here
# so the pure function matches the role's rendered path byte-for-byte.
DEFAULT_KV_MOUNT = "secret"
PATH_TEMPLATE = "{mount}/proj-{slug}/zitadel/oidc/{app_name}"


def derive_app_secret_path(slug: str, app_name: str,
                           mount: str = DEFAULT_KV_MOUNT) -> str:
    """Derive the per-app OpenBao KV path for one project app (R5.1).

    Returns ``<mount>/proj-<slug>/zitadel/oidc/<app_name>``. The derivation is a
    pure function of ``(slug, app_name, mount)``:

    * **Stable** — the same inputs always yield the same path.
    * **Collision-free per project** — the path ends in the app name, which is
      unique within a project, so two distinct app names in one project (same
      slug + mount) always yield distinct paths.
    """
    return PATH_TEMPLATE.format(mount=mount, slug=slug, app_name=app_name)
