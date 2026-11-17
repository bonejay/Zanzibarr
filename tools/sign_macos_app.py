#!/usr/bin/env python3
"""Sign a built Zanzibarr.app for Developer ID distribution.

The Flutter/Xcode build signs the app with an Apple Development identity and
leaves the vendored Mpv.framework ad-hoc signed. This script replaces those
signatures inside-out, seals every framework's resources, requires a Developer
ID provisioning profile for the sandbox/keychain entitlements, verifies
codesign, and reports Gatekeeper's notarization status.

Build and export first:
    xcodebuild -workspace macos/Runner.xcworkspace -scheme Runner \
        -configuration Release -archivePath build/macos/Zanzibarr.xcarchive \
        -allowProvisioningUpdates archive
    xcodebuild -exportArchive -archivePath build/macos/Zanzibarr.xcarchive \
        -exportPath build/macos/DeveloperID \
        -exportOptionsPlist macos/ExportOptions.developer-id.plist \
        -allowProvisioningUpdates

Then sign the exported app:
    python3 tools/sign_macos_app.py
    python3 tools/sign_macos_app.py path/to/zanzibarr.app

Override the certificate with ZANZIBARR_MACOS_IDENTITY when needed.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP = REPO_ROOT / "build/macos/DeveloperID/zanzibarr.app"
DEFAULT_IDENTITY = "Developer ID Application: Enver Anil Karakoc (8665FDLXA6)"
TEAM_ID = "8665FDLXA6"
BUNDLE_SUFFIXES = {".app", ".appex", ".bundle", ".framework", ".xpc"}
MACH_O_MAGIC = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
}


def run(cmd, *, capture=False):
    proc = subprocess.run(cmd, capture_output=capture, text=capture)
    if proc.returncode != 0:
        detail = proc.stderr if capture else ""
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{detail}")
    return proc


def is_mach_o(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACH_O_MAGIC


def bundle_executable(bundle):
    """Locate a bundle's main executable, which must be signed as a bundle."""
    if bundle.suffix == ".framework":
        plist_path = bundle / "Versions/Current/Resources/Info.plist"
        executable_dir = bundle / "Versions/Current"
    else:
        plist_path = bundle / "Contents/Info.plist"
        executable_dir = bundle / "Contents/MacOS"
    try:
        with plist_path.open("rb") as handle:
            name = plistlib.load(handle)["CFBundleExecutable"]
    except (OSError, KeyError, plistlib.InvalidFileException):
        return None
    executable = executable_dir / name
    return executable.resolve() if executable.exists() else None


def code_items(app):
    """Return standalone binaries and nested bundles, each deepest-first."""
    mach_o = set()
    bundles = set()
    for root, dirs, files in os.walk(app, followlinks=False):
        root_path = Path(root)
        for name in dirs:
            path = root_path / name
            if path.suffix in BUNDLE_SUFFIXES and not path.is_symlink():
                bundles.add(path.resolve())
        for name in files:
            path = root_path / name
            if is_mach_o(path):
                mach_o.add(path.resolve())

    # Re-signing a framework's main executable as a loose Mach-O before its
    # companion dylibs breaks the existing resource envelope on macOS 27.
    # These executables are signed exactly once, at their bundle stage.
    for bundle in [*bundles, app.resolve()]:
        executable = bundle_executable(bundle)
        if executable is not None:
            mach_o.discard(executable)

    deepest = lambda paths: sorted(paths, key=lambda path: len(path.parts), reverse=True)
    return deepest(mach_o), deepest(bundles)


def current_entitlements(app, bundle_id):
    """Read Xcode's expanded entitlements, falling back to Release.plist."""
    proc = subprocess.run(
        ["codesign", "-d", "--entitlements", ":-", str(app)],
        capture_output=True,
        text=True,
    )
    entitlements = None
    if proc.returncode == 0 and proc.stdout.lstrip().startswith("<?xml"):
        try:
            entitlements = plistlib.loads(proc.stdout.encode())
        except plistlib.InvalidFileException:
            entitlements = None
    if entitlements is None:
        with (REPO_ROOT / "macos/Runner/Release.entitlements").open("rb") as handle:
            entitlements = plistlib.load(handle)

    # Development-only debugging must never leak into a distribution build.
    entitlements.pop("com.apple.security.get-task-allow", None)
    entitlements["com.apple.application-identifier"] = f"{TEAM_ID}.{bundle_id}"
    entitlements["com.apple.developer.team-identifier"] = TEAM_ID

    prefix = f"{TEAM_ID}."
    groups = entitlements.get("keychain-access-groups")
    if isinstance(groups, list):
        entitlements["keychain-access-groups"] = [
            item.replace("$(AppIdentifierPrefix)", prefix) if isinstance(item, str) else item
            for item in groups
        ]
    return entitlements


