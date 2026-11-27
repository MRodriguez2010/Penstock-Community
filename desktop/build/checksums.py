"""Hash the assembled installer deliverables alongside the GitHub workflow artifacts."""
from hashlib import file_digest
from pathlib import Path


root = Path(__file__).resolve().parents[1] / "src-tauri/target/release/bundle"
paths = sorted(path for path in root.rglob("*") if path.is_file() and
               path.suffix.lower() in {".appimage", ".deb", ".dmg", ".exe"} and
               "macos" not in path.relative_to(root).parts)
if not paths:
    raise SystemExit("The native build did not create an installer.")
with (root / "SHA256SUMS.txt").open("w", encoding="utf-8") as output:
    for path in paths:
        with path.open("rb") as installer:
            digest = file_digest(installer, "sha256").hexdigest()
        output.write(f"{digest}  {path.relative_to(root).as_posix()}\n")
