"""Project-eigen testrunner. Volgens HP's CLAUDE.md: `python tests/main.py`."""
import sys

import pytest


if __name__ == '__main__':
    sys.exit(pytest.main(['-x', '-q', 'tests/']))
