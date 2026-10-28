"""``python3.12 -m confluence_publisher``: the same command as ``cpub``.

Help and ``Fix:`` lines name it ``python3.12 -m confluence_publisher``:
:class:`~.cli.CommandName` recognises ``.../confluence_publisher/__main__.py``
in ``sys.argv[0]``.
"""
import sys

from .cli import main

if __name__ == "__main__":
    try:
        main()
    finally:
        # A script run flushes stdout before an uncaught SystemExit message reaches
        # stderr; runpy (-m) does not, so redirected output would put the error
        # above the plan table. Flush to keep the shim's order.
        sys.stdout.flush()
