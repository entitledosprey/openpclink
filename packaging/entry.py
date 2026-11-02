"""Packaged entry point: arguments mean the CLI, none means the GUI."""
import sys

if len(sys.argv) > 1:
    from openpclink.cli import main
else:
    from openpclink.gui import main

sys.exit(main())
