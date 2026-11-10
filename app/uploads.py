"""Decrypted IPAs you upload for a build, served at a secret link GitHub can download.

These are inputs to a build, never part of the source. Forks of public repos
have public Actions logs, and workflows often print their inputs, so treat a
link as visible to anyone once a build has used it. To keep that window small,
each upload is deleted as soon as the GitHub run that used it finishes
(whether it worked or not), after MAX_DOWNLOADS downloads, or after MAX_AGE,
whichever comes first.
"""
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
MAX_AGE = float(os.environ.get("UPLOAD_LINK_HOURS", "6") or 6) * 3600
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
    store.save()
    return True


def remove_linked(values) -> None:
    """Delete uploads whose secret link appears in these build inputs."""
    tokens = {m.group(1) for v in values for m in LINK.finditer(str(v))}
    for u in [u for u in _list() if u["token"] in tokens]:
        remove(u["id"])


def prune() -> None:
    cutoff = time.time() - MAX_AGE
    for u in [u for u in _list() if u.get("added_ts", 0) < cutoff]:
        remove(u["id"])


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
