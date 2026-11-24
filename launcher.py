"""
Tiny launcher: double-click it to open the Highlight Clipper window, no PowerShell needed.
It is built into HighlightClipper.exe with PyInstaller (see the build steps).
Keep the .exe in the same folder as clipper_gui.py (C:\\highlight_clipper).
"""
import ctypes
import subprocess
import sys
from pathlib import Path


def find_folder():
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    for folder in (here, here.parent, Path(r"C:\highlight_clipper")):
        if (folder / "clipper_gui.py").is_file():
            return folder
    return None


def message(text):
    ctypes.windll.user32.MessageBoxW(0, text, "Highlight Clipper", 0x10)


def main():
    folder = find_folder()
    if not folder:
        message("Could not find clipper_gui.py.\nKeep HighlightClipper.exe inside C:\\highlight_clipper.")
        return
    gui = str(folder / "clipper_gui.py")
    for launcher in ("pyw", "py"):  # pyw = no black console window
        try:
            subprocess.Popen([launcher, "-3.12", gui], cwd=folder)
            return
        except FileNotFoundError:
            continue
    message("Python 3.12 was not found.\nInstall it with:  winget install Python.Python.3.12")


if __name__ == "__main__":
    main()
