"""Install the native CI artifact in an isolated location and exercise its backend."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from smoke import DESKTOP, SUFFIX, qualify


TARGETS = {
    "Linux": ("deb", "*.deb", ".deb"),
    "Windows": ("nsis", "*.exe", ".exe"),
    "Darwin": ("dmg", "*.dmg", ".dmg"),
}
DEFAULT_BUNDLE = DESKTOP / "src-tauri/target/release/bundle"


def exactly_one(paths, label):
    candidates = sorted(paths)
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one {label}; found {len(candidates)}.")
    return candidates[0]


def select_artifact(system, bundle, explicit=None):
    directory, pattern, suffix = TARGETS[system]
    if explicit is None:
        return exactly_one((file for file in (bundle / directory).glob(pattern) if file.is_file()),
                           f"{suffix} installer in {bundle / directory}").resolve(strict=True)
    artifact = explicit.resolve(strict=True)
    if not artifact.is_file() or artifact.suffix.lower() != suffix:
        raise RuntimeError(f"Choose a native {suffix} installer.")
    return artifact


def linux_install(artifact, scratch):
    """Unpack and register the Debian payload in a dedicated dpkg root."""
    root = scratch / "installed-root"
    database = root / "var/lib/dpkg"
    database.mkdir(parents=True)
    status = database / "status"
    status.touch()
    logdir = root / "var/log"
    logdir.mkdir()
    subprocess.run(["dpkg", f"--root={root}", f"--log={logdir / 'dpkg.log'}", "--force-not-root",
                    "--no-triggers", "--unpack", artifact], check=True, timeout=180)
    if "Status: install ok unpacked" not in status.read_text("utf-8"):
        raise RuntimeError("The Debian package was not registered in the temporary root.")
    if not any(path.is_file() for path in (root / "usr/bin").glob("penstock*")):
        raise RuntimeError("The Debian payload did not install the Penstock launcher under usr/bin.")
    return root / "usr/lib"


def nsis_command(executable, argument, destination, cwd):
    # NSIS consumes everything after /D= or _?= as a single path. It must come last and stay unquoted.
    if any(char in str(destination) for char in ('"', "\r", "\n")):
        raise ValueError("The requested temporary install path is not an NSIS destination.")
    command = subprocess.list2cmdline([str(executable), "/S"]) + " " + argument + str(destination)
    subprocess.run(command, executable=str(executable), cwd=cwd, check=True, timeout=360)


@contextmanager
def windows_install(artifact, scratch):
    destination = scratch / "Penstock isolated install"
    try:
        nsis_command(artifact, "/D=", destination, scratch)
        if not destination.resolve(strict=True).is_relative_to(scratch):
            raise RuntimeError("The NSIS install escaped its temporary destination.")
        exactly_one((file for file in destination.glob("uninstall*.exe") if file.is_file()),
                    "installed Penstock uninstaller")
        launchers = [file for file in destination.glob("*.exe")
                     if file.is_file() and file.stem.casefold().startswith("penstock")]
        if not launchers:
            raise RuntimeError("The NSIS install did not place a Penstock launcher in the requested destination.")
        yield destination
    finally:
        # An NSIS package can create the uninstaller before returning an error.
        candidates = [file for file in destination.glob("uninstall*.exe") if file.is_file()]
        if candidates:
            uninstall = exactly_one(candidates, "uninstaller for this temporary install")
            if not uninstall.resolve(strict=True).is_relative_to(scratch):
                raise RuntimeError("Refusing to uninstall outside this smoke test's temporary directory.")
            copy = scratch / "disposable-uninstall.exe"
            shutil.copy2(uninstall, copy)
            nsis_command(copy, "_?=", destination, scratch)
            if any(file.is_file() for file in (destination / "backend").glob("penstock-backend.exe")):
                raise RuntimeError("The NSIS uninstaller did not remove the installed backend.")


def macos_install(artifact, scratch):
    mount = scratch / "disk-image"
    mount.mkdir()
    applications = scratch / "Applications"
    applications.mkdir()
    try:
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                        "-mountpoint", mount, artifact], check=True, timeout=180)
        application = exactly_one((entry for entry in mount.glob("*.app") if entry.is_dir() and not entry.is_symlink()),
                                  "application in the Penstock disk image")
        installed = applications / application.name
        subprocess.run(["ditto", application, installed], check=True, timeout=180)
        if not any(path.is_file() for path in (installed / "Contents/MacOS").iterdir()):
            raise RuntimeError("The installed application bundle did not contain a native launcher.")
        return installed / "Contents/Resources"
    finally:
        if mount.is_mount():
            try:
                subprocess.run(["hdiutil", "detach", mount], check=True, timeout=30)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if mount.is_mount():
                    # Retry only the uniquely owned read-only mount after ditto has stopped.
                    subprocess.run(["hdiutil", "detach", "-force", mount], check=True, timeout=30)
        if mount.is_mount():
            raise RuntimeError("The Penstock disk image is still mounted.")


def installed_runtimes(root):
    root = root.resolve(strict=True)
    expected = "penstock-backend" + SUFFIX
    backend = exactly_one((file for file in root.rglob(expected)
                          if file.is_file() and file.parent.name == "backend"), "installed Penstock backend")
    node = backend.parent.parent / "node" / ("node" + SUFFIX)
    for file in (backend, node):
        if not file.resolve(strict=True).is_relative_to(root) or not file.is_file():
            raise RuntimeError(f"{file.name} does not belong to the installed application resources.")
        if os.name != "nt" and not os.access(file, os.X_OK):
            raise RuntimeError(f"The installed {file.name} is not executable.")
    return backend, node


def exercise(root, tier):
    backend, node = installed_runtimes(root)
    qualify([backend], node, tier=tier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--artifact", type=Path, help="Use a single native installer instead of discovering the CI artifact.")
    args = parser.parse_args()
    system = platform.system()
    if system not in TARGETS:
        parser.error("Use a native Linux, Windows, or macOS runner.")
    try:
        artifact = select_artifact(system, args.bundle_dir, args.artifact)
        with tempfile.TemporaryDirectory(prefix="penstock-installer-qualification-") as temporary:
            scratch = Path(temporary).resolve(strict=True)
            if system == "Windows":
                with windows_install(artifact, scratch) as root:
                    exercise(root, "Installed Windows NSIS")
            elif system == "Darwin":
                exercise(macos_install(artifact, scratch), "Installed macOS DMG")
            else:
                exercise(linux_install(artifact, scratch), "Installed Debian payload")
    except (AssertionError, OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        parser.exit(1, f"Installer qualification failed: {exc}\n")


if __name__ == "__main__":
    main()
