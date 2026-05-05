#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "gitpython",
#     "pydantic",
# ]
# ///
"""
build.py — Build orchestration script for project releases.

Reads project configs from projects/*.json, clones and builds each one,
collects artifacts, and produces a staging directory for gh-pages deployment.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProjectConfig(BaseModel):
    name: str
    repository: str
    branch: str = "main"
    build_command: str = ""
    artifact_pattern: str = ""
    setup: str = ""
    java_version: str = "21"
    node_version: str = "20"
    archived: bool = False


class BuildEntry(BaseModel):
    commit_hash: str = ""
    commit_message: str = ""
    commit_date: str = ""
    artifact_name: str = ""
    artifact_path: str = ""
    build_status: str = "success"
    build_date: str = ""


class ProjectBuilds(BaseModel):
    id: str
    name: str
    repository: str
    archived: bool = False
    builds: list[BuildEntry] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    id: str
    name: str
    repository: str
    archived: bool = False


class ProjectsIndex(BaseModel):
    last_updated: str
    projects: list[ProjectEntry]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[build] ERROR: {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_model(path: Path, data: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data.model_dump_json(indent=2))


def load_project_builds(path: Path) -> ProjectBuilds | None:
    if not path.exists():
        return None
    return ProjectBuilds.model_validate_json(path.read_text())


def resolve_repository(repo: str) -> str:
    """Expand short-form 'owner/repo' into a full GitHub URL."""
    if not repo.startswith(("https://", "http://", "git@")):
        return f"https://github.com/{repo}"
    return repo


def setup_git_auth() -> None:
    """Configure git to use GITHUB_TOKEN for all GitHub HTTPS requests."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    subprocess.run(
        [
            "git",
            "config",
            "--global",
            "url.https://x-access-token:{}@github.com/.insteadOf".format(token),
            "https://github.com/",
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    setup_git_auth()
    root_dir = Path(__file__).resolve().parent.parent
    staging_dir = root_dir / "staging"
    work_dir = root_dir / ".work"
    ghpages_dir = root_dir / "gh-pages-existing"

    # Clean and prepare
    shutil.rmtree(staging_dir, ignore_errors=True)
    shutil.rmtree(work_dir, ignore_errors=True)
    for d in (staging_dir / "artifacts", staging_dir / "builds", work_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Copy static files to staging
    shutil.copy2(root_dir / "index.html", staging_dir)

    # Restore existing data from gh-pages
    for subdir in ("artifacts", "builds"):
        src = ghpages_dir / subdir
        if src.is_dir():
            log(f"Restoring existing {subdir} from gh-pages...")
            shutil.copytree(src, staging_dir / subdir, dirs_exist_ok=True)

    # Build each project
    for config_path in sorted((root_dir / "projects").glob("*.json")):
        project_id = config_path.stem
        config = ProjectConfig.model_validate_json(config_path.read_text())
        config.repository = resolve_repository(config.repository)
        builds_file = staging_dir / "builds" / f"{project_id}.json"
        build_date = now_iso()

        log("━" * 40)
        log(f"Building: {config.name} ({project_id})")
        log(f"  Repo:   {config.repository}")
        log(f"  Branch: {config.branch}")
        if config.archived:
            log("  ⚠ Archived — skipping build")
        log("━" * 40)

        # Archived projects: update flag only, skip build
        if config.archived:
            data = load_project_builds(builds_file) or ProjectBuilds(
                id=project_id, name=config.name, repository=config.repository
            )
            data.archived = True
            save_model(builds_file, data)
            continue

        # Clone
        clone_dir = work_dir / project_id
        try:
            repo = Repo.clone_from(
                config.repository,
                clone_dir,
                branch=config.branch,
                depth=1,
                single_branch=True,
            )
        except Exception as e:
            err(f"Failed to clone {config.repository}: {e}")
            data = load_project_builds(builds_file) or ProjectBuilds(
                id=project_id, name=config.name, repository=config.repository
            )
            data.builds.insert(0, BuildEntry(
                commit_message="Clone failed", build_status="failed", build_date=build_date
            ))
            save_model(builds_file, data)
            continue

        # Commit info
        head = repo.head.commit
        commit_hash = head.hexsha
        commit_short = repo.git.rev_parse(head.hexsha, short=True)
        commit_message = head.summary
        commit_date = head.authored_datetime.isoformat()
        log(f"  Commit: {commit_short} — {commit_message}")

        # Skip if already built
        existing = load_project_builds(builds_file)
        if existing and commit_hash in {b.commit_hash for b in existing.builds}:
            log(f"  ⏭ Already built {commit_short}, skipping.")
            continue

        # Build
        log(f"  Running: {config.build_command}")
        build_ok = subprocess.run(config.build_command, cwd=clone_dir, shell=True).returncode == 0
        build_status = "success" if build_ok else "failed"
        if not build_ok:
            err(f"Build failed for {config.name}")

        # Collect artifact
        artifact_name = ""
        artifact_path = ""
        if build_ok:
            matches = glob.glob(str(clone_dir / config.artifact_pattern))
            if not matches:
                err(f"No artifacts matching: {config.artifact_pattern}")
                build_status = "failed"
            else:
                src = Path(matches[0])
                artifact_name = src.name
                artifact_path = f"artifacts/{project_id}/{commit_hash}/{artifact_name}"
                dest = staging_dir / "artifacts" / project_id / commit_hash
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / artifact_name)
                log(f"  Artifact: {artifact_name}")

        # Record build
        data = existing or ProjectBuilds(
            id=project_id, name=config.name, repository=config.repository
        )
        data.builds.insert(0, BuildEntry(
            commit_hash=commit_hash,
            commit_message=commit_message,
            commit_date=commit_date,
            artifact_name=artifact_name,
            artifact_path=artifact_path,
            build_status=build_status,
            build_date=build_date,
        ))
        save_model(builds_file, data)
        log(f"  ✓ Build recorded ({build_status})")

    # Generate projects.json
    projects: list[ProjectEntry] = []
    for builds_file in sorted((staging_dir / "builds").glob("*.json")):
        d = ProjectBuilds.model_validate_json(builds_file.read_text())
        projects.append(ProjectEntry(id=d.id, name=d.name, repository=d.repository, archived=d.archived))

    save_model(staging_dir / "projects.json", ProjectsIndex(last_updated=now_iso(), projects=projects))
    log(f"Generated projects.json with {len(projects)} project(s)")

    # Cleanup
    shutil.rmtree(work_dir, ignore_errors=True)
    log("━" * 40)
    log(f"Build complete. Staging directory: {staging_dir}")
    log("━" * 40)


if __name__ == "__main__":
    main()
