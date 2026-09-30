#!/usr/bin/env python3
"""CI helpers for the releases workflow (stdlib only, no uv needed).

Subcommands (each maps to one workflow step):
  matrix    list projects/*.json stems -> $GITHUB_OUTPUT for the build matrix
  checkout  init/fetch/checkout the gh-pages site repo (sparse for build jobs)
  overlay   merge per-project snapshots into the publish checkout
  message   derive the publish commit message from working-tree status
  push      commit site deltas (no-op when clean) and push with retry
  notify    send notification for newly built projects via Discord Webhook
"""

from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def update_site_files(root: Path, site: Path):
    # 1. .nojekyll
    nojekyll = site / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("")

    # 2. index.html
    src_idx = root / "index.html"
    if src_idx.is_file():
        dest_idx = site / "index.html"
        data = src_idx.read_bytes()
        if not dest_idx.exists() or dest_idx.read_bytes() != data:
            dest_idx.write_bytes(data)
            log("updated index.html")

    # 3. projects.json
    projects = []
    projects_dir = root / "projects"
    for f in sorted((site / "builds").glob("*.json")):
        proj_cfg_file = projects_dir / f.name
        if not proj_cfg_file.exists():
            continue  # removed project: keep files, drop from index
        try:
            d = json.loads(f.read_text())
        except Exception as e:
            log(f"warning: skipping unparsable {f}: {e}")
            continue

        projects.append({
            "id": d.get("id", f.stem),
            "name": d.get("name", f.stem),
            "repository": d.get("repository", ""),
            "archived": d.get("archived", False),
        })

    # Include any projects from projects/ that don't yet have builds recorded
    if projects_dir.is_dir():
        for pf in sorted(projects_dir.glob("*.json")):
            if not any(p["id"] == pf.stem for p in projects):
                try:
                    pcfg = json.loads(pf.read_text())
                    repo = pcfg.get("repository", "")
                    if repo and not repo.startswith(("http", "git@")):
                        repo = f"https://github.com/{repo}"
                    projects.append({
                        "id": pf.stem,
                        "name": pcfg.get("name", pf.stem),
                        "repository": repo,
                        "archived": pcfg.get("archived", False),
                    })
                except Exception:
                    pass

    projects.sort(key=lambda p: p["name"].lower())

    out = site / "projects.json"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if out.exists():
        try:
            old = json.loads(out.read_text())
            if old.get("projects") == projects:
                log(f"Index: Unchanged with {len(projects)} projects.")
                return
        except Exception:
            pass

    index_data = {
        "last_updated": now_iso,
        "projects": projects,
    }
    out.write_text(json.dumps(index_data, indent=2))
    log(f"Index: Generated with {len(projects)} projects.")


def cmd_overlay(args: argparse.Namespace) -> int:
    site = Path(args.site_dir)
    incoming = Path(args.incoming_dir)
    root = Path(getattr(args, "root", "."))
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

    update_site_files(root, site)
    return 0



def changed_pids(site: Path) -> set[str]:
    out = git(site, "status", "--porcelain", "-uall").stdout
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
    built = changed_pids(site)
    if not built:
        log("site: no changes, skipping push")
        (site / ".new_builds").unlink(missing_ok=True)
        return 0
    git(site, "add", "-A")
    if git(site, "diff", "--cached", "--quiet", check=False).returncode == 0:
        log("site: no changes, skipping push")
        (site / ".new_builds").unlink(missing_ok=True)
        return 0
    git(site, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=github-actions[bot]@users.noreply.github.com",
        "commit", "-m", args.message)
    for attempt in (1, 2, 3):
        if git(site, "push", "origin", "gh-pages", check=False).returncode == 0:
            log("pushed")
            Path(site, ".new_builds").write_text(",".join(sorted(built)))
            return 0
        if attempt == 3:
            print("site: push failed after 3 attempts", file=sys.stderr, flush=True)
            return 1
        git(site, "pull", "--rebase", "origin", "gh-pages")
    return 1


def get_site_base_url(root: Path) -> str:
    override = os.environ.get("SITE_URL") or os.environ.get("PAGES_URL")
    if override:
        return override.rstrip("/")

    cfg_file = root / "config.json"
    if cfg_file.is_file():
        try:
            cfg = json.loads(cfg_file.read_text())
            org = cfg.get("org")
            repo = cfg.get("repo")
            if org and repo:
                if repo.lower() == f"{org.lower()}.github.io":
                    return f"https://{org}.github.io"
                return f"https://{org}.github.io/{repo}"
        except Exception as e:
            log(f"warning: failed to parse config.json for base url: {e}")

    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if gh_repo and "/" in gh_repo:
        org, repo = gh_repo.split("/", 1)
        if repo.lower() == f"{org.lower()}.github.io":
            return f"https://{org}.github.io"
        return f"https://{org}.github.io/{repo}"

    return ""


