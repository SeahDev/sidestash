"""News items for the source: AltStore shows them in its News tab."""
import datetime as dt
import re

from .store import new_id, now_iso, store

HEX = re.compile(r"#?[0-9A-Fa-f]{6}")


class NewsError(ValueError):
    pass


def items() -> list[dict]:
    return store.data.setdefault("news", [])


def find(nid: str) -> dict | None:
    return next((n for n in items() if n["id"] == nid), None)


def _clean(body: dict, current: dict | None = None) -> dict:
    """Validate the fields a person can set; the rest (id, created) belong to the server."""
    n = dict(current or {})
    for key in ("title", "caption", "url", "imageURL", "tintColor", "appID", "date"):
        if key in body:
            n[key] = str(body[key] or "").strip()
    for key in ("notify", "published"):
        if key in body:
            n[key] = bool(body[key])

    if not n.get("title"):
        raise NewsError("Give the news item a title.")
    if len(n["title"]) > 120:
        raise NewsError("Keep the title under 120 characters; it's shown on one or two lines.")
    if not n.get("caption"):
        raise NewsError("Write a caption: a sentence or two under the title.")
    if n.get("url") and not re.match(r"https?://", n["url"]):
        raise NewsError("The link has to start with https:// or http://.")
    img = n.get("imageURL", "")
    if img and not (re.match(r"https?://", img) or img.startswith("/icons/")):
        raise NewsError("The image has to be a web link or an image you uploaded here.")
    if n.get("tintColor") and not HEX.fullmatch(n["tintColor"]):
        raise NewsError("Tint color should be a hex color like #0071E3.")
    if n.get("appID") and n["appID"] not in store.data["apps"]:
        raise NewsError("That app isn't in the dashboard any more.")
    try:
        when = dt.datetime.fromisoformat((n.get("date") or now_iso()).replace("Z", "+00:00"))
    except ValueError:
        raise NewsError("The date isn't valid.")
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    n["date"] = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n.setdefault("notify", False)
    n.setdefault("published", True)
    return n


def create(body: dict) -> dict:
    n = _clean(body)
    n["id"] = new_id()
    n["created"] = now_iso()
    items().append(n)
    store.save()
    return n


def update(nid: str, body: dict) -> dict:
    """Validate first, so a rejected edit leaves the item as it was."""
    cur = find(nid)
    if not cur:
        raise KeyError(nid)
    cleaned = _clean(body, current=dict(cur))
    cur.clear()
    cur.update(cleaned)
    store.save()
    return cur


def delete(nid: str) -> bool:
    cur = find(nid)
    if not cur:
        return False
    items().remove(cur)
    store.save()
    return True


def for_source(data: dict, base: str) -> list[dict]:
    """Published items, newest first, in AltStore's news format. `data` is a store snapshot."""
    out = []
    for n in sorted((n for n in data.get("news", []) if n.get("published", True)), key=lambda n: n["date"], reverse=True):
        entry = {"title": n["title"], "identifier": n["id"], "caption": n["caption"],
                 "date": n["date"], "notify": bool(n.get("notify"))}
        if n.get("tintColor"):
            entry["tintColor"] = n["tintColor"].lstrip("#")
        if n.get("imageURL"):
            entry["imageURL"] = base + n["imageURL"] if n["imageURL"].startswith("/") else n["imageURL"]
        if n.get("url"):
            entry["url"] = n["url"]
        if n.get("appID") and n["appID"] in data["apps"]:
            entry["appID"] = n["appID"]
        out.append(entry)
    return out
