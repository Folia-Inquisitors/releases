#!/usr/bin/env python3
"""CI helpers for the releases workflow (stdlib only, no uv needed).

Subcommands (each maps to one workflow step):
  matrix    list projects/*.json stems -> $GITHUB_OUTPUT for the build matrix
  checkout  init/fetch/checkout the gh-pages site repo (sparse for build jobs)
  overlay   merge per-project snapshots into the publish checkout
  message   derive the publish commit message from working-tree status
  push      commit site deltas (no-op when clean) and push with retry
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def log(msg: str):
    print(f"[ci] {msg}", flush=True)


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr, flush=True)
        sys.exit(r.returncode)
    return r


def git(site: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh("git", "-C", str(site), *args, check=check)


def origin_url() -> str:
    override = os.environ.get("CI_GIT_REMOTE")
    if override:
        return override
    token = os.environ["GITHUB_TOKEN"]
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    host = server.removeprefix("https://").removeprefix("http://")
    repo = os.environ["GITHUB_REPOSITORY"]
    return f"https://x-access-token:{token}@{host}/{repo}.git"


def cmd_matrix(args: argparse.Namespace) -> int:
    root = Path(args.root)
    pids = sorted(p.stem for p in (root / "projects").glob("*.json"))
    line = "matrix=" + json.dumps(pids)
    if args.output:
        with open(args.output, "a") as f:
            f.write(line + "\n")
    log(line)
    return 0


def cmd_checkout(args: argparse.Namespace) -> int:
    site = Path(args.site_dir)
    site.mkdir(parents=True, exist_ok=True)
    if not (site / ".git").is_dir():
        sh("git", "init", str(site))
    url = origin_url()
    existing = sh("git", "-C", str(site), "remote", "get-url", "origin", check=False)
    if existing.returncode == 0:
        git(site, "remote", "set-url", "origin", url)
    else:
        git(site, "remote", "add", "origin", url)
    git(site, "fetch", "--depth", "1", "origin", "gh-pages:gh-pages", check=False)
    has = git(site, "rev-parse", "--verify", "--quiet", "gh-pages", check=False)
    if has.returncode == 0:
        git(site, "checkout", "gh-pages")
        if args.sparse:
            git(site, "sparse-checkout", "set", "--no-cone",
                f"builds/{args.sparse}.json", f"artifacts/{args.sparse}/")
            log(f"sparse: builds/{args.sparse}.json artifacts/{args.sparse}/")
    else:
        git(site, "checkout", "--orphan", "gh-pages")
        log("orphan gh-pages (first run)")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    site = Path(args.site_dir)
    incoming = Path(args.incoming_dir)
    matrix = json.loads(args.matrix)
    for pid in matrix:
        src = incoming / f"site-{pid}"
        if not src.is_dir():
            log(f"no snapshot for {pid}, keeping published state")
            continue
        (site / "builds").mkdir(parents=True, exist_ok=True)
        snap = src / "builds" / f"{pid}.json"
        if snap.is_file():
            (site / "builds" / f"{pid}.json").write_bytes(snap.read_bytes())
        snap_art = src / "artifacts" / pid
        if snap_art.is_dir():
            dest = site / "artifacts" / pid
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(snap_art, dest, dirs_exist_ok=True)
            # mirror retention deletions made inside the build job
            for existing in sorted(dest.iterdir()):
                if existing.is_dir() and not (snap_art / existing.name).is_dir():
                    shutil.rmtree(existing, ignore_errors=True)
                    log(f"pruned artifacts/{pid}/{existing.name}")
        log(f"overlaid {pid}")
    return 0


def changed_pids(site: Path) -> set[str]:
    out = git(site, "status", "--porcelain").stdout
    built = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        p = parts[-1]  # last field: plain path, or new path of a rename
        if p.startswith("builds/") and p.endswith(".json"):
            built.add(p[len("builds/"):-len(".json")])
    return built


def cmd_message(args: argparse.Namespace) -> int:
    site = Path(args.site_dir)
    matrix = json.loads(args.matrix)
    built = changed_pids(site)
    skipped = [p for p in matrix if p not in built]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"chore(releases): {date} "
          f"built=[{','.join(sorted(built)) or 'none'}] "
          f"skipped=[{','.join(skipped) or 'none'}]")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    site = Path(args.site_dir)
    git(site, "add", "-A")
    if git(site, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log("site: no changes, skipping push")
        return 0
    git(site, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=github-actions[bot]@users.noreply.github.com",
        "commit", "-m", args.message)
    for attempt in (1, 2, 3):
        if git(site, "push", "origin", "gh-pages", check=False).returncode == 0:
            log("pushed")
            return 0
        if attempt == 3:
            print("site: push failed after 3 attempts", file=sys.stderr, flush=True)
            return 1
        git(site, "pull", "--rebase", "origin", "gh-pages")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="CI helpers for the releases workflow")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("matrix", help="emit build matrix to GITHUB_OUTPUT")
    m.add_argument("--root", default=".")
    m.add_argument("--output", default=None)

    c = sub.add_parser("checkout", help="checkout the gh-pages site repo")
    c.add_argument("--site-dir", required=True)
    c.add_argument("--sparse", default=None, help="pid for sparse build-job checkout")

    o = sub.add_parser("overlay", help="merge per-project snapshots into site")
    o.add_argument("--site-dir", required=True)
    o.add_argument("--incoming-dir", required=True)
    o.add_argument("--matrix", required=True, help="JSON pid list from plan job")

    g = sub.add_parser("message", help="print publish commit message")
    g.add_argument("--site-dir", required=True)
    g.add_argument("--matrix", required=True)

    p = sub.add_parser("push", help="commit deltas and push with retry")
    p.add_argument("--site-dir", required=True)
    p.add_argument("--message", required=True)

    args = ap.parse_args()
    return {"matrix": cmd_matrix, "checkout": cmd_checkout, "overlay": cmd_overlay,
            "message": cmd_message, "push": cmd_push}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
