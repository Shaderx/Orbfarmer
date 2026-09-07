#!/usr/bin/env python3
"""
build.py – Automates compiling orbfarmer.

This script:
1. Resolves the latest version from git tags.
2. Writes the version to orbfarmer/_version.py.
3. Invokes PyInstaller with the proper parameters to build a single executable.
"""

import re
import sys
import subprocess
import os
import shutil
from pathlib import Path


def get_git_version() -> str:
    """Retrieve an exact Git tag, falling back to checked-in metadata."""
    project_root = Path(__file__).resolve().parent
    try:
        res = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=project_root,
        )
        tag = res.stdout.strip()
        if tag:
            return tag.lstrip("v")
    except Exception:
        pass

    version_file = project_root / "orbfarmer" / "_version.py"
    match = re.search(
        r'^VERSION\s*=\s*["\']([^"\']+)["\']',
        version_file.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match or match.group(1) == "0.0.0":
        raise RuntimeError("Refusing to build without valid version metadata")
    return match.group(1)


def main():
    project_root = Path(__file__).resolve().parent
    # 1. Resolve version
    version = get_git_version()
    print(f"[*] Resolving version from Git: {version}")

    # 2. Write to orbfarmer/_version.py
    version_file = project_root / "orbfarmer" / "_version.py"
    print(f"[*] Baking version into: {version_file}")
    version_file.write_text(f'VERSION = "{version}"\n', encoding="utf-8")

    # Stage Tcl/Tk scripts in the workspace so native library probes can read them
    # in restricted build environments. PyInstaller's Tk hook bundles these files.
    import _tkinter
    tcl_source = Path(sys.base_prefix) / "tcl"
    tcl_staging = project_root / "build-local" / "tcl-runtime"
    build_env = os.environ.copy()
    for env_name, directory in (("TCL_LIBRARY", f"tcl{_tkinter.TCL_VERSION}"),
                                ("TK_LIBRARY", f"tk{_tkinter.TK_VERSION}")):
        source = tcl_source / directory
        if not source.is_dir():
            raise RuntimeError(f"A complete Python Tcl/Tk installation is required: {source}")
        shutil.copytree(source, tcl_staging / directory, dirs_exist_ok=True)
        build_env[env_name] = str(tcl_staging / directory)
    subprocess.run([sys.executable, "-c", "import tkinter; tkinter.Tcl()"], env=build_env, check=True)

    # 3. Build with the PyInstaller installed in this exact Python environment.
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "Orbfarmer",
        "--noconsole",
        "--exclude-module",
        "settings",
        "--distpath",
        "dist-local",
        "--workpath",
        "build-local",
        "--specpath",
        "build-local",
        "orbfarmer.py",
    ]

    print(f"[*] Executing PyInstaller command:\n    {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, cwd=project_root, env=build_env)
        print("\n[OK] Build completed successfully! Check the 'dist-local' directory.")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] PyInstaller execution failed with exit code: {e.returncode}")
        sys.exit(e.returncode)


if __name__ == "__main__":
    main()
