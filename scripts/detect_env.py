#!/usr/bin/env python3
"""
detect_env.py — Detect required build environments from project configs.

Scans project config JSON files and outputs GitHub Actions variables
for which environments (Java, Node) need to be set up and their versions.
Supports multiple versions per environment.
"""

import json
import os
import sys
from pathlib import Path


def main():
    projects_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("projects")
    output_file = os.environ.get("GITHUB_OUTPUT")

    java_versions: set[str] = set()
    node_versions: set[str] = set()

    for config_path in sorted(projects_dir.glob("*.json")):
        config = json.loads(config_path.read_text())
        setup = config.get("setup", "")
        if setup == "java":
            java_versions.add(str(config.get("java_version", "21")))
        elif setup == "node":
            node_versions.add(str(config.get("node_version", "20")))

    outputs = {
        "needs_java": str(bool(java_versions)).lower(),
        "needs_node": str(bool(node_versions)).lower(),
        "java_version": "\n".join(sorted(java_versions)),
        "node_version": "\n".join(sorted(node_versions)),
    }

    if output_file:
        with open(output_file, "a") as f:
            for key, value in outputs.items():
                if "\n" in value:
                    f.write(f"{key}<<EOF\n{value}\nEOF\n")
                else:
                    f.write(f"{key}={value}\n")
    else:
        for key, value in outputs.items():
            print(f"{key}={value}")


if __name__ == "__main__":
    main()
