#!/usr/bin/env python3
"""
Elitea LLM Gateway Feature Validator

Validates features defined in features.json by running their validator definitions.
Updates the "passes" field in features.json based on validation results.

Usage:
    python validate.py                    # Validate all features
    python validate.py --phase phase-1    # Validate specific phase
    python validate.py --feature F1.1     # Validate specific feature
    python validate.py --report           # Generate validation report
    python validate.py --update           # Update features.json with results
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ValidationResult:
    feature_id: str
    description: str
    passed: bool
    message: str
    validator_type: str


def load_features(features_path: Path) -> dict:
    """Load features.json file."""
    with open(features_path) as f:
        return json.load(f)


def save_features(features_path: Path, features: dict) -> None:
    """Save updated features.json file."""
    with open(features_path, "w") as f:
        json.dump(features, f, indent=2)
        f.write("\n")


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def validate_directory_exists(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate that all specified directories exist."""
    paths = validator.get("paths", [])
    missing = []

    for path in paths:
        full_path = project_root / path
        if not full_path.is_dir():
            missing.append(path)

    if missing:
        return False, f"Missing directories: {', '.join(missing)}"
    return True, f"All {len(paths)} directories exist"


def validate_file_exists(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate that specified file(s) exist."""
    path = validator.get("path")
    paths = validator.get("paths", [])
    any_mode = validator.get("any", False)

    if path:
        paths = [path]

    existing = []
    missing = []

    for p in paths:
        full_path = project_root / p
        if full_path.is_file():
            existing.append(p)
        else:
            missing.append(p)

    if any_mode:
        if existing:
            return True, f"Found: {existing[0]}"
        return False, f"None of the files exist: {', '.join(missing)}"
    else:
        if missing:
            return False, f"Missing files: {', '.join(missing)}"
        return True, f"All {len(paths)} files exist"


def validate_file_contains(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate that a file contains all specified patterns."""
    path = validator.get("path")
    patterns = validator.get("patterns", [])

    full_path = project_root / path

    if not full_path.is_file():
        return False, f"File does not exist: {path}"

    try:
        content = full_path.read_text()
    except Exception as e:
        return False, f"Error reading file: {e}"

    missing = []
    for pattern in patterns:
        if pattern not in content:
            missing.append(pattern)

    if missing:
        return False, f"Missing patterns in {path}: {missing}"
    return True, f"All {len(patterns)} patterns found in {path}"


def validate_command(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate by running a shell command."""
    command = validator.get("command")

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            return True, f"Command succeeded: {result.stdout.strip()[:100]}"
        return False, f"Command failed: {result.stderr.strip()[:100]}"
    except subprocess.TimeoutExpired:
        return False, "Command timed out"
    except Exception as e:
        return False, f"Command error: {e}"


def validate_coverage(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate test coverage meets threshold."""
    path = validator.get("path")
    threshold = validator.get("threshold", 85)

    full_path = project_root / path

    if not full_path.exists():
        return False, f"Path does not exist: {path}"

    # Check if it's a Python or JS/TS project
    if "EliteaUI" in path:
        # Frontend - check for Vitest coverage
        coverage_file = project_root / "EliteaUI" / "coverage" / "coverage-summary.json"
        if not coverage_file.exists():
            return False, "Coverage not generated. Run: npm run test:coverage"

        try:
            with open(coverage_file) as f:
                coverage = json.load(f)

            # Get total coverage
            total = coverage.get("total", {})
            lines_pct = total.get("lines", {}).get("pct", 0)

            if lines_pct >= threshold:
                return True, f"Frontend coverage: {lines_pct}% >= {threshold}%"
            return False, f"Frontend coverage: {lines_pct}% < {threshold}%"
        except Exception as e:
            return False, f"Error reading coverage: {e}"
    else:
        # Check for coverage XML (pytest or vitest cobertura)
        candidates = [
            full_path / "coverage.xml",
            full_path / "coverage" / "cobertura-coverage.xml",
            full_path / "coverage" / "coverage-summary.json",
        ]

        for coverage_file in candidates:
            if not coverage_file.exists():
                continue

            if coverage_file.name == "coverage-summary.json":
                try:
                    with open(coverage_file) as f:
                        coverage = json.load(f)
                    total = coverage.get("total", {})
                    lines_pct = total.get("lines", {}).get("pct", 0)
                    if lines_pct >= threshold:
                        return True, f"Coverage: {lines_pct}% >= {threshold}%"
                    return False, f"Coverage: {lines_pct}% < {threshold}%"
                except Exception as e:
                    return False, f"Error reading coverage-summary.json: {e}"
            else:
                try:
                    import xml.etree.ElementTree as ET
                    tree = ET.parse(coverage_file)
                    root = tree.getroot()
                    line_rate = float(root.get("line-rate", 0)) * 100
                    if line_rate >= threshold:
                        return True, f"Coverage: {line_rate:.1f}% >= {threshold}%"
                    return False, f"Coverage: {line_rate:.1f}% < {threshold}%"
                except Exception as e:
                    return False, f"Error reading {coverage_file.name}: {e}"

        return False, f"No coverage data found. Run: cd {path} && npx vitest run --coverage"


def validate_sql_tables(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate that SQL migration files exist for the specified tables."""
    tables = validator.get("tables", [])

    # Check multiple migration directories
    migration_dirs = [
        project_root / "centry" / "pylon_main" / "plugins" / "gateway_access" / "migrations",
        project_root / "centry" / "pylon_main" / "plugins" / "gateway_analytics" / "migrations",
        project_root / "centry" / "pylon_gateway" / "plugins" / "gateway_core" / "migrations",
    ]

    all_content = ""
    files_found = 0

    for migrations_dir in migration_dirs:
        if not migrations_dir.exists():
            continue

        # Look for migration files containing table definitions
        migration_files = list(migrations_dir.glob("*.py")) + list(migrations_dir.glob("*.sql"))

        for mf in migration_files:
            try:
                all_content += mf.read_text()
                files_found += 1
            except:
                pass

    if files_found == 0:
        return False, "No migration files found"

    missing = []
    for table in tables:
        # Check for various CREATE TABLE patterns including schema-prefixed
        patterns = [
            f"CREATE TABLE {table}",
            f"CREATE TABLE IF NOT EXISTS {table}",
            f"CREATE TABLE centry.{table}",
            f"CREATE TABLE IF NOT EXISTS centry.{table}",
            f'"{table}"',
        ]
        if not any(p in all_content for p in patterns):
            missing.append(table)

    if missing:
        return False, f"Missing table migrations: {missing}"
    return True, f"All {len(tables)} table migrations found"


def validate_sql_view(validator: dict, project_root: Path) -> tuple[bool, str]:
    """Validate that a materialized view exists in migrations."""
    view = validator.get("view")

    migrations_dir = project_root / "centry" / "pylon_main" / "plugins" / "gateway_analytics" / "migrations"

    if not migrations_dir.exists():
        return False, f"No migrations directory found"

    migration_files = list(migrations_dir.glob("*.py")) + list(migrations_dir.glob("*.sql"))

    for mf in migration_files:
        try:
            content = mf.read_text()
            # Check for various CREATE MATERIALIZED VIEW patterns including schema-prefixed
            patterns = [
                f"CREATE MATERIALIZED VIEW {view}",
                f"CREATE MATERIALIZED VIEW IF NOT EXISTS {view}",
                f"CREATE MATERIALIZED VIEW centry.{view}",
                f"CREATE MATERIALIZED VIEW IF NOT EXISTS centry.{view}",
            ]
            if any(p in content for p in patterns):
                return True, f"Materialized view {view} found"
        except:
            pass

    return False, f"Materialized view {view} not found in migrations"


VALIDATORS = {
    "directory_exists": validate_directory_exists,
    "file_exists": validate_file_exists,
    "file_contains": validate_file_contains,
    "command": validate_command,
    "coverage": validate_coverage,
    "sql_tables": validate_sql_tables,
    "sql_view": validate_sql_view,
}


def validate_feature(feature: dict, project_root: Path) -> ValidationResult:
    """Validate a single feature."""
    feature_id = feature["id"]
    description = feature["description"]
    validator = feature.get("validator", {})
    validator_type = validator.get("type", "unknown")

    if validator_type not in VALIDATORS:
        return ValidationResult(
            feature_id=feature_id,
            description=description,
            passed=False,
            message=f"Unknown validator type: {validator_type}",
            validator_type=validator_type
        )

    try:
        passed, message = VALIDATORS[validator_type](validator, project_root)
    except Exception as e:
        passed = False
        message = f"Validation error: {e}"

    return ValidationResult(
        feature_id=feature_id,
        description=description,
        passed=passed,
        message=message,
        validator_type=validator_type
    )


def validate_phase(phase: dict, project_root: Path) -> list[ValidationResult]:
    """Validate all features in a phase."""
    results = []
    for feature in phase.get("features", []):
        result = validate_feature(feature, project_root)
        results.append(result)
    return results


def validate_all(features_data: dict, project_root: Path) -> dict[str, list[ValidationResult]]:
    """Validate all phases and features."""
    all_results = {}
    for phase in features_data.get("phases", []):
        phase_id = phase["id"]
        results = validate_phase(phase, project_root)
        all_results[phase_id] = results
    return all_results


def print_results(all_results: dict[str, list[ValidationResult]], features_data: dict) -> None:
    """Print validation results."""
    total_passed = 0
    total_failed = 0

    for phase in features_data.get("phases", []):
        phase_id = phase["id"]
        phase_name = phase["name"]
        results = all_results.get(phase_id, [])

        phase_passed = sum(1 for r in results if r.passed)
        phase_total = len(results)

        print(f"\n{'='*60}")
        print(f"Phase: {phase_name} ({phase_id})")
        print(f"Progress: {phase_passed}/{phase_total} features passing")
        print(f"{'='*60}")

        for r in results:
            status = "✓" if r.passed else "✗"
            color = "\033[92m" if r.passed else "\033[91m"
            reset = "\033[0m"
            print(f"  {color}{status}{reset} [{r.feature_id}] {r.description[:50]}...")
            if not r.passed:
                print(f"      └─ {r.message}")

        total_passed += phase_passed
        total_failed += (phase_total - phase_passed)

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_passed} passed, {total_failed} failed")
    print(f"{'='*60}")


def update_features_json(features_data: dict, all_results: dict[str, list[ValidationResult]], features_path: Path) -> None:
    """Update features.json with validation results."""
    for phase in features_data.get("phases", []):
        phase_id = phase["id"]
        results = all_results.get(phase_id, [])

        result_map = {r.feature_id: r.passed for r in results}

        for feature in phase.get("features", []):
            feature_id = feature["id"]
            if feature_id in result_map:
                feature["passes"] = result_map[feature_id]

    save_features(features_path, features_data)
    print(f"\nUpdated {features_path}")


def print_progress_bar(passed: int, total: int, width: int = 30) -> str:
    """Create a visual progress bar."""
    if total == 0:
        return "[" + " " * width + "] 0%"
    pct = passed / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct*100:.0f}%"


def print_dashboard(all_results: dict[str, list[ValidationResult]], features_data: dict) -> None:
    """Print a compact dashboard view."""
    print("\n" + "=" * 60)
    print("  ELITEA LLM GATEWAY - Implementation Progress")
    print("=" * 60 + "\n")

    total_passed = 0
    total_features = 0

    for phase in features_data.get("phases", []):
        phase_id = phase["id"]
        phase_name = phase["name"]
        duration = phase.get("duration", "")
        results = all_results.get(phase_id, [])

        phase_passed = sum(1 for r in results if r.passed)
        phase_total = len(results)
        total_passed += phase_passed
        total_features += phase_total

        bar = print_progress_bar(phase_passed, phase_total, 20)
        status = "✓" if phase_passed == phase_total else " "
        print(f"  {status} {phase_name:<30} {bar} {phase_passed}/{phase_total}")

    print("\n" + "-" * 60)
    overall_bar = print_progress_bar(total_passed, total_features, 30)
    print(f"  OVERALL {overall_bar} {total_passed}/{total_features}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Validate Elitea LLM Gateway features")
    parser.add_argument("--phase", help="Validate specific phase (e.g., phase-1)")
    parser.add_argument("--feature", help="Validate specific feature (e.g., F1.1)")
    parser.add_argument("--report", action="store_true", help="Generate detailed report")
    parser.add_argument("--update", action="store_true", help="Update features.json with results")
    parser.add_argument("--dashboard", action="store_true", help="Show compact progress dashboard")
    args = parser.parse_args()

    project_root = get_project_root()
    features_path = project_root / ".ralph" / "features.json"

    if not features_path.exists():
        print(f"Error: {features_path} not found")
        sys.exit(1)

    features_data = load_features(features_path)

    if args.feature:
        # Validate single feature
        for phase in features_data.get("phases", []):
            for feature in phase.get("features", []):
                if feature["id"] == args.feature:
                    result = validate_feature(feature, project_root)
                    status = "PASS" if result.passed else "FAIL"
                    print(f"[{status}] {result.feature_id}: {result.description}")
                    print(f"  {result.message}")
                    sys.exit(0 if result.passed else 1)
        print(f"Feature {args.feature} not found")
        sys.exit(1)

    if args.phase:
        # Validate single phase
        for phase in features_data.get("phases", []):
            if phase["id"] == args.phase:
                results = validate_phase(phase, project_root)
                all_results = {args.phase: results}
                print_results(all_results, {"phases": [phase]})

                if args.update:
                    update_features_json(features_data, all_results, features_path)

                failed = sum(1 for r in results if not r.passed)
                sys.exit(1 if failed > 0 else 0)
        print(f"Phase {args.phase} not found")
        sys.exit(1)

    # Validate all
    all_results = validate_all(features_data, project_root)

    if args.dashboard:
        print_dashboard(all_results, features_data)
    else:
        print_results(all_results, features_data)

    if args.update:
        update_features_json(features_data, all_results, features_path)

    total_failed = sum(
        sum(1 for r in results if not r.passed)
        for results in all_results.values()
    )
    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
