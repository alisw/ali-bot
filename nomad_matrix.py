#!/usr/bin/env python3

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import defaultdict


def run(cmd):
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout


def get_nodes(node_name_pattern):
    data = json.loads(run(["nomad", "node", "status", "-os", "-json"]))
    return {
        node_name: node["Attributes"]["os.name"]
        for node in data
        if re.match(node_name_pattern, node_name := node["Name"])
    }


def get_jobs(job_name_pattern):
    data = json.loads(run(["nomad", "job", "status", "-json"]))
    return sorted(job_name for job in data if re.match(job_name_pattern, job_name := job["Summary"]["JobID"]))


def get_allocations(job_name_pattern, node_name_pattern):
    data = json.loads(run(["nomad", "alloc", "status", "-json"]))
    mapping = defaultdict(set)
    for alloc in data:
        if alloc.get("ClientStatus") != "running":
            continue
        node_name = alloc.get("NodeName")
        if not re.match(node_name_pattern, node_name):
            continue
        job_name = alloc.get("JobID")
        if not re.match(job_name_pattern, job_name):
            continue
        if node_name and job_name:
            mapping[node_name].add(job_name)
    return mapping


def compute_job_counts(nodes, jobs, mapping):
    counts = {}
    for job in jobs:
        count = sum(1 for node in nodes if job in mapping[node])
        counts[job] = count
    return counts


def extract_check_name(path):
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("CHECK_NAME="):
                value = line.split("=", 1)[1].strip()
                # Remove optional quotes
                value = value.strip('"').strip("'")
                return value
    return None


def get_job_checks(repos_dir):
    """
    Returns:
        {"ci-role-arch": ["check1", "check2"]}
    """
    job_checks = defaultdict(list)
    if not os.path.isdir(repos_dir):
        raise ValueError(f"Directory {repos_dir} does not exist")
    for role in os.listdir(repos_dir):
        role_path = os.path.join(repos_dir, role)
        if not os.path.isdir(role_path):
            continue
        for arch in os.listdir(role_path):
            arch_path = os.path.join(role_path, arch)
            if not os.path.isdir(arch_path):
                continue
            job_name = f"ci-{role}-{arch}"
            for filename in os.listdir(arch_path):
                if not filename.endswith(".env"):
                    continue
                filepath = os.path.join(arch_path, filename)
                try:
                    check_name = extract_check_name(filepath)
                    if check_name:
                        job_checks[job_name].append(check_name)
                except Exception:
                    pass
    # Sort and deduplicate
    for job in job_checks:
        job_checks[job] = sorted(set(job_checks[job]))
    return job_checks


def print_csv(nodes, jobs, mapping, job_checks):
    writer = csv.writer(sys.stdout)

    job_counts = compute_job_counts(nodes, jobs, mapping)

    # Header row
    writer.writerow(["", "OS", "Jobs"] + jobs)

    # Check names row
    writer.writerow(["Checks", "", ""] + ["\n".join(job_checks.get(job, [])) for job in jobs])

    # Node count row
    writer.writerow(["Nodes", "", ""] + [job_counts[job] for job in jobs])

    # Node rows
    for node, os_node in sorted(nodes.items()):
        running_jobs = mapping[node]
        row = [node, os_node, len(running_jobs)]
        for job in jobs:
            row.append("x" if job in running_jobs else "")
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Generate Nomad node-job-check matrix as CSV")
    parser.add_argument(
        "--repos-dir",
        default="ci/repo-config",
        help="Path to repos directory (default: 'ci/repo-config')",
    )
    parser.add_argument(
        "--job",
        default="",
        help="Regex to filter job names",
    )
    parser.add_argument(
        "--node",
        default="",
        help="Regex to filter node names",
    )
    args = parser.parse_args()

    try:
        nodes = get_nodes(args.node)
        jobs = get_jobs(args.job)
        mapping = get_allocations(args.job, args.node)
        job_checks = get_job_checks(args.repos_dir)
        print_csv(nodes, jobs, mapping, job_checks)
    except subprocess.CalledProcessError as e:
        print("Command failed:", " ".join(e.cmd), file=sys.stderr)
        print(e.stderr, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
