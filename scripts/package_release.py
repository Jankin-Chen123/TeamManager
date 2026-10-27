"""Create a source archive without local credentials, data, caches or dependencies."""

from pathlib import Path
import subprocess
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode("utf-8")
        .split("\0")
    )
    output = root / "dist" / "TeamManager-v0.4.0-source.zip"
    output.parent.mkdir(exist_ok=True)
    excluded = {".git", ".venv", "data", "backups", "test-results", "dist", "node_modules", "__pycache__"}
    count = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(set(files)):
            path = Path(relative)
            if not relative or any(part in excluded for part in path.parts) or path.name == ".env":
                continue
            source = root / path
            if source.is_file():
                archive.write(source, "TeamManager/" + path.as_posix())
                count += 1
    print(f"Created {output.name}: {count} source files")


if __name__ == "__main__":
    main()
