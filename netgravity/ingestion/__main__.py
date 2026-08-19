"""Enables `python -m netgravity.ingestion`."""

import sys

from netgravity.ingestion.cli import main

if __name__ == "__main__":
    sys.exit(main())
