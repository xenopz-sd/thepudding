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

# WS-C46: no Hypothesis per-example deadline. The default 200 ms check made a cold or loaded run fail
# intermittently (testing-strategy.md: no time-dependent flakes); the examples themselves still run.
try:
    from hypothesis import settings as _hyp_settings

    _hyp_settings.register_profile("devinfra", deadline=None)
    _hyp_settings.load_profile("devinfra")
except ImportError:  # pragma: no cover - hypothesis is in requirements.txt
    pass