def get_recently_built_pids(site: Path) -> set[str]:
    # 1. Read marker written by cmd_push in the current run
    marker = site / ".new_builds"
    if marker.is_file():
        try:
            content = marker.read_text().strip()
            marker.unlink(missing_ok=True)
            if content:
                return set(p.strip() for p in content.split(",") if p.strip())
        except Exception:
            pass

    # 2. Check uncommitted changes (e.g. before push or in testing)
    uncommitted = changed_pids(site)
    if uncommitted:
        return uncommitted

    return set()


def send_discord_webhook(webhook_url: str, payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Actions-Releases-Notifier/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        log(f"Discord webhook failed (HTTP {e.code}): {err_body}")
        return False
    except Exception as e:
        log(f"Discord webhook error: {e}")
        return False


def build_discord_embed(pid: str, data: dict, base_url: str) -> dict | None:
    builds = data.get("builds") or []
    if not builds:
        return None

    latest = builds[0]
    status = latest.get("build_status", "")
    if status != "success":
        # Only notify when there is actually a successful new build
        return None

    name = data.get("name", pid)
    repo_url = data.get("repository", "")
    commit_hash = latest.get("commit_hash", "")
    commit_msg = latest.get("commit_message", "")
    artifact_name = latest.get("artifact_name", "")
    artifact_path = latest.get("artifact_path", "")
    build_date = latest.get("build_date") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    site_url = f"{base_url}/#{pid}" if base_url else repo_url
    embed = {
        "title": f"New Build: {name}",
        "url": site_url,
        "color": 0x2EB886,
        "fields": [],
        "footer": {
            "text": "Project Releases",
        },
        "timestamp": build_date,
    }

    if commit_hash:
        short_sha = commit_hash[:7]
        commit_url = f"{repo_url.rstrip('/')}/commit/{commit_hash}" if repo_url else ""
        commit_ref = f"[`{short_sha}`]({commit_url})" if commit_url else f"`{short_sha}`"
        msg_summary = commit_msg.splitlines()[0] if commit_msg else "No commit message"
        commit_value = f"{commit_ref} {msg_summary}"
        embed["fields"].append({
            "name": "Commit",
            "value": commit_value[:1024],
            "inline": False,
        })

    if artifact_name:
        if base_url and artifact_path:
            art_url = f"{base_url}/{artifact_path}"
            art_val = f"[`{artifact_name}`]({art_url})"
        else:
            art_val = f"`{artifact_name}`"
        embed["fields"].append({
            "name": "Artifact",
            "value": art_val,
            "inline": True,
        })

    embed["fields"].append({
        "name": "Status",
        "value": "✅ Success",
        "inline": True,
    })

    return embed



def cmd_notify(args: argparse.Namespace) -> int:
    webhook_url = (
        os.environ.get("DISCORD_URL")
        or os.environ.get("DISCORD_WEBHOOK")
        or os.environ.get("DISCORD_WEBHOOK_URL")
        or ""
    ).strip()

    if not webhook_url:
        log("DISCORD_URL not set, skipping notification")
        return 0

    site = Path(args.site_dir)
    root = Path(args.root)

    if args.pids:
        pids = set(p.strip() for p in args.pids.split(",") if p.strip())
    else:
        pids = get_recently_built_pids(site)

    if not pids:
        log("notify: no newly built projects to notify")
        return 0

    base_url = get_site_base_url(root)
    embeds = []

    for pid in sorted(pids):
        data_file = site / "builds" / f"{pid}.json"
        if not data_file.is_file():
            log(f"notify: skipping {pid}, {data_file} not found")
            continue
        try:
            data = json.loads(data_file.read_text())
        except Exception as e:
            log(f"notify: failed to parse {data_file}: {e}")
            continue

        embed = build_discord_embed(pid, data, base_url)
        if embed:
            embeds.append(embed)

    if not embeds:
        log("notify: no valid build data found for notification")
        return 0

    username = os.environ.get("DISCORD_USERNAME", "GitHub Releases")
    avatar_url = os.environ.get(
        "DISCORD_AVATAR_URL",
        "https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png"
    )

    # Send each build as an individual message so Discord displays each build card cleanly
    success = True
    for embed in embeds:
        payload = {
            "username": username,
            "avatar_url": avatar_url,
            "embeds": [embed],
        }
        title = embed.get("title", "build")
        log(f"sending Discord notification for {title}...")
        if not send_discord_webhook(webhook_url, payload):
            success = False
        if len(embeds) > 1:
            time.sleep(0.5)

    return 0 if success else 1



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
    o.add_argument("--root", default=".")

    g = sub.add_parser("message", help="print publish commit message")
    g.add_argument("--site-dir", required=True)
    g.add_argument("--matrix", required=True)

    p = sub.add_parser("push", help="commit deltas and push with retry")
    p.add_argument("--site-dir", required=True)
    p.add_argument("--message", required=True)

    n = sub.add_parser("notify", help="send notification for newly built projects via Discord Webhook")
    n.add_argument("--site-dir", required=True)
    n.add_argument("--root", default=".")
    n.add_argument("--pids", default=None, help="comma-separated list of project IDs")

    args = ap.parse_args()
    return {"matrix": cmd_matrix, "checkout": cmd_checkout, "overlay": cmd_overlay,
            "message": cmd_message, "push": cmd_push, "notify": cmd_notify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

