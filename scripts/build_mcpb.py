"""Build dist/garmin-mcp-<version>.mcpb.

Copies an allowlist of files into build/bundle (so .env, saved Garmin tokens and
the local .venv can never end up in the bundle), validates the manifest and
packs it with the official mcpb CLI.

Usage:  python scripts/build_mcpb.py      (needs Node.js for npx)
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_FILES = ["manifest.json", "pyproject.toml", "uv.lock", "server.py", "icon.png", "README.md"]
MCPB_CLI = "@anthropic-ai/mcpb@2.1.2"
SECRET_NAMES = {".env", "garmin_tokens.json", "oauth1_token.json", "oauth2_token.json"}


def run(*args: str) -> None:
    print("$", " ".join(args))
    subprocess.run(args, check=True, cwd=ROOT)


def check_versions(manifest_version: str) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    match = re.search(r'^__version__ = "([^"]+)"', (ROOT / "server.py").read_text("utf-8"), re.M)
    server = match.group(1) if match else None
    if not manifest_version == pyproject == server:
        sys.exit(f"Version mismatch: manifest {manifest_version}, pyproject {pyproject}, server.py {server}")


def main() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    version = manifest["version"]
    check_versions(version)

    npx = shutil.which("npx")
    if not npx:
        sys.exit("npx was not found. Install Node.js to build the bundle.")

    staging = ROOT / "build" / "bundle"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name in BUNDLE_FILES:
        shutil.copy2(ROOT / name, staging / name)

    out = ROOT / "dist" / f"{manifest['name']}-{version}.mcpb"
    out.parent.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)

    run(npx, "-y", MCPB_CLI, "validate", str(staging / "manifest.json"))
    run(npx, "-y", MCPB_CLI, "pack", str(staging), str(out))

    with zipfile.ZipFile(out) as bundle:
        names = bundle.namelist()
    leaked = [n for n in names if SECRET_NAMES & set(Path(n).parts) or n.startswith((".garth", ".venv"))]
    if leaked:
        out.unlink()
        sys.exit(f"Refusing to ship a bundle containing {leaked}")
    unexpected = sorted(set(names) - set(BUNDLE_FILES))
    if unexpected:
        out.unlink()
        sys.exit(f"Unexpected files in bundle: {unexpected}")

    print(f"\nBuilt {out.relative_to(ROOT)} ({out.stat().st_size / 1024:.0f} KB): {', '.join(sorted(names))}")


if __name__ == "__main__":
    main()
