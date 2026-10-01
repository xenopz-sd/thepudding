"""Pytest bootstrap for the SVC-07 automated-installer helper tests.

Adds the ``scripts/installer/`` directory to ``sys.path`` so that
``import cluster_env`` resolves regardless of the pytest invocation cwd.

This mirrors the repo's established conftest idiom (see
``ansible/inventory/tests/conftest.py`` and
``infra/platform-foundation/scripts/tests/conftest.py``) and the dev-workflow
rule that tests run via the project venv (``~/venv/devinfra/bin/pytest``)
without relying on an ambient install of the helper module.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/installer/conftest.py -> repo root is parents[2]; the helper lives at
# scripts/installer/cluster_env.py.
_INSTALLER_DIR = Path(__file__).resolve().parents[2] / "scripts" / "installer"

if str(_INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALLER_DIR))
