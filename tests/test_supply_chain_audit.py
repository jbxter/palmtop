"""Tests for the supply chain audit script."""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts/ to path so we can import the audit module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from audit_supply_chain import (  # noqa: E402
    BLOCKLIST,
    AuditResult,
    _levenshtein,
    _normalize_name,
    check_blocklist,
    check_lockfile_integrity,
    check_typosquatting,
    run_audit,
)


class TestLevenshtein:
    def test_identical(self):
        assert _levenshtein("hello", "hello") == 0

    def test_one_insert(self):
        assert _levenshtein("cat", "cats") == 1

    def test_one_delete(self):
        assert _levenshtein("cats", "cat") == 1

    def test_one_substitute(self):
        assert _levenshtein("cat", "car") == 1

    def test_two_edits(self):
        assert _levenshtein("kitten", "mitten") == 1
        assert _levenshtein("flask", "flack") == 1

    def test_empty_strings(self):
        assert _levenshtein("", "") == 0
        assert _levenshtein("abc", "") == 3
        assert _levenshtein("", "abc") == 3

    def test_completely_different(self):
        assert _levenshtein("abc", "xyz") == 3


class TestNormalizeName:
    def test_hyphens(self):
        assert _normalize_name("my-package") == "my-package"

    def test_underscores_to_hyphens(self):
        assert _normalize_name("my_package") == "my-package"

    def test_dots_to_hyphens(self):
        assert _normalize_name("my.package") == "my-package"

    def test_mixed(self):
        assert _normalize_name("My_Package.Name") == "my-package-name"

    def test_uppercase(self):
        assert _normalize_name("MyPackage") == "mypackage"

    def test_multiple_separators(self):
        assert _normalize_name("a--b__c..d") == "a-b-c-d"


class TestCheckBlocklist:
    def test_clean_packages(self):
        result = AuditResult()
        check_blocklist(["requests", "flask", "numpy"], result)
        assert len(result.findings) == 0

    def test_blocked_package(self):
        result = AuditResult()
        check_blocklist(["requessts"], result)
        assert len(result.findings) == 1
        assert result.findings[0].severity == "critical"
        assert "malicious" in result.findings[0].message

    def test_multiple_blocked(self):
        result = AuditResult()
        check_blocklist(["requessts", "djanga", "flask"], result)
        assert len(result.findings) == 2

    def test_blocked_sets_failed(self):
        result = AuditResult()
        check_blocklist(["python3-dateutil"], result)
        assert result.passed is False

    def test_all_blocklist_entries_detected(self):
        result = AuditResult()
        check_blocklist(list(BLOCKLIST), result)
        assert len(result.findings) == len(BLOCKLIST)


class TestCheckTyposquatting:
    def test_clean_packages(self):
        result = AuditResult()
        check_typosquatting(["unrelated-pkg"], result)
        assert len(result.findings) == 0

    def test_close_to_popular(self):
        result = AuditResult()
        # "requets" is 1 edit from "requests"
        check_typosquatting(["requets"], result)
        assert len(result.findings) == 1
        assert result.findings[0].severity == "high"
        assert "typosquat" in result.findings[0].message.lower()

    def test_allowlisted_skipped(self):
        result = AuditResult()
        # httpx is in the allowlist — don't flag it even if close to something
        check_typosquatting(["httpx"], result)
        assert len(result.findings) == 0

    def test_exact_popular_not_flagged(self):
        result = AuditResult()
        # "requests" itself should not be flagged
        check_typosquatting(["requests"], result)
        assert len(result.findings) == 0

    def test_short_names_not_flagged(self):
        result = AuditResult()
        # Short names (<=3 chars) are excluded to avoid noise
        check_typosquatting(["pi"], result)
        assert len(result.findings) == 0

    def test_two_edit_distance(self):
        result = AuditResult()
        # "flasks" is 1 edit from "flask"
        check_typosquatting(["flasks"], result)
        assert len(result.findings) == 1


class TestCheckLockfileIntegrity:
    def test_pyproject_parsing(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = [\n  "requests>=2.28.0",\n  "flask>=3.0",\n  "click",\n]\n')
        result = AuditResult()
        # Pass a non-existent lockfile path so it falls back to pyproject.toml
        packages = check_lockfile_integrity(tmp_path / "uv.lock", result)
        assert "requests" in packages
        assert "flask" in packages
        assert "click" in packages

    def test_requirements_txt(self, tmp_path):
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("requests==2.31.0\nflask>=3.0\n# comment\nnumpy\n")
        result = AuditResult()
        packages = check_lockfile_integrity(reqs, result)
        assert "requests" in packages
        assert "flask" in packages
        assert "numpy" in packages
        assert len(packages) == 3

    def test_requirements_txt_skips_comments(self, tmp_path):
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("# This is a comment\n-r other.txt\nflask\n")
        result = AuditResult()
        packages = check_lockfile_integrity(reqs, result)
        assert "flask" in packages
        assert len(packages) == 1

    def test_uv_lock_format(self, tmp_path):
        lock = tmp_path / "uv.lock"
        lock.write_text(
            '[[package]]\nname = "requests"\nversion = "2.31.0"\n\n'
            '[[package]]\nname = "flask"\nversion = "3.0.0"\n\n'
            '[[package]]\nname = "palmtop"\nversion = "0.1.0"\n'
        )
        result = AuditResult()
        packages = check_lockfile_integrity(lock, result)
        assert "requests" in packages
        assert "flask" in packages
        assert "palmtop" not in packages  # Self is excluded

    def test_missing_lockfile_no_pyproject(self, tmp_path):
        result = AuditResult()
        packages = check_lockfile_integrity(tmp_path / "nonexistent.lock", result)
        assert packages == []


class TestRunAudit:
    def test_clean_project(self, tmp_path):
        # Create a minimal pyproject with clean deps
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = [\n  "unrelated-package>=1.0",\n]\n')
        result = run_audit(tmp_path)
        assert result.packages_checked == 1
        assert result.passed is True

    def test_blocked_package_fails(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = [\n  "requessts>=1.0",\n]\n')
        result = run_audit(tmp_path)
        assert result.passed is False
        critical = [f for f in result.findings if f.severity == "critical"]
        assert len(critical) == 1

    def test_strict_mode_any_finding_fails(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\ndependencies = [\n  "flasks>=1.0",\n]\n')
        # Without strict — high severity but still could pass if logic allows
        result = run_audit(tmp_path, strict=True)
        assert result.passed is False

    def test_no_packages(self, tmp_path):
        # Empty project
        result = run_audit(tmp_path)
        assert result.packages_checked == 0
        assert any("No packages found" in f.message for f in result.findings)

    def test_deduplicates_packages(self, tmp_path):
        lock = tmp_path / "uv.lock"
        lock.write_text(
            '[[package]]\nname = "flask"\nversion = "3.0.0"\n\n[[package]]\nname = "flask"\nversion = "3.0.0"\n'
        )
        result = run_audit(tmp_path)
        # Should only count flask once
        assert result.packages_checked == 1

    def test_real_project_passes(self):
        """Smoke test: the actual palmtop project should pass the audit."""
        project_root = Path(__file__).parent.parent
        result = run_audit(project_root)
        assert result.passed is True
        assert result.packages_checked > 0
