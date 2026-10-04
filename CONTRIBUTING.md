# Contributing to Orbfarmer

Use Windows and Python 3.12 with Tcl/Tk. Install `requirements-build.txt` in a virtual environment, make your changes on a branch, and run:

```powershell
python -m pytest -q
python build.py
```

Keep timer windows detectable, preserve game-specific relative paths, and treat downloaded metadata as data. Keep low-level file creation exclusive. Existing executables default to Run; replace them only after the user selects Replace. Preserve unrelated sidecars and restore original files if replacement fails. Cleanup must delete only unchanged files the current session owns. Update artwork ownership and cleanup tests when changing generated files.

Local simulation is the default. Write to a Steam library only after the user selects Steam library + ACF and confirms the displayed paths. Preserve existing manifests and use their validated installation directories. Validate path containment and manifest values before writing or launching.

For protected Steam-library operations, check the selected filesystem operations before requesting native Windows elevation. Keep timer launch in the original user process. The elevated helper must dispatch before package or settings imports, accept one immutable plan, and preserve ownership, rollback, and containment checks through cleanup. Handle UAC cancellation without launching or claiming setup succeeded.

Pull requests should explain the user-visible change and how it was verified. Report bugs with the game, selected mode, Windows version, and relevant error text; omit account tokens and personal data.

Retain the original attribution and license notices.
