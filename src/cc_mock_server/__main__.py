"""Console-script entry point for `cc-mock` (see `pyproject.toml`
`[project.scripts]`) and `python -m cc_mock_server`."""

from __future__ import annotations

import sys

from cc_mock_server.cli import main as _cli_main


def main() -> None:
    sys.exit(_cli_main())


if __name__ == "__main__":
    main()