def validate_distribution_profile(app, entitlements):
    """Reject missing, expired, development-only or mismatched profiles."""
    profile = app / "Contents/embedded.provisionprofile"
    if not profile.is_file():
        raise RuntimeError(
            "Developer ID provisioning profile is required for "
            "keychain-access-groups; export the archive with "
            "macos/ExportOptions.developer-id.plist first"
        )
    proc = run(["security", "cms", "-D", "-i", str(profile)], capture=True)
    try:
        profile_data = plistlib.loads(proc.stdout.encode())
    except plistlib.InvalidFileException as error:
        raise RuntimeError("embedded provisioning profile is unreadable") from error
    if profile_data.get("ProvisionsAllDevices") is not True:
        raise RuntimeError(
            "embedded provisioning profile is development-only; "
            "a Developer ID profile with ProvisionsAllDevices is required"
        )
    if TEAM_ID not in profile_data.get("TeamIdentifier", []):
        raise RuntimeError("embedded provisioning profile belongs to another team")
    expiration = profile_data.get("ExpirationDate")
    if not isinstance(expiration, datetime):
        raise RuntimeError("embedded provisioning profile has no expiration date")
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    if expiration <= datetime.now(timezone.utc):
        raise RuntimeError("embedded provisioning profile has expired")

    profile_entitlements = profile_data.get("Entitlements", {})
    application_id = entitlements["com.apple.application-identifier"]
    profile_application_id = profile_entitlements.get("com.apple.application-identifier")
    if not isinstance(profile_application_id, str) or not fnmatchcase(
        application_id, profile_application_id
    ):
        raise RuntimeError("provisioning profile does not cover the application identifier")

    profile_groups = profile_entitlements.get("keychain-access-groups", [])
    for group in entitlements.get("keychain-access-groups", []):
        if not any(fnmatchcase(group, pattern) for pattern in profile_groups):
            raise RuntimeError(
                f"provisioning profile does not cover keychain group: {group}"
            )


def main():
    app = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP).resolve()
    identity = os.environ.get("ZANZIBARR_MACOS_IDENTITY", DEFAULT_IDENTITY)
    if not app.is_dir():
        sys.exit(f"app bundle not found: {app}")

    with (app / "Contents/Info.plist").open("rb") as handle:
        bundle_id = plistlib.load(handle)["CFBundleIdentifier"]

    identities = run(
        ["security", "find-identity", "-v", "-p", "codesigning"], capture=True
    ).stdout
    if identity not in identities:
        sys.exit(f"codesigning identity not available: {identity}")

    entitlements = current_entitlements(app, bundle_id)
    validate_distribution_profile(app, entitlements)
    mach_o_files, bundles = code_items(app)

    def sign(path, *, app_entitlements=None):
        cmd = [
            "codesign",
            "--force",
            "--options",
            "runtime",
            "--timestamp",
            "--sign",
            identity,
        ]
        if app_entitlements is not None:
            cmd.extend(["--entitlements", str(app_entitlements)])
        cmd.append(str(path))
        run(cmd)

    # Sign binaries before their containing bundles. Signing a framework again
    # at the bundle stage seals Info.plist and creates CodeResources.
    for path in mach_o_files:
        sign(path)
    for path in bundles:
        sign(path)

    with tempfile.NamedTemporaryFile(suffix=".entitlements", delete=False) as handle:
        entitlements_path = Path(handle.name)
    try:
        with entitlements_path.open("wb") as handle:
            plistlib.dump(entitlements, handle)
        sign(app, app_entitlements=entitlements_path)
    finally:
        entitlements_path.unlink(missing_ok=True)

    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    gatekeeper = subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)],
        capture_output=True,
        text=True,
    )
    gatekeeper_output = (gatekeeper.stdout + gatekeeper.stderr).strip()
    if gatekeeper.returncode != 0:
        if "Unnotarized Developer ID" not in gatekeeper_output:
            raise RuntimeError(f"Gatekeeper rejected the app:\n{gatekeeper_output}")
        print("Developer ID signature verified; notarization is still pending.")
    else:
        print(gatekeeper_output)
    print(f"Developer ID signature verified: {app}")


if __name__ == "__main__":
    main()
