"""The frozen application dispatches the isolated filesystem helper first."""

import subprocess
import sys
from pathlib import Path


def test_helper_dispatch_runs_before_application_imports():
    entrypoint = Path(__file__).resolve().parents[1] / "orbfarmer.py"
    harness = r'''
import runpy
import sys
import types

def helper_main(arguments):
    assert arguments == ["--pipe", "test-pipe", "--parent-pid", "10"]
    assert "orbfarmer" not in sys.modules
    assert "orbfarmer.config" not in sys.modules
    assert "settings" not in sys.modules
    return 0

elevation = types.ModuleType("_orbfarmer_elevation")
elevation.helper_main = helper_main
sys.modules["_orbfarmer_elevation"] = elevation
sys.argv = [sys.argv[1], "--filesystem-helper", "--pipe", "test-pipe", "--parent-pid", "10"]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", harness, str(entrypoint)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
