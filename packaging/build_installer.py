"""Build the per-user Windows Setup EXE from an already verified onedir release.

The optional compiler bootstrap is portable, pinned and restricted to .build.
This script never installs PersonalManagement, launches it or stops processes.
Official compiler: https://github.com/jrsoftware/issrc/releases/tag/is-7_1_0
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow import runtime_directory, version_directory

ROOT = Path(__file__).resolve().parents[1]
COMPILER_VERSION = "7.1.0"
COMPILER_URL = "https://github.com/jrsoftware/issrc/releases/download/is-7_1_0/innosetup-7.1.0-x64.exe"
COMPILER_SHA256 = "0362a383ed217d4c4239b5933866dd96d3eb2102737da92f80f6057a4b40df2f"
TOOLS = ROOT / ".build" / "tools" / "inno-setup-7.1.0"
INSTALLER_APP_ID = "{EA7A3A48-A445-44C0-A56D-3F5275DD323E}"
EXES = ("PersonalManagement.exe", "PersonalManagementService.exe", "PersonalManagementCodex.exe")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plain_path(path: Path) -> Path:
    path = path.absolute()
    for entry in (path, *path.parents):
        if entry.is_symlink() or entry.is_junction():
            raise ValueError(f"Linked build paths are not accepted: {entry}")
    return path.resolve()


def source_version() -> str:
    source = (ROOT / "src/management/__init__.py").read_text("utf-8-sig")
    match = re.search(r'__version__\s*=\s*[\"\x27](\d+\.\d+\.\d+)[\"\x27]', source)
    if not match:
        raise ValueError("Cannot determine the source version")
    return match[1]


def pe_version(path: Path) -> str:
    """Read VERSIONINFO without executing any release executable."""
    api = ctypes.WinDLL("version", use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    api.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
    api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = ctypes.c_int
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    api.VerQueryValueW.restype = ctypes.c_int
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ValueError(f"Missing PE version resource: {path.name}")
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not api.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise ValueError(f"Invalid PE version resource: {path.name}")
    fields = struct.unpack("<13I", ctypes.string_at(pointer, 52))
    if fields[0] != 0xFEEF04BD:
        raise ValueError(f"Invalid PE version signature: {path.name}")
    high, low = fields[2:4]
    return ".".join(map(str, (high >> 16, high & 65535, low >> 16, low & 65535)))


def validate_package(package: Path, version: str) -> list[dict]:
    package = plain_path(package)
    folder = version_directory(version, ROOT).resolve()
    if package not in {folder, folder / 'app'}:
        raise ValueError("Installer input must be the version's app directory or a legacy runtime")
    required = (*EXES, "_internal/management/assets/app-icon.ico", "使用说明.txt", "详细使用说明.md")
    for name in required:
        if not (package / name).is_file():
            raise ValueError(f"Missing release input: {name}")
    forbidden = {".codex", ".agents", ".git", ".venv", ".analysis", ".test-output", "auth.json", "runtime.json", "service.lock", "restore_pending.json", "restore_reconciled.json",
                 "upgrade-backups", "upgrade-state.json", "schema-upgrade.lock", "update_pending.json", "update_pending.json.new", "startup_failure.json", "startup_failure.json.new", "gui.lock", "tray.lock", "tray-status.json", "tray-status.json.new"}
    entries = []
    for path in sorted(package.rglob("*")):
        plain_path(path)
        relative = path.relative_to(package)
        parts = {part.lower() for part in relative.parts}
        if forbidden & parts or relative.parts[0].lower() in {"data", "backups", "blobs", "jobs", ".staging"}:
            raise ValueError(f"Private/runtime data in installer input: {relative}")
        if not path.is_file():
            continue
        if path.name.lower().startswith(".env") or re.search(r"\.(?:sqlite3?|db)(?:-|$)", path.name, re.I) or path.suffix.lower() in {".pem", ".key", ".pfx", ".p12"}:
            raise ValueError(f"Private/runtime file in installer input: {relative}")
        entries.append({"path": relative.as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)})
    for name in EXES:
        if pe_version(package / name) != version + ".0":
            raise ValueError(f"Release executable version mismatch: {name}")
    return entries


def verify_download(path: Path) -> None:
    if digest(path) != COMPILER_SHA256:
        raise ValueError("Official Inno Setup download SHA256 mismatch")
    quoted = "'" + str(path).replace("'", "''") + "'"
    command = "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; $s=Get-AuthenticodeSignature -LiteralPath " + quoted + "; [pscustomobject]@{status=[string]$s.Status;publisher=$s.SignerCertificate.Subject} | ConvertTo-Json -Compress"
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    environment = dict(os.environ)
    # A PowerShell 7 caller can export its incompatible module path to Windows
    # PowerShell 5. Let the child rebuild its own standard module search path.
    for key in tuple(environment):
        if key.upper() == "PSMODULEPATH":
            environment.pop(key)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    signature = json.loads(result.stdout)
    if signature.get("status") != "Valid" or not re.search(r"(?:^|, )O=Pyrsys B\.V\.(?:,|$)", signature.get("publisher") or ""):
        raise ValueError("Official compiler Authenticode validation failed: " + json.dumps(signature))


def bootstrap_compiler() -> Path:
    directory = plain_path(TOOLS)
    directory.mkdir(parents=True, exist_ok=True)
    download = directory / "innosetup-7.1.0-x64.exe"
    if not download.exists():
        temporary = directory / "download.partial"
        with urllib.request.urlopen(COMPILER_URL, timeout=60) as response, temporary.open("wb") as target:
            shutil.copyfileobj(response, target)
        if digest(temporary) != COMPILER_SHA256:
            raise ValueError("Official Inno Setup download SHA256 mismatch")
        temporary.replace(download)
    verify_download(download)
    compiler = directory / "compiler" / "ISCC.exe"
    if not compiler.is_file():
        subprocess.run([str(download), "/PORTABLE=1", "/CURRENTUSER", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/NOICONS", "/DIR=" + str(compiler.parent), "/LOG=" + str(directory / "bootstrap.log")], check=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    return compiler


def compiler_path(explicit: Path | None, bootstrap: bool) -> tuple[Path, str]:
    if explicit:
        path = plain_path(explicit)
    elif bootstrap:
        path = bootstrap_compiler()
    else:
        path = TOOLS / "compiler" / "ISCC.exe"
        if not path.is_file():
            found = shutil.which("ISCC.exe")
            if not found:
                raise ValueError("Inno Setup is missing; pass --bootstrap-compiler or --iscc PATH")
            path = Path(found)
    result = subprocess.run([str(path), "--version"], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    reported = result.stdout.strip()
    if not re.search(r"(?<!\d)" + re.escape(COMPILER_VERSION) + r"(?![.\d])", reported):
        raise ValueError("Use the pinned Inno Setup " + COMPILER_VERSION + "; found " + reported)
    return path, reported


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=None, help="Must match source and all release executable versions")
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--bootstrap-compiler", action="store_true", help="Download and portably unpack the pinned compiler under .build/tools")
    parser.add_argument("--compiler-only", action="store_true", help="Prepare/check the compiler without compiling a release")
    parser.add_argument("--check", action="store_true", help="Validate input and compile the script without creating an installer")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("The installer build requires Windows")
    compiler, compiler_version = compiler_path(args.iscc, args.bootstrap_compiler)
    if args.compiler_only:
        print(json.dumps({"compiler": str(compiler), "version": compiler_version, "sha256": digest(compiler)}))
        return
    version = args.version or source_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or version != source_version():
        parser.error("Version must be a three-part numeric version matching the source")
    package = runtime_directory(version, ROOT)
    entries = validate_package(package, version)
    script = ROOT / "packaging" / "installer.iss"
    output = version_directory(version, ROOT) / f"PersonalManagement-{version}-Setup-x64.exe"
    if output.exists() and not args.check:
        raise FileExistsError(f"Installer already exists; preserve or move it before rebuilding: {output}")
    log = ROOT / '.build' / 'reports' / 'build' / version / ('installer-check.log' if args.check else 'installer.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    # A compile check must never give the compiler a write path to a release.
    compiler_output = log.parent / 'compile-check' if args.check else output.parent
    compiler_output.mkdir(parents=True, exist_ok=True)
    command = [str(compiler), "/Qp", "/DAppVersion=" + version, "/DPackageDir=" + str(package), "/DOutputDir=" + str(compiler_output)]
    if args.check:
        command.append("/O-")
    command.append(str(script))
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run(command, check=True, stdout=stream, stderr=subprocess.STDOUT, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
    # A moving onedir payload must never receive a successful build receipt.
    if validate_package(package, version) != entries:
        raise ValueError("Release payload changed while the installer was compiling; do not publish its output")
    report = {"version": version, "app_id": INSTALLER_APP_ID, "compiler_version": compiler_version, "compiler_sha256": digest(compiler), "compiler_bootstrap_sha256": COMPILER_SHA256,
              "script_sha256": digest(script), "file_count": len(entries), "payload_bytes": sum(item["bytes"] for item in entries), "files": entries,
              "per_user": True, "data_included": False, "closes_applications": False, "checked_only": args.check}
    if not args.check:
        if not output.is_file() or pe_version(output) != version + ".0":
            raise ValueError("Installer output/version verification failed")
        report.update(installer=output.name, installer_bytes=output.stat().st_size, installer_sha256=digest(output))
        (output.with_suffix(".exe.sha256")).write_text(report["installer_sha256"] + "  " + output.name + "\n", encoding="ascii")
        (output.parent / 'SHA256SUMS.txt').write_text(report['installer_sha256'] + '  ' + output.name + '\n', encoding='ascii')
    report_path = log.with_suffix('.json') if args.check else output.parent / 'manifest.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
