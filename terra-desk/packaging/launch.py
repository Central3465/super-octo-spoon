"""Entry point for the packaged terra-desk.exe (PyInstaller needs a script, not a package)."""
import sys

from terra_desk.__main__ import main

sys.exit(main())
