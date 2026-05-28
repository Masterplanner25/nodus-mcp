"""nodus.nd entry-point for nodus-mcp.

Declares the nd root directory so that `import "nodus-mcp"` resolves
after `pip install nodus-mcp`. Contract per nodus-lang's
docs/guide/library-entry-points.md.
"""

import os


def get_nd_root() -> str:
    """Return the absolute path to this package's .nd source root directory."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "nd")
