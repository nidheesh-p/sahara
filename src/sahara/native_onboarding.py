"""Native installer first-run helpers."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path


def _nonempty_lines(output: str) -> list[Path]:
    return [Path(line.strip()) for line in output.splitlines() if line.strip()]


def choose_folders_macos() -> list[Path]:
    script = [
        'set chosenFolders to choose folder with prompt "Choose folders for Sahara to index" '
        "multiple selections allowed",
        'set output to ""',
        "repeat with folderItem in chosenFolders",
        "set output to output & POSIX path of folderItem & linefeed",
        "end repeat",
        "return output",
    ]
    result = subprocess.run(
        ["osascript", *sum((["-e", line] for line in script), [])],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return _nonempty_lines(result.stdout)


def choose_folders_windows() -> list[Path]:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$folders = New-Object System.Collections.Generic.List[string]
do {
  $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
  $dialog.Description = "Choose a folder for Sahara to index"
  $dialog.ShowNewFolderButton = $true
  if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
    break
  }
  $folders.Add($dialog.SelectedPath)
  $again = [System.Windows.Forms.MessageBox]::Show(
    "Add another folder?",
    "Sahara Setup",
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question
  )
} while ($again -eq [System.Windows.Forms.DialogResult]::Yes)
$folders
"""
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return []
    return _nonempty_lines(result.stdout)


def choose_folders_for_platform(platform_name: str | None = None) -> list[Path]:
    system = platform_name or platform.system()
    if system == "Darwin":
        return choose_folders_macos()
    if system == "Windows":
        return choose_folders_windows()
    return []
