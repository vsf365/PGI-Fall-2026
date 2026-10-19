"""
Pytest configuration and shared fixtures for nuclear outage prediction pipeline tests.
"""

import sys
import glob
from pathlib import Path
import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure conda site-packages is in sys.path if available
for p in glob.glob("/opt/miniconda3/lib/python*/site-packages"):
    if p not in sys.path:
        sys.path.append(p)
