"""Build and verify Sahara's Windows x64 installer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_macos_bundle import PROJECT_FILE, project_version  # noqa: E402
from scripts.build_windows_bundle import PLATFORM_TAG, bundle_name, is_windows_x64  # noqa: E402

APP_ID = "{{7F9A2F1B-4E5D-4C5B-9E7C-51F6238B21F1}"
APP_NAME = "Sahara"
PUBLISHER = "Sahara"
DEFAULT_INSTALLER_ROOT = Path("dist") / "native-installers"
DEFAULT_SCRIPT_ROOT = Path("build") / "windows-installer"
DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"
INSTALL_LOCATION = r"%LOCALAPPDATA%\Programs\Sahara"
PATH_ENTRY = r"%LOCALAPPDATA%\Programs\Sahara"
PRESERVED_USER_DATA = r"%USERPROFILE%\.sahara"
FIRST_RUN_COMMAND = r"%LOCALAPPDATA%\Programs\Sahara\sahara.exe first-run"


@dataclass(frozen=True)
class WindowsInstallerArtifact:
    installer: Path
    checksum: Path
    manifest: Path
    script: Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path, checksum_path: Path) -> str:
    digest = sha256_file(path)
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def require_windows_x64(skip_platform_check: bool) -> None:
    if skip_platform_check:
        return
    if not is_windows_x64():
        raise SystemExit(
            "Windows x64 installers must be built on Windows x64. "
            "Pass --skip-platform-check only for metadata tests."
        )


def inno_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\")


def installer_name(version: str) -> str:
    return f"sahara-{version}-{PLATFORM_TAG}-setup.exe"


def installer_base_name(version: str) -> str:
    return installer_name(version).removesuffix(".exe")


def write_inno_script(
    *,
    bundle: Path,
    script: Path,
    output_root: Path,
    version: str,
) -> Path:
    executable = bundle / "sahara.exe"
    if not executable.is_file():
        raise ValueError(f"missing bundled executable: {executable}")

    script.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    script.write_text(
        f"""#define AppVersion "{version}"

