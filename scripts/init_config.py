"""Generate installation-specific secrets without overwriting existing configuration."""

import argparse
import base64
import getpass
import os
from pathlib import Path
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--choose-password", action="store_true", help="Interactively choose the initial password"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    if target.exists():
        print("Existing .env kept. No keys or passwords were changed.")
        return
    password = (
        getpass.getpass("Initial admin password (12+ characters): ")
        if args.choose_password
        else (secrets.token_urlsafe(18))
    )
    if len(password) < 12 or any(c in password for c in "\r\n'\\"):
        raise SystemExit("Use 12+ characters without newlines, quotes or backslashes.")
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    with target.open("x", encoding="utf-8") as handle:
        handle.write(
            f"TM_MASTER_KEY='{key}'\nTM_ADMIN_PASSWORD='{password}'\n"
            "TM_DATABASE_URL=sqlite:///data/teammanager.db\nTM_COOKIE_SECURE=false\n"
        )
    if os.name != "nt":
        target.chmod(0o600)
    print("Created .env. Initial login password is the TM_ADMIN_PASSWORD value in this local file.")
    print("Keep TM_MASTER_KEY when upgrading or restoring the database.")


if __name__ == "__main__":
    main()
