"""Pytest bootstrap for the inventory-generator tests.

Puts the generator's directory (``ansible/inventory/``) on ``sys.path`` so
``import generate_inventory`` resolves regardless of the pytest invocation cwd.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
