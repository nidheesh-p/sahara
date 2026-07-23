# Installation

Sahara requires Python 3.11 or newer. The Python distribution is named
`sahara-memory`, but it installs the `sahara` command. Do not install the unrelated
OpenStack package named `sahara`.

## macOS Apple Silicon Installer

For public Apple Silicon releases, download the signed and notarized
`sahara-<version>-macos-arm64.pkg` from the GitHub release, then install it normally or
from Terminal:

```bash
sudo installer -pkg sahara-0.2.1-macos-arm64.pkg -target /
sahara --version
```

The installer includes Sahara's Python runtime and native dependencies. It installs
the bundle under `/Library/Application Support/Sahara/sahara/` and exposes
`/usr/local/bin/sahara`, so Git, Python, pip, and pipx are not required for this path.
At the end of a normal graphical install, Sahara opens first-run setup for the
current user. The setup flow lets the user choose folders to index, builds the first
index with consent, and offers to connect Claude Desktop when it is detected.

To relaunch setup later:

```bash
sahara-first-run
```

Upgrade by installing the newer package over the existing one. To uninstall the native
package while preserving data:

```bash
sudo rm -rf "/Library/Application Support/Sahara/sahara"
sudo rm -f /usr/local/bin/sahara
sudo pkgutil --forget io.github.nidheesh-p.sahara
```

The installer and uninstall commands preserve `~/.sahara`, configuration, indexes,
credentials, and model caches by default.

## Windows x64 Installer

For public Windows x64 releases, download the signed
`sahara-<version>-windows-x64-setup.exe` from the GitHub release, then run it normally
or install it quietly:

```powershell
.\sahara-0.2.1-windows-x64-setup.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
sahara --version
```

The installer includes Sahara's Python runtime and native dependencies. It installs
for the current user under `%LOCALAPPDATA%\Programs\Sahara` and adds that directory to
the user's `PATH`, so Git, Python, pip, and pipx are not required for this path.
At the end of a normal graphical install, Sahara offers to launch first-run setup.
The setup flow lets the user choose folders to index, builds the first index with
consent, and offers to connect Claude Desktop when it is detected. Quiet installs skip
the first-run launch.

To relaunch setup later:

```powershell
sahara first-run
```

Upgrade by installing the newer setup executable over the existing installation. To
uninstall the native package while preserving data:

```powershell
$uninstall = Join-Path $env:LOCALAPPDATA "Programs\Sahara\unins000.exe"
& $uninstall /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
```

The installer and uninstall commands preserve `%USERPROFILE%\.sahara`,
configuration, indexes, credentials, and model caches by default.

## Recommended: pipx

Sahara is a command-line application, so
[pipx](https://pipx.pypa.io/stable/installation/) is the recommended installer. It
creates an isolated environment while keeping the `sahara` command available in your
shell.

Install `pipx` once using your operating system's package manager. On macOS with
Homebrew:

```bash
brew install pipx
pipx ensurepath
```

On Windows:

```powershell
py -3.11 -m pip install --user pipx
py -3.11 -m pipx ensurepath
```

Open a new terminal after `pipx ensurepath`, then install Sahara:

```bash
pipx install "sahara-memory[search,mcp]"
sahara --version
```

`pipx` normally uses the Python interpreter it was installed with. If it reports that
Sahara requires a different Python version, point it to Python 3.11 or newer:

```bash
pipx install "sahara-memory[search,mcp]" --python python3.12
```

On Windows, select Python through the launcher:

```powershell
py -3.11 -m pipx install "sahara-memory[search,mcp]"
```

Upgrade or remove the isolated installation with:

```bash
pipx upgrade sahara-memory
pipx uninstall sahara-memory
```

## Virtual Environment Alternative

Use a normal virtual environment when `pipx` is unavailable or when developing
scripts that import Sahara.

macOS or Linux:

```bash
python3 -m venv ~/.venvs/sahara
source ~/.venvs/sahara/bin/activate
python -m pip install --upgrade pip
python -m pip install "sahara-memory[search,mcp]"
sahara --version
```

Windows PowerShell:

```powershell
py -3.11 -m venv "$env:USERPROFILE\.venvs\sahara"
& "$env:USERPROFILE\.venvs\sahara\Scripts\Activate.ps1"
python -m pip install --upgrade pip
python -m pip install "sahara-memory[search,mcp]"
sahara --version
```

Activate the virtual environment again before using `sahara` in a new terminal. The
Claude Desktop installer records the absolute Sahara executable path, so Claude can
continue launching it without shell activation.

## `externally-managed-environment`

Homebrew Python and many modern Linux distributions protect their managed interpreter
under [PEP 668](https://peps.python.org/pep-0668/). A direct system-level installation
may fail with:

```text
error: externally-managed-environment
```

This does not indicate a Sahara package failure. Install with `pipx` or inside a
virtual environment as shown above. Do not use `sudo pip`, modify Homebrew's Python, or
pass `--break-system-packages`; those approaches can damage the managed installation.

## First Index

The first `sahara index` downloads a local embedding model. The current download is
roughly 70 MB and may use additional disk space after extraction. A Hugging Face
warning about unauthenticated requests is informational; no account or token is
required.
