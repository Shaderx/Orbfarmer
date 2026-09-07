# Contributing to Orbfarmer

Use Windows and Python 3.12 with Tcl/Tk. Install `requirements-build.txt` in a virtual environment, make your changes on a branch, and run:

```powershell
python -m pytest -q
python build.py
```

Keep timer windows detectable, preserve game-specific relative paths, and treat downloaded metadata as data. Never overwrite an existing executable or delete a file the current session does not own. Update artwork ownership and cleanup tests when changing generated files.

Pull requests should explain the user-visible change and how it was verified. Report bugs with the game, selected mode, Windows version, and relevant error text; omit account tokens and personal data.

Retain the original attribution and license notices.
