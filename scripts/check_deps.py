"""Exit non-zero, listing them, if installed packages don't meet requirements.txt.

Run with the interpreter build.sh bundles from. Asks pip what a
`pip install -r requirements.txt` would change, without changing anything.
"""
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution


def installed_by_brew(name: str) -> bool:
    """True for packages Homebrew owns (e.g. certifi), which pip can't upgrade."""
    try:
        return (distribution(name).read_text("INSTALLER") or "").strip() == "brew"
    except PackageNotFoundError:
        return False


def unmet(requirements: str = "requirements.txt") -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
         "--report", "-", "-r", requirements],
        capture_output=True, text=True, check=True,
    ).stdout
    report = json.loads(out)
    return [i["metadata"]["name"] + " " + i["metadata"]["version"]
            for i in report.get("install", [])]


def main() -> int:
    try:
        missing = unmet()
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"Could not check dependencies: {e}", file=sys.stderr)
        return 2
    if not missing:
        return 0
    print("These dependencies are missing or don't meet requirements.txt "
          "(pip would install):")
    for line in missing:
        print(f"  {line}")
    brew = [line.split()[0] for line in missing if installed_by_brew(line.split()[0])]
    print("\nUpdate them, then build again:")
    if brew:
        # pip can't replace these (no RECORD file) and aborts half-way if asked
        print(f"  brew upgrade {' '.join(brew)}")
    print(f"  {sys.executable} -m pip install --break-system-packages -r requirements.txt")
    return 1


if __name__ == "__main__":
    sys.exit(main())
