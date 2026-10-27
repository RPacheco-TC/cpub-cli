"""``python3.12 -m confluence_publisher``: same command line as ``confluence_publish.py``."""
import sys

from .cli import main

if __name__ == "__main__":
    # argparse names the program after argv[0], which is ".../__main__.py" under -m;
    # keep usage and error lines identical to the confluence_publish.py entry point.
    sys.argv[0] = "confluence_publish.py"
    try:
        main()
    finally:
        # A script run flushes stdout before an uncaught SystemExit message reaches
        # stderr; runpy (-m) does not, so redirected output would put the error
        # above the plan table. Flush to keep the shim's order.
        sys.stdout.flush()
