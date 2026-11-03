"""Allow ``python -m gitlane`` (used by the app-menu launcher)."""

import sys

from .cli import main

sys.exit(main())
