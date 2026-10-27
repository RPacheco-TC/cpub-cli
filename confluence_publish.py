#!/usr/bin/env python3
"""Publish a folder of Markdown docs to Confluence Cloud (v1.x entry point).

Thin shim kept so ``python3.12 confluence_publish.py DOCS_DIR --space KEY
--parent ID`` keeps working; the code lives in the :mod:`confluence_publisher`
package (``python3.12 -m confluence_publisher`` is equivalent). Importing the
package runs its Python >= 3.12 guard first, so keep this file parseable by
old Pythons.
"""
from confluence_publisher.cli import main

if __name__ == "__main__":
    main()
