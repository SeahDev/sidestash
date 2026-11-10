"""Third-party build tools pinned in a build repo's pins.json.

A build repo (one whose workflows build your IPAs) may keep a pins.json that
fixes each outside tool to an exact commit or release, so a change upstream
can't reach your IPAs until you accept it. This checks each pin against its
upstream and, when you ask, commits an updated pins.json to the build repo.

pins.json maps a key to either a commit pin
    {"name", "about", "repo", "branch", "commit", "hold"?}
or a release pin
    {"name", "about", "repo", "release", "asset", "sha256", "hold"?}
"hold": true keeps "Update all" from touching it.
"""
import base64
import hashlib
import json
import logging
from urllib.parse import quote

import httpx

from .enrich import gh_headers
from .store import now_iso, store

log = logging.getLogger("tools")
API = "https://api.github.com"
PINS_FILE = "pins.json"


class ToolsError(Exception):
    pass


def build_repos() -> list[tuple[str, str]]:
    """Each (repo, branch) an app builds from, once."""
    seen = {}
    for app in store.data["apps"].values():
        cfg = app.get("build") or {}
        if cfg.get("fork_repo"):
            seen.setdefault(cfg["fork_repo"].lower(), (cfg["fork_repo"], cfg.get("ref") or "main"))
    return list(seen.values())


async def _read_pins(c: httpx.AsyncClient, repo: str, branch: str) -> tuple[dict, str] | None:
    """(pins, blob sha) from the repo, or None if it has no pins.json."""
    r = await c.get(f"{API}/repos/{repo}/contents/{PINS_FILE}", params={"ref": branch})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    try:
        return json.loads(base64.b64decode(d["content"])), d["sha"]
    except ValueError as e:
        raise ToolsError(f"{PINS_FILE} in {repo} isn't valid JSON: {e}")


async def _asset_sha256(c: httpx.AsyncClient, asset: dict) -> str:
    """GitHub reports a digest for release assets; older ones are hashed here."""
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        return digest[7:]
    h = hashlib.sha256()
    async with c.stream("GET", asset["browser_download_url"], follow_redirects=True) as r:
        r.raise_for_status()
        async for chunk in r.aiter_bytes(1 << 20):
            h.update(chunk)
    return h.hexdigest()


async def _latest(c: httpx.AsyncClient, pin: dict, with_hash: bool = False) -> dict:
    """What the pin would move to, and how far behind it is."""
    repo = pin["repo"]
    if pin.get("release"):
        r = await c.get(f"{API}/repos/{repo}/releases/latest")
        r.raise_for_status()
        rel = r.json()
        out = {"release": rel["tag_name"], "url": rel.get("html_url", ""), "date": rel.get("published_at", ""),
               "behind": 0 if rel["tag_name"] == pin["release"] else 1}
        if with_hash and out["behind"]:
            asset = next((a for a in rel.get("assets", []) if a["name"] == pin.get("asset")), None)
            if not asset:
                raise ToolsError(f"{repo} {rel['tag_name']} has no asset named {pin.get('asset')}.")
            out["sha256"] = await _asset_sha256(c, asset)
        return out
    branch = pin.get("branch") or "main"
    r = await c.get(f"{API}/repos/{repo}/commits/{quote(branch, safe='')}")
    r.raise_for_status()
    head = r.json()
    out = {"commit": head["sha"], "date": head["commit"]["committer"]["date"], "behind": 0,
           "url": f"https://github.com/{repo}/commits/{branch}"}
    if head["sha"] != pin["commit"]:
        cmp = await c.get(f"{API}/repos/{repo}/compare/{pin['commit']}...{head['sha']}")
        if cmp.status_code == 200:
            d = cmp.json()
            out.update(behind=d.get("ahead_by", 1) or 1, url=d.get("html_url", out["url"]))
        else:
            out["behind"] = 1
    return out


async def _pinned_date(c: httpx.AsyncClient, pin: dict) -> str:
    """When the pinned commit or release was made, for showing its age."""
    try:
        if pin.get("release"):
            r = await c.get(f"{API}/repos/{pin['repo']}/releases/tags/{quote(pin['release'], safe='')}")
            return r.json().get("published_at", "") if r.status_code == 200 else ""
        r = await c.get(f"{API}/repos/{pin['repo']}/commits/{pin['commit']}")
        return r.json()["commit"]["committer"]["date"] if r.status_code == 200 else ""
    except (httpx.HTTPError, KeyError, ValueError):
        return ""


