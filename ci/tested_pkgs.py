#!/usr/bin/env python3

import os
import sys
import subprocess
import re
import glob
import logging
import argparse
import json
from pathlib import Path
from typing import Set, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(levelname)s: %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)


def find_alidist_config_files() -> List[str]:
    """Find all .env files in ci/repo-config that have PR_REPO=alisw/alidist"""
    config_files = []

    # Search for all .env files in ci/repo-config
    pattern = "../ci/repo-config/**/*.env"
    env_files = glob.glob(pattern, recursive=True)

    for env_file in env_files:
        try:
            with open(env_file) as f:
                content = f.read()
                if 'PR_REPO=alisw/alidist' in content:
                    config_files.append(env_file)
        except Exception as e:
            logger.warning(f"Could not read {env_file}: {e}")

    return config_files


def extract_packages_from_configs(config_files: List[str]) -> Set[str]:
    """Extract PACKAGE= values from config files"""
    packages = set()

    for config_file in config_files:
        logger.debug(f"Processing {config_file}")
        try:
            with open(config_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('PACKAGE='):
                        package = line.split('=', 1)[1]
                        packages.add(package)
        except Exception as e:
            logger.warning(f"Could not process {config_file}: {e}")

    return packages


def get_all_alidist_packages() -> Set[str]:
    """Get all packages defined in alidist directory"""
    packages = set()
    package_case_map = {}  # Maps lowercase -> original case

    # Search for package: lines in alidist directory
    alidist_files = glob.glob("../alidist/*.sh")

    for alidist_file in alidist_files:
        try:
            with open(alidist_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('package:'):
                        package = line.split(':', 1)[1].strip()
                        package_lower = package.lower()
                        packages.add(package_lower)
                        package_case_map[package_lower] = package
        except Exception as e:
            logger.warning(f"Could not read {alidist_file}: {e}")

    # Also search in ci/repo-config for package: lines
    config_files = glob.glob("ci/repo-config/**/*", recursive=True)
    for config_file in config_files:
        if os.path.isfile(config_file):
            try:
                with open(config_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('package:'):
                            package = line.split(':', 1)[1].strip()
                            package_lower = package.lower()
                            packages.add(package_lower)
                            package_case_map[package_lower] = package
            except Exception as e:
                # Skip files that can't be read
                pass

    return packages


def run_alidoctor(package: str) -> Set[str]:
    """Run aliDoctor for a package and extract packages that will be built"""
    tested_packages = set()

    logger.debug(f"Testing {package}")
    command = ['aliDoctor', package, "--no-system", "-c", "../alidist"]

    try:
        # Run aliDoctor and capture output
        result = subprocess.run(command,
                              text=True,
                              check=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)

        output = result.stdout

        # Parse the output to extract packages that will be built
        in_build_section = False
        if not output:
            logger.warning(f"aliDoctor output for {package} is empty")
            return tested_packages
        for line in output.split('\n'):
            line = line.strip()

            if 'The following packages will be built by aliBuild' in line:
                in_build_section = True
                continue
            elif ('The following packages will be picked up from the system' in line or
                  'This is not a real issue, but it might take longer' in line):
                in_build_section = False
                continue

            if in_build_section and line.startswith('- '):
                # Extract package name (remove "- " prefix) and normalize to lowercase
                package_name = line[2:].strip().lower()
                tested_packages.add(package_name)

    except subprocess.CalledProcessError as e:
      logger.warning(f"aliDoctor: `{' '.join(command)}` failed with return code {e.returncode}")
    except FileNotFoundError:
        logger.error("aliDoctor binary not found. Make sure it's installed and in PATH.")
        sys.exit(1)
    except Exception as e:
      logger.error(f"aliDoctor: `{' '.join(command)}` failed with error: {e}")
      traceback.print_exception(*sys.exc_info())
      sys.exit(1)

    return tested_packages


def analyze_packages(max_workers: int = 4, target_packages: Optional[Set[str]] = None) -> dict:
    """
    Analyze packages tested by CI and return structured results.

    Args:
        max_workers: Maximum number of parallel aliDoctor processes
        target_packages: If provided, only analyze these specific packages

    Returns:
        Dictionary containing statistics, tested packages, untested packages, etc.
    """
    # Change to script directory
    script_dir = Path(__file__).parent
    original_cwd = os.getcwd()
    os.chdir(script_dir)

    try:
        # Verify we're in the right directory
        if not os.path.isdir('../ci/repo-config'):
            raise FileNotFoundError("ci/repo-config directory not found")

        if not os.path.isdir('../alidist'):
            raise FileNotFoundError("alidist directory not found")

        # Find config files with PR_REPO=alisw/alidist
        config_files = find_alidist_config_files()
        logger.debug(f"Found {len(config_files)} config files with PR_REPO=alisw/alidist")
        for config_file in config_files:
            logger.debug(config_file)

        # Extract packages to test
        packages_to_test = extract_packages_from_configs(config_files)
        logger.debug(f"Packages to test: {sorted(packages_to_test)}")

        # Get all packages from alidist (normalized to lowercase)
        all_alidist_packages_lower = get_all_alidist_packages()
        logger.debug(f"Found {len(all_alidist_packages_lower)} packages in alidist")

        # If target_packages is specified, filter to only those packages
        if target_packages:
            target_packages_lower = {pkg.lower() for pkg in target_packages}
            all_alidist_packages_lower = all_alidist_packages_lower.intersection(target_packages_lower)
            logger.debug(f"Filtering to {len(all_alidist_packages_lower)} target packages")

        # Build case mapping for output (to preserve original case from alidist)
        package_case_map = {}
        alidist_files = glob.glob("alidist/*.sh")
        for alidist_file in alidist_files:
            try:
                with open(alidist_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('package:'):
                            package = line.split(':', 1)[1].strip()
                            package_case_map[package.lower()] = package
            except Exception:
                traceback.print_exception(*sys.exc_info())
                pass

        # Run aliDoctor for each package in parallel and collect tested packages
        all_tested_packages_lower = set()

        logger.debug(f"Running aliDoctor for {len(packages_to_test)} packages with {max_workers} workers...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all aliDoctor tasks
            future_to_package = {
                executor.submit(run_alidoctor, package): package
                for package in sorted(packages_to_test)
            }

            # Collect results as they complete
            for future in as_completed(future_to_package):
                package = future_to_package[future]
                try:
                    tested_packages = future.result()
                    all_tested_packages_lower.update(tested_packages)
                    logger.debug(f"Completed {package}: found {len(tested_packages)} tested packages")
                except Exception as e:
                    logger.warning(f"Error processing {package}: {e}")
                    traceback.print_exception(*sys.exc_info())

        # Find untested packages (using lowercase comparison)
        untested_packages_lower = all_alidist_packages_lower - all_tested_packages_lower

        # Convert back to original case for output
        tested_packages_original = [
            package_case_map.get(pkg, pkg) for pkg in sorted(all_tested_packages_lower)
        ]
        untested_packages_original = [
            package_case_map.get(pkg, pkg) for pkg in sorted(untested_packages_lower)
        ]

        # Return structured results
        return {
            "statistics": {
                "total_alidist_packages": len(all_alidist_packages_lower),
                "tested_packages_count": len(all_tested_packages_lower),
                "untested_packages_count": len(untested_packages_lower),
                "config_files_found": len(config_files),
                "packages_to_test_count": len(packages_to_test)
            },
            "tested_packages": tested_packages_original,
            "untested_packages": untested_packages_original,
            "packages_to_test": sorted(list(packages_to_test)),
            "config_files": config_files
        }

    finally:
        # Restore original working directory
        os.chdir(original_cwd)




def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Find packages tested by CI and identify untested ones')
    parser.add_argument('--json', action='store_true',
                       help='Output results in JSON format for easier parsing')
    parser.add_argument('--max-workers', type=int, default=4,
                       help='Maximum number of parallel aliDoctor processes (default: 4)')
    parser.add_argument('--packages',
                       help='Comma-separated list of specific packages to analyze (if not provided, analyzes all packages)')
    args = parser.parse_args()

    # Parse target packages if provided
    target_packages = None
    if args.packages:
        target_packages = set(pkg.strip() for pkg in args.packages.split(',') if pkg.strip())
        logger.debug(f"Target packages: {sorted(target_packages)}")

    # Configure logging level based on output mode
    if args.json:
        # Suppress debug logs for JSON output
        logging.getLogger().setLevel(logging.WARNING)

    try:
        # Get the analysis results
        result = analyze_packages(max_workers=args.max_workers, target_packages=target_packages)

        if args.json:
            # Output structured JSON
            print(json.dumps(result, indent=2))
        else:
            # Simple text output - just the untested packages
            untested_packages = result["untested_packages"]
            for package in untested_packages:
                print(package)

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
