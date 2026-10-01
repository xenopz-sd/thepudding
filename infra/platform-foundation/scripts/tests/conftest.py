"""Pytest configuration for the network-foundation property suite.

Adds the ``scripts/`` directory (this file's parent's parent) to ``sys.path``
so that both the ``netfoundation`` package and the top-level ``onboard_project``
module import cleanly when the suite is run from anywhere.

This mirrors the dev-workflow rule that tests are run via the project venv
(``~/venv/devinfra/bin/pytest``) without relying on an ambient install of the
derivation layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
