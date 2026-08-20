#!/usr/bin/env python3
"""Run mypy against services/api using that project's own venv, instead of a
separately-pinned isolated environment. This keeps type-checking pinned to the exact
FastAPI version declared in services/api/pyproject.toml, with no second, drift-prone
copy of that version bound to keep in sync."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    api_dir = repo_root / "services" / "api"
    venv_dir = api_dir / ".venv"

    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    if not venv_python.exists():
        if sys.platform == "win32":
            create_cmd = "python -m venv services\\api\\.venv"
            install_cmd = '.venv\\Scripts\\pip install -e "services\\api[dev]"'
        else:
            create_cmd = "python -m venv services/api/.venv"
            install_cmd = 'pip install -e "services/api[dev]"'
        print(
            f"error: no venv found at {venv_python}\n"
            "Create it first, then install the project in dev mode:\n"
            f"  {create_cmd}\n"
            f"  {install_cmd}",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run([str(venv_python), "-m", "mypy", "app"], cwd=api_dir)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
