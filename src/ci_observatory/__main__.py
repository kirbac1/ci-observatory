"""Allow `python -m ci_observatory`."""

import sys

from ci_observatory.cli import main

sys.exit(main())
