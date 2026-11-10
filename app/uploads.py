"""Decrypted IPAs you upload for a build, served at a secret link GitHub can download.

These are inputs to a build, never part of the source. Forks of public repos
have public Actions logs, and workflows often print their inputs, so treat a
link as visible to anyone once a build has used it. To keep that window small,
each upload is deleted once a build that used it works, or after MAX_AGE.
A failed run leaves it in place so you can try again. MAX_DOWNLOADS caps how
many times the link can be fetched meanwhile.
"""
import asyncio
import ipaddress
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from .store import DATA_DIR, new_id, now_iso, store

UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_AGE = float(os.environ.get("UPLOAD_LINK_HOURS", "3") or 3) * 3600
MAX_DOWNLOADS = int(os.environ.get("UPLOAD_LINK_DOWNLOADS", "5") or 5)
LINK = re.compile(r"/base/([A-Za-z0-9_-]{20,})/")


def _list() -> list[dict]:
    return store.data.setdefault("uploads", [])


def add(tmp: Path, original_name: str, meta: dict) -> dict:
    stored = f"{new_id()}.ipa"
    tmp.rename(UPLOAD_DIR / stored)
    u = {
        "id": new_id(), "token": secrets.token_urlsafe(24), "stored": stored,
        "filename": re.sub(r"[^A-Za-z0-9._-]", "_", original_name) or "app.ipa",
        "bundleIdentifier": meta.get("bundleIdentifier", ""), "version": meta.get("version", ""),
        "size": (UPLOAD_DIR / stored).stat().st_size, "added": now_iso(), "added_ts": time.time(),
    }
    _list().append(u)
    store.save()
    return u


def find(token: str, filename: str, count: bool = True) -> Path | None:
    """The file behind a secret link, or None if the link is wrong, expired or used up.

    count=False for requests that don't start a new download (HEAD, resumed ranges).
    """
    u = next((u for u in _list() if secrets.compare_digest(u["token"].encode(), token.encode())), None)
    if not u or u["filename"] != filename:
        return None
    if time.time() - u.get("added_ts", 0) > MAX_AGE:
        remove(u["id"])
        return None
    if count:
        if u.get("downloads", 0) >= MAX_DOWNLOADS:
            return None
        u["downloads"] = u.get("downloads", 0) + 1
        store.save()
    return UPLOAD_DIR / u["stored"]


def remove(uid: str) -> bool:
    u = next((u for u in _list() if u["id"] == uid), None)
    if not u:
        return False
    (UPLOAD_DIR / u["stored"]).unlink(missing_ok=True)
    _list().remove(u)
    forget_dead_links()
    store.save()
    return True


def forget_dead_links() -> None:
    """Blank saved build inputs that point to an upload that no longer exists,
    so the form doesn't offer a link that would 404."""
    live = {u["token"] for u in _list()}
    for app in store.data["apps"].values():
        saved = (app.get("build") or {}).get("inputs") or {}
        for k, v in saved.items():
            m = LINK.search(str(v))
            if m and m.group(1) not in live:
                saved[k] = ""


def remove_linked(values) -> None:
    """Delete uploads whose secret link appears in these build inputs."""
    tokens = {m.group(1) for v in values for m in LINK.finditer(str(v))}
    for u in [u for u in _list() if u["token"] in tokens]:
        remove(u["id"])


def check_links(values) -> None:
    """Raise LookupError if these build inputs point to an upload that's gone."""
    live = {u["token"] for u in _list() if time.time() - u.get("added_ts", 0) <= MAX_AGE}
    if any(m.group(1) not in live for v in values for m in LINK.finditer(str(v))):
        raise LookupError("The IPA you uploaded for this build has been deleted. Upload it again and start a new build.")


async def prune_loop() -> None:
    """Delete expired uploads as they expire, not only at startup."""
    while True:
        await asyncio.sleep(300)
        prune()


def prune() -> None:
    cutoff = time.time() - MAX_AGE
    for u in [u for u in _list() if u.get("added_ts", 0) < cutoff]:
        remove(u["id"])
    forget_dead_links()   # also catches links whose upload was deleted by an older version
    store.save()


def link(base: str, u: dict) -> str:
    return f"{base}/base/{u['token']}/{u['filename']}"


def reachable_from_internet(base: str) -> bool:
    """Best guess at whether GitHub's runners can download from this address."""
    host = (urlparse(base).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".lan", ".home", ".internal", ".ts.net")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a real hostname, e.g. apps.example.com
    return ip.is_global and ip not in ipaddress.ip_network("100.64.0.0/10")
