"""Secrets management via dotenvx.

dotenvx encrypts .env files with AES-256-GCM so they can be safely
committed to git. At runtime, `dotenvx run` decrypts them transparently.

CLI:
    palmtop init    # Interactive setup: create .env, encrypt with dotenvx
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _find_dotenvx() -> str | None:
    """Find dotenvx binary on PATH."""
    return shutil.which("dotenvx")


def _install_hint() -> str:
    """Return install instructions for the current platform."""
    if sys.platform == "darwin":
        return "brew install dotenvx/brew/dotenvx"
    if "TERMUX_VERSION" in os.environ:
        return "npm install -g @dotenvx/dotenvx"
    return "curl -sfS https://dotenvx.sh | sh"


def _check_dotenvx() -> str | None:
    """Check if dotenvx is available. Returns path or None."""
    path = _find_dotenvx()
    if path:
        return path
    # Try npx fallback
    npx = shutil.which("npx")
    if npx:
        try:
            result = subprocess.run(
                ["npx", "@dotenvx/dotenvx", "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return "npx @dotenvx/dotenvx"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return None


def init_secrets() -> None:
    """Interactive setup: create .env from template and encrypt with dotenvx."""
    env_path = Path(".env")
    example_path = Path(".env.example")
    vault_path = Path(".env.vault")

    print("Palmtop secrets setup")
    print("=" * 40)
    print()

    # Step 1: Check for dotenvx
    dotenvx = _check_dotenvx()
    if not dotenvx:
        print("dotenvx not found.")
        print()
        print(f"  Install: {_install_hint()}")
        print("  Docs:    https://dotenvx.com/docs")
        print()
        print("After installing, run `palmtop init` again.")
        raise SystemExit(1)

    print(f"Found dotenvx: {dotenvx}")
    print()

    # Step 2: Create .env if it doesn't exist
    if not env_path.exists():
        if example_path.exists():
            shutil.copy(example_path, env_path)
            print("Created .env from .env.example")
        else:
            env_path.write_text("# Palmtop secrets — fill in your API keys\n")
            print("Created empty .env")
        print()
        print("Edit .env with your API keys:")
        print("  $EDITOR .env")
        print()
        input("Press Enter when ready to encrypt...")
        print()
    else:
        print(".env already exists")
        print()

    # Step 3: Encrypt
    if vault_path.exists():
        print(".env.vault already exists — secrets are encrypted.")
        print()
        print("To re-encrypt after changes:")
        print(f"  {dotenvx} encrypt")
    else:
        print("Encrypting .env...")
        cmd = dotenvx.split() + ["encrypt"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print("Encrypted successfully.")
            print()
            print("Your secrets are now in .env.vault (safe to commit).")
            print("Decryption keys are in .env.keys (keep private).")
        else:
            print(f"Encryption failed: {result.stderr.strip()}")
            raise SystemExit(1)

    print()
    print("To run Palmtop with encrypted secrets:")
    print(f"  {dotenvx} run -- python -m palmtop")
    print()
    print("To add DOTENV_KEY to CI (GitHub Actions):")
    print("  gh secret set DOTENV_KEY < .env.keys")
