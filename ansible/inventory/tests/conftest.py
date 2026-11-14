"""Pytest bootstrap for the inventory-generator tests.

Puts the generator's directory (``ansible/inventory/``) on ``sys.path`` so
``import generate_inventory`` resolves regardless of the pytest invocation cwd.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# WS-C46: no Hypothesis per-example deadline. The default 200 ms check made a cold or loaded run fail
# intermittently (testing-strategy.md: no time-dependent flakes); the examples themselves still run.
try:
    from hypothesis import settings as _hyp_settings

    _hyp_settings.register_profile("devinfra", deadline=None)
    _hyp_settings.load_profile("devinfra")
except ImportError:  # pragma: no cover - hypothesis is in requirements.txt
    pass
