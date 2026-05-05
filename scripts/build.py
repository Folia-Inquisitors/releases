#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "gitpython",
#     "pydantic",
#     "docker",
# ]
# ///
"""
build.py — Build orchestration script for project releases.
Builds projects in isolated Docker containers and manages artifact lifecycle.
"""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import docker
from git import Repo
from pydantic import BaseModel, Field

# --- Models ---

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

# --- Helpers ---

def log(msg: str):
    print(f"[info] {msg}", flush=True)

def err(msg: str):
    print(f"[error] {msg}", file=sys.stderr, flush=True)

def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def resolve_repo(repo: str) -> str:
    if not repo.startswith(("http", "git@")):
        return f"https://github.com/{repo}"
    return repo

def get_docker_image(config: ProjectConfig) -> str:
    s = config.setup.lower()
    if s == "java":
        return f"maven:3-eclipse-temurin-{config.java_version}"
    if s == "node":
        return f"node:{config.node_version}"
    return "ubuntu:22.04"

# --- Main Orchestrator ---

class Orchestrator:
    def __init__(self, root: Path, docker_client: docker.DockerClient):
        self.root = root
        self.staging = root / "staging"
        self.work = root / ".work"
        self.cache = root / ".cache"
        self.history = root / "gh-pages-existing"
        self.docker = docker_client

    def setup(self):
        for p in [self.staging, self.work]:
            shutil.rmtree(p, ignore_errors=True)
        
        target_dirs = [
            self.staging / "artifacts",
            self.staging / "builds",
            self.work,
            self.cache
        ]
        for p in target_dirs:
            p.mkdir(parents=True, exist_ok=True)
        
        for d in ["artifacts", "builds"]:
            src = self.history / d
            if src.is_dir():
                log(f"Restoring {d} history...")
                shutil.copytree(src, self.staging / d, dirs_exist_ok=True)
        
        idx = self.root / "index.html"
        if idx.exists():
            shutil.copy2(idx, self.staging)

    def run_build(self, pid: str, config: ProjectConfig):
        repo_url = resolve_repo(config.repository)
        data_path = self.staging / "builds" / f"{pid}.json"
        
        if data_path.exists():
            data = ProjectBuilds.model_validate_json(data_path.read_text())
        else:
            data = ProjectBuilds(id=pid, name=config.name, repository=repo_url)

        print(f"\n--- Project: {config.name} [{pid}] ---")
        log(f"Repo: {repo_url} ({config.branch})")

        if config.archived:
            log("Status: Archived (skipping)")
            data.archived = True
            data_path.write_text(data.model_dump_json(indent=2))
            return

        clone_dir = self.work / pid
        home_dir = self.work / f"{pid}_home"
        home_dir.mkdir(parents=True, exist_ok=True)

        try:
            repo = Repo.clone_from(repo_url, clone_dir, branch=config.branch, depth=1, single_branch=True)
            head = repo.head.commit
            log(f"Commit: {head.hexsha[:7]} - {head.summary}")

            already_built = any(
                b.commit_hash == head.hexsha and 
                b.build_status == "success" and 
                (self.staging / b.artifact_path).is_file() 
                for b in data.builds
            )
            
            if already_built:
                log("Result: Already built, skipping.")
                return

            # Docker Configuration
            img = get_docker_image(config)
            vols = {
                clone_dir: "/workspace",
                home_dir: "/home/builder"
            }
            envs = {
                "HOME": "/home/builder"
            }
            
            if config.setup.lower() == "java":
                if os.getenv("USE_LOCAL_M2") == "true":
                    m2 = Path.home() / ".m2"
                else:
                    m2 = self.cache / "m2"
                
                m2.mkdir(parents=True, exist_ok=True)
                vols[m2] = "/home/builder/.m2"
                envs.update({
                    "MAVEN_CONFIG": "/home/builder/.m2",
                    "MAVEN_OPTS": "-Dmaven.repo.local=/home/builder/.m2/repository"
                })
                
                gradle = self.cache / "gradle"
                gradle.mkdir(parents=True, exist_ok=True)
                vols[gradle] = "/home/builder/.gradle"
            elif config.setup.lower() == "node":
                npm = self.cache / "npm"
                npm.mkdir(parents=True, exist_ok=True)
                vols[npm] = "/home/builder/.npm"

            log(f"Command: {config.build_command}")
            
            # Run via Docker SDK
            container = self.docker.containers.run(
                img,
                command=["sh", "-c", config.build_command],
                volumes={str(s.resolve()): {"bind": d, "mode": "rw"} for s, d in vols.items()},
                environment=envs,
                user=f"{os.getuid()}:{os.getgid()}",
                working_dir="/workspace",
                remove=True,
                detach=True
            )
            
            for line in container.logs(stream=True):
                print(line.decode("utf-8"), end="", flush=True)
            
            res = container.wait()
            ok = (res.get("StatusCode", 0) == 0)

            art_name, art_path = "", ""
            if ok:
                matches = list(clone_dir.glob(config.artifact_pattern))
                if matches:
                    src = matches[0]
                    art_name = src.name
                    art_path = f"artifacts/{pid}/{head.hexsha}/{art_name}"
                    dest = self.staging / "artifacts" / pid / head.hexsha
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest / art_name)
                    log(f"Output: {art_name}")
                else:
                    err("No artifacts found.")
                    ok = False

            data.builds.insert(0, BuildEntry(
                commit_hash=head.hexsha,
                commit_message=head.summary,
                commit_date=head.authored_datetime.isoformat(),
                artifact_name=art_name,
                artifact_path=art_path,
                build_status="success" if ok else "failed",
                build_date=now()
            ))
            data_path.write_text(data.model_dump_json(indent=2))
            log(f"Result: {'SUCCESS' if ok else 'FAILED'}")

        except Exception as e:
            err(f"Build failed: {e}")
            data.builds.insert(0, BuildEntry(
                commit_message=f"Error: {str(e)[:50]}...",
                build_status="failed",
                build_date=now()
            ))
            data_path.write_text(data.model_dump_json(indent=2))

    def finalize(self):
        projects = []
        for f in sorted((self.staging / "builds").glob("*.json")):
            d = ProjectBuilds.model_validate_json(f.read_text())
            projects.append(ProjectEntry(
                id=d.id, name=d.name, repository=d.repository, archived=d.archived
            ))
        
        index = ProjectsIndex(last_updated=now(), projects=projects)
        (self.staging / "projects.json").write_text(index.model_dump_json(indent=2))
        
        log(f"Index: Generated with {len(projects)} projects.")
        shutil.rmtree(self.work, ignore_errors=True)

def main():
    token = os.getenv("GITHUB_TOKEN")
    if token:
        subprocess.run([
            "git", "config", "--global", 
            f"url.https://x-access-token:{token}@github.com/.insteadOf", 
            "https://github.com/"
        ], check=True)

    root = Path(__file__).resolve().parent.parent
    client = docker.from_env()
    orc = Orchestrator(root, client)
    log("Starting build orchestration (Docker isolation)...")
    orc.setup()

    paths = sorted((root / "projects").glob("*.json"))
    configs = [
        (ProjectConfig.model_validate_json(p.read_text()), p.stem) 
        for p in paths
    ]

    # Pre-pull images
    active_images = {
        get_docker_image(c) 
        for c, _ in configs 
        if not c.archived
    }
    log(f"Pre-pulling {len(active_images)} Docker images...")
    for img in sorted(active_images):
        log(f"Pulling {img}...")
        client.images.pull(img)

    for config, pid in configs:
        orc.run_build(pid, config)
    
    orc.finalize()
    log("All tasks complete.")

if __name__ == "__main__":
    main()
