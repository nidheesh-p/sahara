# Package Manager Publishing

Sahara's stable native artifacts can be republished through Homebrew and WinGet after
the matching GitHub release assets are present. Package-manager entries must always
source their versions and checksums from the signed release artifacts rather than from
short-lived workflow artifacts.

## Homebrew

Use a dedicated tap such as `nidheesh-p/homebrew-sahara`. The formula should install
the signed and notarized macOS `.pkg`, not the Python package and not the unrelated
OpenStack `sahara` package.

Formula template:

```ruby
class Sahara < Formula
  desc "Local-first semantic search and MCP memory CLI"
  homepage "https://github.com/nidheesh-p/sahara"
  url "https://github.com/nidheesh-p/sahara/releases/download/v0.3.0/sahara-0.3.0-macos-arm64.pkg"
  sha256 "<sha256 from sahara-0.3.0-macos-arm64.pkg.sha256>"
  license "MIT"

  depends_on arch: :arm64

  def install
    pkg = buildpath/"sahara-0.3.0-macos-arm64.pkg"
    system "/usr/sbin/pkgutil", "--expand-full", pkg, "expanded"
    bin.install "expanded/Payload/Library/Application Support/Sahara/sahara/sahara"
    bin.install "expanded/Payload/Library/Application Support/Sahara/sahara/sahara-first-run"
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/sahara --version")
  end
end
```

After publishing the tap, document user commands:

```bash
brew tap nidheesh-p/sahara
brew install sahara
brew upgrade sahara
brew uninstall sahara
```

Homebrew uninstall removes the managed executable but preserves `~/.sahara` by default.

## WinGet

Submit a manifest to `microsoft/winget-pkgs` only after the Windows release asset is
published and signed. The manifest URL must point to the release `.exe`, and the
installer hash must match the release checksum.

Installer manifest template:

```yaml
PackageIdentifier: nidheesh-p.Sahara
PackageVersion: 0.3.0
InstallerType: inno
Scope: user
InstallModes:
  - interactive
  - silent
InstallerSwitches:
  Silent: /VERYSILENT /NORESTART /SUPPRESSMSGBOXES
  SilentWithProgress: /SILENT /NORESTART /SUPPRESSMSGBOXES
UpgradeBehavior: install
Installers:
  - Architecture: x64
    InstallerUrl: https://github.com/nidheesh-p/sahara/releases/download/v0.3.0/sahara-0.3.0-windows-x64-setup.exe
    InstallerSha256: <sha256 from sahara-0.3.0-windows-x64-setup.exe.sha256>
ManifestType: installer
ManifestVersion: 1.9.0
```

Document user commands once the manifest is accepted:

```powershell
winget install --id nidheesh-p.Sahara
winget upgrade --id nidheesh-p.Sahara
winget uninstall --id nidheesh-p.Sahara
```

The Inno Setup uninstaller removes the managed runtime and preserves
`%USERPROFILE%\.sahara` by default.

## Release Checklist

For every package-manager update:

1. Confirm the GitHub release contains the signed installer, checksum, manifest,
   dependency inventory, and smoke log for the target platform.
2. Verify the checksum locally before copying it into the package-manager manifest.
3. Install from the package manager on a clean machine.
4. Run `sahara --version`, `sahara setup`, `sahara index`, `sahara search`, and stdio
   MCP startup.
5. Test upgrade and uninstall while confirming user data under `~/.sahara` or
   `%USERPROFILE%\.sahara` remains present.
