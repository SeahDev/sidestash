"""Watch the original repos for new releases, and bring your forks up to date.

For each app this records:
  - the original repo's newest release, and whether it's newer than what you've published
  - how many commits your fork's build branch is behind the original
and can sync the fork the same way GitHub's "Sync fork" button does.
"""
import asyncio
import logging
import os
import re
from urllib.parse import quote

import httpx

from .enrich import gh_headers
from .store import now_iso, store

log = logging.getLogger("upstream")
API = "https://api.github.com"
CHECK_HOURS = float(os.environ.get("UPDATE_CHECK_HOURS", "6") or 0)


class SyncError(Exception):
    pass


def vtuple(s: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", s or "")]
    while nums and nums[-1] == 0:
        nums.pop()
    return tuple(nums)


def release_version(rel: dict) -> str:
    m = re.search(r"\d+(?:\.\d+)+", f"{rel.get('tag_name', '')} {rel.get('name', '')}")
    return m.group(0) if m else ""


def _published_tweak(bundle_id: str) -> str:
    vs = [v for v in store.data["versions"] if v["bundleIdentifier"] == bundle_id and v.get("tweakVersion")]
    return max((v["tweakVersion"] for v in vs), key=vtuple, default="")


async def _latest_release(c: httpx.AsyncClient, repo: str) -> dict | None:
    r = await c.get(f"{API}/repos/{repo}/releases", params={"per_page": 10})
    if r.status_code == 404:
        raise SyncError(f"GitHub can't find {repo}.")
    r.raise_for_status()
    rels = [x for x in r.json() if not x.get("draft")]
    rel = next((x for x in rels if not x.get("prerelease")), rels[0] if rels else None)
    if not rel:
        return None
    return {"tag": rel.get("tag_name", ""), "name": rel.get("name") or rel.get("tag_name", ""),
            "url": rel.get("html_url", ""), "published_at": rel.get("published_at", ""),
            "version": release_version(rel), "prerelease": bool(rel.get("prerelease"))}


async def _fork_status(c: httpx.AsyncClient, fork: str, ref: str) -> dict:
    r = await c.get(f"{API}/repos/{fork}")
    if r.status_code == 404:
        raise SyncError(f"GitHub can't find your fork {fork}, or your token doesn't include it.")
    r.raise_for_status()
    repo = r.json()
    parent = repo.get("parent") or {}
    info = {"default_branch": repo.get("default_branch", "main"), "ref": ref or repo.get("default_branch", "main"),
            "parent": parent.get("full_name", ""), "parent_branch": "", "behind": None, "ahead": None, "compare_url": ""}
    if not parent:
        info["note"] = "This repo isn't a fork, so there's nothing to sync from."
        return info
    # Compare against the original's branch of the same name if it has one, else its default branch.
    b = await c.get(f"{API}/repos/{parent['full_name']}/branches/{quote(info['ref'], safe='')}")
    info["parent_branch"] = info["ref"] if b.status_code == 200 else parent.get("default_branch", "main")
    owner = parent["full_name"].split("/")[0]
    cmp = await c.get(f"{API}/repos/{fork}/compare/{quote(info['ref'], safe='')}...{owner}:{quote(info['parent_branch'], safe='')}")
    if cmp.status_code == 200:
        d = cmp.json()
        info.update(behind=d.get("ahead_by", 0), ahead=d.get("behind_by", 0), compare_url=d.get("html_url", ""))
    return info


async def check_app(c: httpx.AsyncClient, app: dict) -> dict:
    out = {"checked": now_iso(), "release": None, "fork": None, "errors": []}
    repo, cfg = app.get("github_repo"), app.get("build") or {}
    if cfg.get("fork_repo"):
        try:
            out["fork"] = await _fork_status(c, cfg["fork_repo"], cfg.get("ref") or "")
        except (SyncError, httpx.HTTPError) as e:
            out["errors"].append(f"Fork: {e}")
    # If the "original repo" is really your fork, watch the repo it was forked from.
    parent = (out["fork"] or {}).get("parent")
    if parent and (not repo or repo.lower() == cfg["fork_repo"].lower()):
        repo = parent
        out["release_repo"] = parent
    if repo:
        try:
            rel = await _latest_release(c, repo)
            if rel:
                mine = _published_tweak(app["bundleIdentifier"])
                rel["published_tweak"] = mine
                rel["newer"] = bool(rel["version"] and (not mine or vtuple(rel["version"]) > vtuple(mine)))
            out["release"] = rel
        except (SyncError, httpx.HTTPError) as e:
            out["errors"].append(f"Releases: {e}")
    app["upstream"] = out
    return out


async def check_all() -> dict:
    async with httpx.AsyncClient(timeout=30, headers=gh_headers()) as c:
        apps = list(store.data["apps"].values())
        await asyncio.gather(*(check_app(c, a) for a in apps))
    store.save()
    return {a["bundleIdentifier"]: a.get("upstream") for a in store.data["apps"].values()}


def _sync_error(r: httpx.Response, fork: str, branch: str) -> SyncError:
    try:
        msg = r.json().get("message", "")
    except ValueError:
        msg = r.text[:200]
    if "workflow" in msg.lower():
        return SyncError("The update changes workflow files, so the token also needs Workflows: read and write on this fork.")
    if r.status_code in (401, 403):
        return SyncError("The token can't update this fork. It needs Contents: read and write on it.")
    if r.status_code == 409:
        return SyncError(f"Your changes on {branch} conflict with the original. Open {fork} on GitHub and resolve it there.")
    return SyncError(f"GitHub couldn't sync {branch} ({r.status_code}): {msg}")


async def sync_fork(app: dict) -> dict:
    """Bring the build branch up to date with the original, like GitHub's Sync fork button."""
    cfg = app.get("build") or {}
    fork = cfg.get("fork_repo")
    if not fork:
        raise SyncError("Set your fork first.")
    async with httpx.AsyncClient(timeout=60, headers=gh_headers()) as c:
        info = await _fork_status(c, fork, cfg.get("ref") or "")
        if not info["parent"]:
            raise SyncError(info.get("note", "Not a fork."))
        ref, steps = info["ref"], []
        if info["parent_branch"] == ref:
            # The original has this branch too: sync it directly.
            r = await c.post(f"{API}/repos/{fork}/merge-upstream", json={"branch": ref})
            if r.status_code != 200:
                raise _sync_error(r, fork, ref)
            steps.append(r.json().get("message", "Synced."))
        else:
            # Your build branch only exists in your fork (e.g. one holding your own edits):
            # sync the default branch, then merge it into the build branch.
            default = info["default_branch"]
            r = await c.post(f"{API}/repos/{fork}/merge-upstream", json={"branch": default})
            if r.status_code != 200:
                raise _sync_error(r, fork, default)
            steps.append(r.json().get("message", f"Synced {default}."))
            if ref != default:
                m = await c.post(f"{API}/repos/{fork}/merges",
                                 json={"base": ref, "head": default, "commit_message": f"Merge {default} (synced from {info['parent']}) into {ref}"})
                if m.status_code == 201:
                    steps.append(f"Merged {default} into {ref}.")
                elif m.status_code == 204:
                    steps.append(f"{ref} already had everything from {default}.")
                else:
                    raise _sync_error(m, fork, ref)
        # The workflow may have changed: have the dashboard read its inputs again.
        cfg.pop("schema", None)
        await check_app(c, app)
    store.save()
    return {"steps": steps, "upstream": app["upstream"]}


async def loop():
    """Check every app now and then every CHECK_HOURS hours."""
    if CHECK_HOURS <= 0:
        return
    await asyncio.sleep(20)  # let startup finish
    while True:
        try:
            await check_all()
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(CHECK_HOURS * 3600)