async def check_repo(c: httpx.AsyncClient, repo: str, branch: str) -> dict | None:
    found = await _read_pins(c, repo, branch)
    if not found:
        return None
    pins, _ = found
    out = {"repo": repo, "branch": branch, "checked": now_iso(), "pins": {}, "errors": []}
    for key, pin in pins.items():
        entry = {**pin, "key": key}
        try:
            entry["latest"] = await _latest(c, pin)
            entry["pinned_date"] = await _pinned_date(c, pin)
        except (httpx.HTTPError, ToolsError, KeyError) as e:
            entry["error"] = str(e) if isinstance(e, ToolsError) else f"Couldn't check {pin.get('repo', key)}."
        out["pins"][key] = entry
    return out


async def check_all(c: httpx.AsyncClient | None = None) -> dict:
    """Check every build repo that has a pins.json. Stored under store.data['tools']."""
    own = c is None
    c = c or httpx.AsyncClient(timeout=60, headers=gh_headers(), follow_redirects=True)
    result = {}
    try:
        for repo, branch in build_repos():
            try:
                info = await check_repo(c, repo, branch)
            except (httpx.HTTPError, ToolsError) as e:
                info = {"repo": repo, "branch": branch, "checked": now_iso(), "pins": {},
                        "errors": [str(e) if isinstance(e, ToolsError) else f"Couldn't read {PINS_FILE} from {repo}."]}
            if info:
                result[repo] = info
    finally:
        if own:
            await c.aclose()
    store.data["tools"] = result
    store.save()
    return result


def _commit_error(r: httpx.Response, repo: str) -> ToolsError:
    if r.status_code in (403, 404):
        return ToolsError(f"The token can't write to {repo}. Give it Contents: Read and write on that repo.")
    if r.status_code == 409:
        return ToolsError(f"{PINS_FILE} changed on GitHub while updating. Check again, then retry.")
    return ToolsError(f"GitHub returned an error ({r.status_code}) saving {PINS_FILE}.")


async def update(repo: str, keys: list[str] | None) -> dict:
    """Move pins to their latest version in one commit. keys=None means every
    pin that's behind and not on hold."""
    branch = next((b for r, b in build_repos() if r.lower() == repo.lower()), None)
    if not branch:
        raise ToolsError(f"No app builds from {repo}.")
    async with httpx.AsyncClient(timeout=120, headers=gh_headers(), follow_redirects=True) as c:
        found = await _read_pins(c, repo, branch)
        if not found:
            raise ToolsError(f"{repo} has no {PINS_FILE}.")
        pins, sha = found
        if keys is not None:
            missing = [k for k in keys if k not in pins]
            if missing:
                raise ToolsError(f"{PINS_FILE} has no {', '.join(missing)}.")
        changed = []
        for key, pin in pins.items():
            if keys is None and pin.get("hold"):
                continue
            if keys is not None and key not in keys:
                continue
            latest = await _latest(c, pin, with_hash=True)
            if not latest["behind"]:
                continue
            if pin.get("release"):
                old = pin["release"]
                pin["release"], pin["sha256"] = latest["release"], latest["sha256"]
                changed.append((pin.get("name", key), old, latest["release"], latest["url"]))
            else:
                old = pin["commit"]
                pin["commit"] = latest["commit"]
                changed.append((pin.get("name", key), old[:7], latest["commit"][:7], latest["url"]))
        if not changed:
            raise ToolsError("Already up to date.")
        if len(changed) == 1:
            title = f"Update {changed[0][0]} to {changed[0][2]}"
        else:
            title = f"Update {len(changed)} build tools"
        body = "\n".join(f"- {name}: {old} → {new} ({url})" for name, old, new, url in changed)
        content = json.dumps(pins, indent=2, ensure_ascii=False) + "\n"
        r = await c.put(f"{API}/repos/{repo}/contents/{PINS_FILE}", json={
            "message": f"{title}\n\n{body}\n\nUpdated from Sidestash.",
            "content": base64.b64encode(content.encode()).decode(),
            "sha": sha, "branch": branch,
        })
        if r.status_code not in (200, 201):
            raise _commit_error(r, repo)
        commit_url = r.json().get("commit", {}).get("html_url", "")
        info = await check_repo(c, repo, branch)
    if info:
        store.data.setdefault("tools", {})[repo] = info
        store.save()
    return {"updated": [c[0] for c in changed], "commit_url": commit_url}
