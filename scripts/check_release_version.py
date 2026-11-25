"""Fail fast on a mismatched release version without installing dependencies."""

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def match(path: str, pattern: str) -> str:
    found = re.search(pattern, read(path), re.MULTILINE)
    if found is None:
        raise SystemExit(f"Missing version declaration: {path}")
    return found.group(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Optional Git tag to validate")
    args = parser.parse_args()
    version = tomllib.loads(read("pyproject.toml"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"Invalid release version: {version}")
    package = [p for p in tomllib.loads(read("uv.lock"))["package"] if p["name"] == "reelucid"]
    if len(package) != 1 or package[0].get("source") != {"editable": "."}:
        raise SystemExit("Expected one editable reelucid package in uv.lock")
    web_lock = json.loads(read("web/package-lock.json"))
    checks = {
        "src/reelucid/__init__.py": match("src/reelucid/__init__.py", r'^__version__ = "([^"]+)"$'),
        "uv.lock (root package)": package[0]["version"],
        "web/package.json": json.loads(read("web/package.json"))["version"],
        "web/package-lock.json": web_lock["version"],
        "web/package-lock.json (root package)": web_lock["packages"][""]["version"],
        "docs/openapi.json": json.loads(read("docs/openapi.json"))["info"]["version"],
        "compose.yaml": match("compose.yaml", r'image: \$\{REELUCID_IMAGE:-reelucid:([^}]+)\}'),
        "Dockerfile OCI label": match("Dockerfile", r'^LABEL org\.opencontainers\.image\.version="([^"]+)"$'),
    }
    mismatches = [f"{name}: {value} != {version}" for name, value in checks.items() if value != version]
    if args.tag is not None and args.tag != f"v{version}":
        mismatches.append(f"Git tag: {args.tag} != v{version}")
    if mismatches:
        raise SystemExit("Release version mismatch:\n" + "\n".join(mismatches))
    print(f"Release version {version}: {len(checks)} declarations agree")


if __name__ == "__main__":
    main()
