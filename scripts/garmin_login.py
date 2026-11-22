"""Sign in to Garmin and save a separate session for the remote server.

The remote server needs its own Garmin session (its own tokens). If it shared the
tokens in ~/.garminconnect with the Claude Desktop bundle, whichever refreshed
them first could log the other one out.

Usage:
  python scripts/garmin_login.py
      Signs in and saves the session in ~/.garminconnect-remote. Email and password
      come from .env or are asked for; asks for a verification code if Garmin wants one.

  python scripts/garmin_login.py --fly-app <app-name>
      Stores the session as the GARMIN_TOKENS_B64 secret on that Fly.io app (signs in
      first if there is no session yet). Runs flyctl itself, so no shell pipe is involved:
      Windows PowerShell 5.1 adds a byte-order mark to piped text, which breaks the secret.

  python scripts/garmin_login.py --print-fly-secret
      Prints GARMIN_TOKENS_B64=... on stdout instead (everything else goes to stderr).
"""

import argparse
import base64
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOKEN_DIR = Path("~/.garminconnect-remote").expanduser()


def ask(prompt: str) -> str:
    sys.stderr.write(prompt)
    sys.stderr.flush()
    return input().strip()


def login(token_dir: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv(ROOT / ".env")
    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL") or ask("Garmin email: ")
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ", stream=sys.stderr)
    api = Garmin(email, password, prompt_mfa=lambda: ask("Garmin verification code: "))
    api.login()  # no token store: always a fresh sign-in, so this is a new session
    token_dir.mkdir(parents=True, exist_ok=True)
    api.client.dump(str(token_dir))
    print(f"Signed in to Garmin. Session saved in {token_dir}", file=sys.stderr)


def find_flyctl() -> str:
    for name in ("fly", "flyctl"):
        if found := shutil.which(name):
            return found
    default = Path("~/.fly/bin/flyctl.exe" if os.name == "nt" else "~/.fly/bin/flyctl").expanduser()
    if default.exists():
        return str(default)
    sys.exit("flyctl was not found. Install it from https://fly.io/docs/flyctl/install/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token-dir", type=Path, default=DEFAULT_TOKEN_DIR)
    parser.add_argument("--fly-app", help="store the session as a secret on this Fly.io app")
    parser.add_argument("--print-fly-secret", action="store_true", help="print GARMIN_TOKENS_B64=... on stdout")
    parser.add_argument("--fresh", action="store_true", help="sign in again even if a session exists")
    args = parser.parse_args()

    token_file = args.token_dir.expanduser() / "garmin_tokens.json"
    if args.fresh or not token_file.exists():
        login(args.token_dir.expanduser())
    elif not (args.print_fly_secret or args.fly_app):
        print(f"A session already exists in {token_file}. Use --fresh to sign in again.", file=sys.stderr)

    secret = "GARMIN_TOKENS_B64=" + base64.b64encode(token_file.read_bytes()).decode()
    if args.fly_app:
        # The value goes over stdin, so it never shows up in a process list or shell history.
        subprocess.run([find_flyctl(), "secrets", "import", "-a", args.fly_app], input=secret + "\n", text=True, check=True)
        print(f"Stored the Garmin session as GARMIN_TOKENS_B64 on {args.fly_app}.", file=sys.stderr)
    if args.print_fly_secret:
        print(secret)


if __name__ == "__main__":
    main()
