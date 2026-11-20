"""Screenshots linked from private GitHub repos. Phones can't sign in to GitHub to
load them, so Sidestash reads them with its tokens and serves copies from /icons/.
Links to public repos and other sites are left as they are."""
import hashlib
import logging
import re
import struct

import httpx

from .enrich import GITHUB_RELEASES_TOKEN, GITHUB_TOKEN, gh_headers
from .ingest import slug
from .store import ICON_DIR, store

log = logging.getLogger("sidestash.shots")
API = "https://api.github.com"
MAX_BYTES = 20 * 1024 * 1024

# github.com/<owner>/<repo>/blob|raw/<ref>/<path>  and  raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>
GH_LINK = re.compile(
    r"^https://(?:github\.com/(?P<o1>[\w.-]+)/(?P<r1>[\w.-]+)/(?:blob|raw)/|raw\.githubusercontent\.com/(?P<o2>[\w.-]+)/(?P<r2>[\w.-]+)/)"
    r"(?P<rest>[^?#]+)")
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif"}


class ShotError(Exception):
    pass


def parse(url: str) -> tuple[str, str, str, str] | None:
    """(owner, repo, ref, path) for a link to a file on GitHub, else None.
    A ref is taken as one path segment, or refs/heads/<name> / refs/tags/<name>."""
    m = GH_LINK.match(url.strip())
    if not m:
        return None
    parts = m["rest"].split("/")
    n = 3 if parts[0] == "refs" and len(parts) > 3 else 1
    if len(parts) <= n:
        return None
    return m["o1"] or m["o2"], m["r1"] or m["r2"], "/".join(parts[:n]), "/".join(parts[n:])


def image_type(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((ext for magic, ext in IMAGE_TYPES.items() if data.startswith(magic)), None)


def _tokens() -> list[str]:
    return list(dict.fromkeys(t for t in (GITHUB_TOKEN, GITHUB_RELEASES_TOKEN) if t))


async def _private_token(c: httpx.AsyncClient, owner: str, repo: str, cache: dict) -> str | None:
    """The token that can read a private repo; None for a public one."""
    key = f"{owner}/{repo}".lower()
    if key not in cache:
        cache[key] = ShotError(f"No GitHub token can read {owner}/{repo}.")
        r = await c.get(f"{API}/repos/{owner}/{repo}", headers=gh_headers(""))
        if r.status_code == 200 and not r.json().get("private"):
            cache[key] = None
        else:
            for token in _tokens():
                r = await c.get(f"{API}/repos/{owner}/{repo}", headers=gh_headers(token))
                if r.status_code == 200:
                    cache[key] = token if r.json().get("private") else None
                    break
    if isinstance(cache[key], ShotError):
        raise cache[key]
    return cache[key]


async def _fetch(c: httpx.AsyncClient, url: str, cache: dict) -> bytes | None:
    owner, repo, ref, path = parse(url)
    token = await _private_token(c, owner, repo, cache)
    if token is None:
        return None
    r = await c.get(f"{API}/repos/{owner}/{repo}/contents/{path}", params={"ref": ref},
                    headers={**gh_headers(token), "Accept": "application/vnd.github.raw"})
    if r.status_code == 404:
        raise ShotError(f"{path} isn't in {owner}/{repo} at {ref}.")
    if r.status_code != 200:
        raise ShotError(f"GitHub answered {r.status_code} for {path}.")
    if len(r.content) > MAX_BYTES:
        raise ShotError(f"{path} is over 20 MB.")
    return r.content


async def sync(app: dict) -> None:
    """Copy the app's private-repo screenshots, drop copies no longer listed. A link
    that can't be read right now keeps its last copy."""
    prefix = f"{slug(app['bundleIdentifier'])}-shot-"
    old, mirrors, problems, cache = app.get("screenshot_mirrors") or {}, {}, [], {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        for url in app.get("screenshots") or []:
            if url in mirrors or not parse(url):
                continue
            try:
                data = await _fetch(c, url, cache)
                if data is None:
                    continue      # public repo: phones can load the link itself
                ext = image_type(data)
                if not ext:
                    raise ShotError(f"{parse(url)[3]} isn't a PNG, JPEG, GIF or WebP image.")
            except (ShotError, httpx.HTTPError) as e:
                problems.append(str(e) if isinstance(e, ShotError) else f"Couldn't reach GitHub: {e}")
                if url in old and (ICON_DIR / old[url]["path"].rsplit("/", 1)[-1]).is_file():
                    mirrors[url] = old[url]
                continue
            name = f"{prefix}{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
            if not (ICON_DIR / name).is_file():
                (ICON_DIR / name).write_bytes(data)
            entry = {"path": f"/icons/{name}"}
            if ext == "png" and data[12:16] == b"IHDR":   # sizes help store apps lay out before loading
                entry["width"], entry["height"] = struct.unpack(">II", data[16:24])
            mirrors[url] = entry
    keep = {m["path"].rsplit("/", 1)[-1] for m in mirrors.values()}
    for f in ICON_DIR.glob(f"{prefix}*"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
    app["screenshot_mirrors"], app["screenshot_problems"] = mirrors, problems
    store.save()


async def sync_all() -> None:
    """Pick up screenshots changed in the repos since they were copied."""
    for app in list(store.data["apps"].values()):
        if any(parse(u) for u in app.get("screenshots") or []) or app.get("screenshot_mirrors"):
            try:
                await sync(app)
            except Exception:
                log.exception("screenshot copy failed for %s", app.get("bundleIdentifier"))