[Setup]
AppId={APP_ID}
AppName={APP_NAME}
AppVersion={{#AppVersion}}
AppPublisher={PUBLISHER}
DefaultDirName={{localappdata}}\\Programs\\Sahara
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir={inno_path(output_root)}
OutputBaseFilename={installer_base_name(version)}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={APP_NAME}
ChangesEnvironment=yes

[Files]
Source: "{inno_path(bundle)}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{userprograms}}\\Sahara\\Sahara"; Filename: "{{app}}\\sahara.exe"; Parameters: "--help"

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{{code:UpdatedUserPath}}"; Check: NeedsPathUpdate

[Run]
Filename: "{{app}}\\sahara.exe"; Parameters: "--version"; Flags: runhidden
Filename: "{{app}}\\sahara.exe"; Parameters: "first-run"; Description: "Launch Sahara setup"; Flags: postinstall skipifsilent nowait

[UninstallDelete]
Type: filesandordirs; Name: "{{app}}"

[Code]
function CurrentUserPath(Param: string): string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Result) then
    Result := '';
end;

function PathContains(ExistingPath: string; Entry: string): Boolean;
begin
  Result := Pos(Lowercase(';' + Entry + ';'), Lowercase(';' + ExistingPath + ';')) > 0;
end;

function NeedsPathUpdate: Boolean;
begin
  Result := not PathContains(CurrentUserPath(''), ExpandConstant('{{app}}'));
end;

function UpdatedUserPath(Param: string): string;
var
  ExistingPath: string;
  Entry: string;
begin
  ExistingPath := CurrentUserPath('');
  Entry := ExpandConstant('{{app}}');
  if ExistingPath = '' then
    Result := Entry
  else if PathContains(ExistingPath, Entry) then
    Result := ExistingPath
  else
    Result := ExistingPath + ';' + Entry;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ExistingPath: string;
  Entry: string;
  UpdatedPath: string;
begin
  if CurUninstallStep = usUninstall then begin
    ExistingPath := CurrentUserPath('');
    Entry := ExpandConstant('{{app}}');
    UpdatedPath := ExistingPath;
    StringChangeEx(UpdatedPath, Entry + ';', '', True);
    StringChangeEx(UpdatedPath, ';' + Entry, '', True);
    if UpdatedPath = Entry then
      UpdatedPath := '';
    if UpdatedPath <> ExistingPath then
      RegWriteExpandStringValue(HKCU, 'Environment', 'Path', UpdatedPath);
  end;
end;
""",
        encoding="utf-8",
    )
    return script


def find_iscc() -> str:
    configured = os.environ.get("INNO_SETUP_COMPILER")
    if configured:
        return configured
    found = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("Inno Setup compiler ISCC.exe was not found.")


def compile_inno_script(script: Path, compiler: str | None = None) -> None:
    subprocess.run([compiler or find_iscc(), str(script)], check=True)


def find_signtool() -> str:
    configured = os.environ.get("WINDOWS_SIGNTOOL")
    if configured:
        return configured
    found = shutil.which("signtool.exe") or shutil.which("signtool")
    if found:
        return found
    kits_root = Path(os.environ.get("ProgramFiles(x86)", "")) / "Windows Kits" / "10" / "bin"
    for candidate in sorted(kits_root.glob("*/*/signtool.exe"), reverse=True):
        if candidate.is_file() and candidate.parent.name.lower() == "x64":
            return str(candidate)
    raise SystemExit("signtool.exe was not found.")


def write_certificate_from_base64(encoded: str, destination: Path) -> Path:
    destination.write_bytes(base64.b64decode(encoded))
    return destination


def sign_installer(
    installer: Path,
    *,
    certificate: Path,
    certificate_password: str,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
    signtool: str | None = None,
) -> None:
    subprocess.run(
        [
            signtool or find_signtool(),
            "sign",
            "/fd",
            "SHA256",
            "/tr",
            timestamp_url,
            "/td",
            "SHA256",
            "/f",
            str(certificate),
            "/p",
            certificate_password,
            str(installer),
        ],
        check=True,
    )


def write_manifest(
    destination: Path,
    *,
    installer: Path,
    bundle: Path,
    checksum: str,
    signed: bool,
) -> Path:
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform_tag": PLATFORM_TAG,
        "installer": installer.name,
        "installer_sha256": checksum,
        "bundle": bundle.name,
        "install_location": INSTALL_LOCATION,
        "path_entry": PATH_ENTRY,
        "first_run_command": FIRST_RUN_COMMAND,
        "launches_first_run_after_gui_install": True,
        "per_user": True,
        "quiet_install_args": "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES",
        "quiet_uninstall_args": "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES",
        "signed": signed,
        "preserves_user_data": [PRESERVED_USER_DATA],
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }
    destination.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return destination


def build_windows_installer(
    bundle: Path,
    installer_root: Path = DEFAULT_INSTALLER_ROOT,
    *,
    script_root: Path = DEFAULT_SCRIPT_ROOT,
    sign: bool = False,
    certificate: Path | None = None,
    certificate_password: str | None = None,
    certificate_base64: str | None = None,
    timestamp_url: str = DEFAULT_TIMESTAMP_URL,
    skip_platform_check: bool = False,
) -> WindowsInstallerArtifact:
    require_windows_x64(skip_platform_check)
    version = project_version(PROJECT_FILE)
    installer = installer_root / installer_name(version)
    checksum = installer_root / f"{installer.name}.sha256"
    manifest = installer_root / f"sahara-{version}-{PLATFORM_TAG}-installer-manifest.json"
    script = script_root / "sahara_windows_x64.iss"
    installer_root.mkdir(parents=True, exist_ok=True)

    if sign and not ((certificate or certificate_base64) and certificate_password):
        raise ValueError("Windows signing requires a certificate and certificate password")

    write_inno_script(bundle=bundle, script=script, output_root=installer_root, version=version)
    compile_inno_script(script)

    with tempfile.TemporaryDirectory(prefix="sahara-windows-signing-") as temp_dir:
        cert_path = certificate
        if certificate_base64:
            cert_path = write_certificate_from_base64(
                certificate_base64,
                Path(temp_dir) / "windows-codesign.pfx",
            )
        if sign:
            sign_installer(
                installer,
                certificate=cert_path or Path(),
                certificate_password=certificate_password or "",
                timestamp_url=timestamp_url,
            )

    digest = write_checksum(installer, checksum)
    write_manifest(
        manifest,
        installer=installer,
        bundle=bundle,
        checksum=digest,
        signed=sign,
    )
    verify_windows_installer(installer_root, installer.name)
    return WindowsInstallerArtifact(
        installer=installer,
        checksum=checksum,
        manifest=manifest,
        script=script,
    )


def verify_windows_installer(installer_root: Path, installer_file: str) -> None:
    installer = installer_root / installer_file
    checksum = installer_root / f"{installer_file}.sha256"
    manifest = installer_root / installer_file.replace("-setup.exe", "-installer-manifest.json")
    missing = [path for path in (installer, checksum, manifest) if not path.is_file()]
    if missing:
        raise ValueError(
            "missing Windows installer file(s): "
            + ", ".join(str(path) for path in missing)
        )

    expected_digest = checksum.read_text(encoding="utf-8").split()[0]
    actual_digest = sha256_file(installer)
    if actual_digest != expected_digest:
        raise ValueError(
            f"checksum mismatch for {installer.name}: {actual_digest} != {expected_digest}"
        )

    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    if manifest_data.get("platform_tag") != PLATFORM_TAG:
        raise ValueError(f"manifest platform mismatch: {manifest_data.get('platform_tag')!r}")
    if manifest_data.get("installer") != installer.name:
        raise ValueError(f"manifest installer mismatch: {manifest_data.get('installer')!r}")
    if manifest_data.get("installer_sha256") != actual_digest:
        raise ValueError("manifest checksum does not match installer")
    if manifest_data.get("install_location") != INSTALL_LOCATION:
        raise ValueError("manifest install location does not match supported path")
    if PRESERVED_USER_DATA not in manifest_data.get("preserves_user_data", []):
        raise ValueError("manifest does not document preserved user data")


def main() -> None:
    version = project_version(PROJECT_FILE)
    default_bundle = Path("dist") / "native" / bundle_name(version)

    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=default_bundle)
    parser.add_argument("--installer-root", type=Path, default=DEFAULT_INSTALLER_ROOT)
    parser.add_argument("--script-root", type=Path, default=DEFAULT_SCRIPT_ROOT)
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Authenticode-sign the installer using a PFX certificate.",
    )
    parser.add_argument(
        "--certificate",
        type=Path,
        default=(
            Path(os.environ["WINDOWS_CODESIGN_CERTIFICATE_PATH"])
            if os.environ.get("WINDOWS_CODESIGN_CERTIFICATE_PATH")
            else None
        ),
    )
    parser.add_argument(
        "--certificate-base64",
        default=os.environ.get("WINDOWS_CODESIGN_CERTIFICATE_BASE64"),
        help="Base64-encoded PFX certificate used when --certificate is not provided.",
    )
    parser.add_argument(
        "--certificate-password",
        default=os.environ.get("WINDOWS_CODESIGN_CERTIFICATE_PASSWORD"),
    )
    parser.add_argument(
        "--timestamp-url",
        default=os.environ.get("WINDOWS_CODESIGN_TIMESTAMP_URL") or DEFAULT_TIMESTAMP_URL,
    )
    parser.add_argument(
        "--skip-platform-check",
        action="store_true",
        help="Allow non-Windows-x64 execution for metadata tests.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing Windows installer files instead of building an installer.",
    )
    args = parser.parse_args()

    expected_name = installer_name(version)
    if args.verify_only:
        verify_windows_installer(args.installer_root, expected_name)
        print(f"Verified Windows installer {expected_name}")
        return

    artifact = build_windows_installer(
        args.bundle,
        args.installer_root,
        script_root=args.script_root,
        sign=args.sign,
        certificate=args.certificate,
        certificate_base64=args.certificate_base64,
        certificate_password=args.certificate_password,
        timestamp_url=args.timestamp_url,
        skip_platform_check=args.skip_platform_check,
    )
    print(f"Built {artifact.installer}")
    print(f"Wrote {artifact.checksum}")
    print(f"Wrote {artifact.manifest}")
    print(f"Wrote {artifact.script}")


if __name__ == "__main__":
    main()
