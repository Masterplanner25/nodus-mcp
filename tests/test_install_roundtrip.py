"""Roundtrip test: pip install -e + import "nodus-mcp" via entry-point.

This is the only test in the scaffold. It validates that:
  1. The nodus.nd entry-point is correctly declared in pyproject.toml.
  2. nodus_mcp.nd.get_nd_root() returns a directory containing index.nd.
  3. The nodus-lang resolver finds the .nd file through the entry point,
     so that `import "nodus-mcp"` executes from a Nodus script.

It does NOT test any MCP protocol behaviour — that comes in Phase A+.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

# Make sure nodus-lang is importable (dev install assumed for both packages)
_NODUS_SRC = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "Coding Language", "src")
)
if _NODUS_SRC not in sys.path:
    sys.path.insert(0, _NODUS_SRC)


class EntryPointRoundtripTest(unittest.TestCase):
    """Validates the nodus.nd entry-point contract end-to-end."""

    def test_get_nd_root_returns_existing_directory(self):
        """get_nd_root() must return a real directory that contains index.nd."""
        from nodus_mcp.nd import get_nd_root
        nd_root = get_nd_root()
        self.assertTrue(
            os.path.isdir(nd_root),
            f"get_nd_root() returned {nd_root!r} which is not a directory",
        )
        index_path = os.path.join(nd_root, "index.nd")
        self.assertTrue(
            os.path.isfile(index_path),
            f"Expected index.nd at {index_path}",
        )

    def test_entry_point_declared_in_metadata(self):
        """The nodus.nd entry-point group must be registered after pip install."""
        from importlib.metadata import entry_points
        eps = list(entry_points(group="nodus.nd", name="nodus-mcp"))
        self.assertEqual(
            len(eps), 1,
            f"Expected exactly one 'nodus.nd'/'nodus-mcp' entry point, got {eps}",
        )
        fn = eps[0].load()
        self.assertTrue(callable(fn), "Entry point must be a callable")
        path = fn()
        self.assertTrue(
            os.path.isdir(path),
            f"Entry point callable returned non-directory: {path!r}",
        )

    def test_import_nodus_mcp_resolves_and_executes(self):
        """`import "nodus-mcp"` resolves through the entry-point and runs."""
        import nodus
        from nodus.runtime.module_loader import ModuleLoader

        vm = nodus.VM([], {}, code_locs=[], source_path="test.nd")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            loader = ModuleLoader(
                project_root=tempfile.gettempdir(),
                vm=vm,
            )
            loader.load_module_from_source(
                'import "nodus-mcp" as mcp\nprint(mcp._version)',
                module_name="test.nd",
            )
        stdout = out.getvalue().strip()
        self.assertEqual(
            stdout, "0.1.0.dev0",
            f"Expected version string from mcp._version, got: {stdout!r}\n"
            f"stderr: {err.getvalue()}",
        )


if __name__ == "__main__":
    unittest.main()
