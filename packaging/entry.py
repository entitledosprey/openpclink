"""Packaged entry point: arguments mean the CLI, none means the GUI."""
import sys

if len(sys.argv) > 2 and sys.argv[1] == "--ftdi-helper":
    from openpclink.link_helper import helper_main

    sys.exit(helper_main(sys.argv[2]))
elif len(sys.argv) > 1:
    from openpclink.cli import main
else:
    from openpclink.gui import main

sys.exit(main())
