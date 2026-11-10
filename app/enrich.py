"""Changelogs from GitHub releases and release notes from the App Store."""
import os
import re

import httpx

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
UA = {"User-Agent": "sidestash"}


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", **UA}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def clean_markdown(text: str) -> str:
    """AltStore shows plain-ish text, so drop images, HTML and badge noise."""
    text = re.sub(r"<!--.*?-->", "", text or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)      # links -> text
    text = re.sub(r"<[^>]+>", "", text)                       # html tags
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)        # headings -> plain lines
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def github_release(repo: str, tweak_version: str | None) -> dict | None:
    """Release matching the tweak version, else the newest stable release."""
    if not repo:
        return None
    async with httpx.AsyncClient(timeout=20, headers=gh_headers()) as c:
        r = await c.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 30})
        if r.status_code != 200:
            return None
        releases = [x for x in r.json() if not x.get("draft")]
    if not releases:
        return None
    pick = None
    if tweak_version:
        pat = re.compile(rf"(?<![\d.]){re.escape(tweak_version)}(?![\d])")
        pick = next((x for x in releases if pat.search(f"{x.get('tag_name','')} {x.get('name','')}")), None)
    pick = pick or next((x for x in releases if not x.get("prerelease")), releases[0])
    return {
        "tag": pick.get("tag_name", ""),
        "name": pick.get("name") or pick.get("tag_name", ""),
        "body": clean_markdown(pick.get("body", "")),
        "url": pick.get("html_url", ""),
        "published_at": pick.get("published_at", ""),
        "matched": bool(tweak_version and pick.get("tag_name") and tweak_version in (pick.get("tag_name", "") + pick.get("name", ""))),
    }


async def appstore_lookup(bundle_id: str, country: str = "us") -> dict:
    if not bundle_id:
        return {}
    async with httpx.AsyncClient(timeout=20, headers=UA) as c:
        r = await c.get("https://itunes.apple.com/lookup", params={"bundleId": bundle_id, "country": country})
        if r.status_code != 200:
            return {}
        results = r.json().get("results", [])
    if not results:
        return {}
    x = results[0]
    return {
        "trackName": x.get("trackName", ""),
        "version": x.get("version", ""),
        "releaseNotes": (x.get("releaseNotes") or "").strip(),
        "currentVersionReleaseDate": x.get("currentVersionReleaseDate", ""),
        "artworkUrl512": x.get("artworkUrl512", ""),
        "screenshotUrls": x.get("screenshotUrls", []),
        "description": x.get("description", ""),
        "trackViewUrl": x.get("trackViewUrl", ""),
        "minimumOsVersion": x.get("minimumOsVersion", ""),
    }


def compose_notes(app: dict, version: dict) -> str:
    """The text shown under a version in AltStore."""
    parts = []
    if version.get("custom_notes"):
        parts.append(version["custom_notes"].strip())

    rel = version.get("github_release") or {}
    if rel.get("body"):
        title = f"{app.get('name')} {version.get('tweakVersion') or rel.get('tag', '')}".strip()
        parts.append(f"{title}\n{rel['body']}")

    store_notes = version.get("appstore_notes") or {}
    if store_notes.get("releaseNotes"):
        base = app.get("base_app_name") or "Base app"
        parts.append(f"{base} {store_notes.get('version', version.get('version'))}\n{store_notes['releaseNotes']}")

    if not parts:
        base = app.get("base_app_name")
        tv = version.get("tweakVersion")
        parts.append(" · ".join(p for p in [f"{app.get('name')} {tv}" if tv else "", f"{base} {version.get('version')}" if base else f"Version {version.get('version')}"] if p))
    return "\n\n".join(parts)
