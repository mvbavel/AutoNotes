"""Write requirements.lock from a `pip install --dry-run --report` JSON file.

Usage: python3.14 scripts/write_lock.py REPORT.json
"""
import json
import sys


def main(report_path: str) -> None:
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    pins = sorted(((i["metadata"]["name"], i["metadata"]["version"])
                   for i in report["install"]), key=lambda p: p[0].lower())
    with open("requirements.lock", encoding="utf-8") as f:
        header = "".join(line for line in f if line.startswith("#"))
    with open("requirements.lock", "w", encoding="utf-8") as f:
        f.write(header + "".join(f"{name}=={version}\n" for name, version in pins))
    print(f"requirements.lock: {len(pins)} pins")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
