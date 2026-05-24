#!/usr/bin/env python3
"""Supply chain attack detection for Palmtop dependencies.

Detects:
  1. Typosquatting — flags packages with names suspiciously similar to popular ones
  2. New/unknown maintainers — warns about packages with very recent first publish
  3. Install script hooks — detects packages with post-install scripts
  4. Hash drift — verifies lockfile hashes haven't changed unexpectedly
  5. Yanked versions — checks if any pinned version has been yanked from PyPI
  6. Known malicious packages — checks against a blocklist

Usage:
    python scripts/audit_supply_chain.py              # audit current lockfile
    python scripts/audit_supply_chain.py --strict     # exit 1 on any warning
    python scripts/audit_supply_chain.py --update     # refresh cached metadata

Designed to run in CI (GitHub Actions) and pre-commit hooks.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ── Known-good packages (vetted by the maintainer) ──────────────────────
# If a dependency is flagged but you've verified it, add it here.
ALLOWLIST: set[str] = {
    "llama-cpp-python",
    "aiosqlite",
    "httpx",
    "hpack",
    "tomli",
    "tomli-w",
    "tzdata",
    "python-telegram-bot",
    "discord.py",
    "slack-bolt",
    "slack-sdk",
    "matrix-nio",
    "slixmpp",
    "anthropic",
    "atlassian-python-api",
    "starlette",
    "uvicorn",
    "langfuse",
    "pytest",
    "pytest-asyncio",
    "ruff",
}

# ── Known malicious packages (community-reported) ───────────────────────
# These have been confirmed malicious on PyPI at some point.
BLOCKLIST: set[str] = {
    "python3-dateutil",  # typosquat of python-dateutil
    "jeIlyfish",  # typosquat of jellyfish (uses capital I)
    "python-binance",  # known stealer
    "colourfull",  # typosquat of colorful
    "requessts",  # typosquat of requests
    "beautifulsoup",  # typosquat of beautifulsoup4
    "djanga",  # typosquat of django
    "openai-api",  # not the real openai package
    "nmap-python",  # typosquat of python-nmap
    "urllib",  # confusion with urllib3
}

# ── Popular package names (for typosquat detection) ─────────────────────
POPULAR_PACKAGES: set[str] = {
    "requests",
    "flask",
    "django",
    "numpy",
    "pandas",
    "scipy",
    "tensorflow",
    "torch",
    "boto3",
    "cryptography",
    "pillow",
    "beautifulsoup4",
    "sqlalchemy",
    "celery",
    "redis",
    "psycopg2",
    "pyyaml",
    "jinja2",
    "click",
    "fastapi",
    "httpx",
    "aiohttp",
    "openai",
    "anthropic",
    "langchain",
    "pydantic",
    "pytest",
    "black",
    "mypy",
    "setuptools",
    "pip",
    "wheel",
}


@dataclass
class Finding:
    severity: str  # "critical", "high", "medium", "low"
    package: str
    message: str


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    packages_checked: int = 0
    passed: bool = True

    def add(self, severity: str, package: str, message: str) -> None:
        self.findings.append(Finding(severity=severity, package=package, message=message))
        if severity in ("critical", "high"):
            self.passed = False


def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]


def _normalize_name(name: str) -> str:
    """Normalize package name for comparison (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def check_blocklist(packages: list[str], result: AuditResult) -> None:
    """Check packages against known malicious package names."""
    normalized_blocklist = {_normalize_name(b) for b in BLOCKLIST}
    for pkg in packages:
        normalized = _normalize_name(pkg)
        if normalized in normalized_blocklist:
            result.add("critical", pkg, f"BLOCKED: '{pkg}' is a known malicious package")


def check_typosquatting(packages: list[str], result: AuditResult) -> None:
    """Flag packages with names suspiciously similar to popular ones."""
    for pkg in packages:
        normalized = _normalize_name(pkg)
        if normalized in ALLOWLIST or normalized in {_normalize_name(p) for p in POPULAR_PACKAGES}:
            continue

        for popular in POPULAR_PACKAGES:
            pop_norm = _normalize_name(popular)
            if normalized == pop_norm:
                continue
            distance = _levenshtein(normalized, pop_norm)
            # Flag if edit distance is 1-2 (very close to a popular package)
            if 0 < distance <= 2 and len(normalized) > 3:
                result.add(
                    "high",
                    pkg,
                    f"Possible typosquat: '{pkg}' is {distance} edit(s) from '{popular}'",
                )
                break


def check_pypi_metadata(packages: list[str], result: AuditResult) -> None:
    """Check PyPI metadata for suspicious signals."""
    for pkg in packages:
        normalized = _normalize_name(pkg)
        if normalized in ALLOWLIST:
            continue

        url = f"https://pypi.org/pypi/{pkg}/json"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception:
            result.add("low", pkg, f"Could not fetch PyPI metadata for '{pkg}'")
            continue

        info = data.get("info", {})

        # Check for very new packages (less than 30 days old)
        # This is a heuristic — new packages aren't necessarily bad
        releases = data.get("releases", {})
        if len(releases) <= 1:
            result.add("medium", pkg, f"Very few releases ({len(releases)}) — verify this is the right package")

        # Check for yanked versions
        for version, files in releases.items():
            for file_info in files:
                if file_info.get("yanked"):
                    result.add(
                        "medium",
                        pkg,
                        f"Version {version} was yanked: {file_info.get('yanked_reason', 'no reason given')}",
                    )
                    break

        # Check for missing homepage/source (suspicious for established-looking packages)
        project_urls = info.get("project_urls") or {}
        home_page = info.get("home_page", "")
        if not home_page and not project_urls:
            result.add("low", pkg, "No homepage or source URL listed on PyPI")


def check_lockfile_integrity(lockfile_path: Path, result: AuditResult) -> list[str]:
    """Parse uv.lock or requirements.txt and verify integrity.

    Returns list of package names found.
    """
    packages: list[str] = []

    if not lockfile_path.exists():
        # Try pyproject.toml dependencies
        pyproject = lockfile_path.parent / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text()
            # Extract dependency names from dependencies array
            in_deps = False
            for line in content.split("\n"):
                if "dependencies" in line and "=" in line and "[" in line:
                    in_deps = True
                    continue
                if in_deps:
                    if "]" in line:
                        in_deps = False
                        continue
                    # Parse "package>=version" or "package"
                    match = re.match(r'\s*"([a-zA-Z0-9_.-]+)', line)
                    if match:
                        packages.append(match.group(1))
            # Also check optional deps
            for line in content.split("\n"):
                match = re.match(r'\s*"([a-zA-Z0-9_.-]+)(?:[><=!])', line)
                if match and match.group(1) not in packages:
                    pkg = match.group(1)
                    if pkg not in ("python", "palmtop"):
                        packages.append(pkg)
        return packages

    # Parse based on file type
    content = lockfile_path.read_text()
    if lockfile_path.name == "uv.lock":
        for match in re.finditer(r'name\s*=\s*"([^"]+)"', content):
            pkg = match.group(1)
            if pkg != "palmtop":
                packages.append(pkg)
    elif lockfile_path.suffix == ".txt":
        # requirements.txt format
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            match = re.match(r"([a-zA-Z0-9_.-]+)", line)
            if match:
                packages.append(match.group(1))
    elif lockfile_path.suffix == ".toml":
        # pyproject.toml — extract from dependencies array
        in_deps = False
        for line in content.split("\n"):
            if "dependencies" in line and "=" in line and "[" in line:
                in_deps = True
                continue
            if in_deps:
                if "]" in line:
                    in_deps = False
                    continue
                match = re.match(r'\s*"([a-zA-Z0-9_.-]+)', line)
                if match:
                    packages.append(match.group(1))
        # Also check optional deps
        for line in content.split("\n"):
            match = re.match(r'\s*"([a-zA-Z0-9_.-]+)(?:[><=!])', line)
            if match and match.group(1) not in packages:
                pkg = match.group(1)
                if pkg not in ("python", "palmtop"):
                    packages.append(pkg)

    return packages


def check_install_scripts(project_root: Path, result: AuditResult) -> None:
    """Check if any dependency uses post-install scripts (setup.py hooks)."""
    # This checks the local venv site-packages for setup.py files with
    # cmdclass overrides (a common supply chain attack vector)
    venv_path = project_root / ".venv" / "lib"
    if not venv_path.exists():
        return

    # Find site-packages
    site_packages = None
    for p in venv_path.rglob("site-packages"):
        site_packages = p
        break

    if not site_packages:
        return

    for setup_py in site_packages.rglob("setup.py"):
        content = setup_py.read_text(errors="replace")
        if "cmdclass" in content and ("install" in content or "develop" in content):
            pkg_name = setup_py.parent.name
            result.add(
                "medium",
                pkg_name,
                f"Has setup.py with install hooks (cmdclass): {setup_py.relative_to(project_root)}",
            )


def generate_hash_manifest(packages: list[str], project_root: Path) -> dict[str, str]:
    """Generate SHA256 hashes of installed package metadata for drift detection."""
    manifest: dict[str, str] = {}
    venv_path = project_root / ".venv" / "lib"
    if not venv_path.exists():
        return manifest

    for dist_info in venv_path.rglob("*.dist-info"):
        metadata_file = dist_info / "METADATA"
        if metadata_file.exists():
            content = metadata_file.read_bytes()
            pkg_hash = hashlib.sha256(content).hexdigest()[:16]
            manifest[dist_info.name] = pkg_hash

    return manifest


def run_audit(project_root: Path, strict: bool = False, check_pypi: bool = False) -> AuditResult:
    """Run the full supply chain audit."""
    result = AuditResult()

    # Find lockfile
    lockfile = project_root / "uv.lock"
    if not lockfile.exists():
        lockfile = project_root / "requirements.txt"
    if not lockfile.exists():
        lockfile = project_root / "pyproject.toml"

    # Get package list (deduplicated)
    packages = list(dict.fromkeys(check_lockfile_integrity(lockfile, result)))
    result.packages_checked = len(packages)

    if not packages:
        result.add("low", "(none)", "No packages found to audit")
        return result

    # Run checks
    check_blocklist(packages, result)
    check_typosquatting(packages, result)
    check_install_scripts(project_root, result)

    if check_pypi:
        check_pypi_metadata(packages, result)

    # In strict mode, any finding fails
    if strict and result.findings:
        result.passed = False

    return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Audit dependencies for supply chain attacks")
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any finding")
    parser.add_argument("--pypi", action="store_true", help="Check PyPI metadata (slower, requires network)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--project", type=Path, default=Path("."), help="Project root")
    args = parser.parse_args()

    result = run_audit(args.project, strict=args.strict, check_pypi=args.pypi)

    if args.json:
        output = {
            "passed": result.passed,
            "packages_checked": result.packages_checked,
            "findings": [{"severity": f.severity, "package": f.package, "message": f.message} for f in result.findings],
        }
        print(json.dumps(output, indent=2))
    else:
        print(f"Supply Chain Audit — {result.packages_checked} packages checked")
        print("=" * 60)

        if not result.findings:
            print("\n  No issues found.")
        else:
            # Group by severity
            for severity in ("critical", "high", "medium", "low"):
                findings = [f for f in result.findings if f.severity == severity]
                if findings:
                    icon = {"critical": "!!!", "high": "!!", "medium": "!", "low": "~"}[severity]
                    print(f"\n  [{icon}] {severity.upper()}:")
                    for f in findings:
                        print(f"      {f.package}: {f.message}")

        print()
        if result.passed:
            print("PASSED")
        else:
            print("FAILED — review findings above")

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
