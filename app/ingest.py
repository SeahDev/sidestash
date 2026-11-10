"""Turn an .ipa on disk into a published version in the source."""
import asyncio
import os
import re
import shutil
from pathlib import Path

from . import enrich
from .ipa import inspect_ipa, sha256_file
from .store import IPA_DIR, new_id, now_iso, store


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "app"


def tweak_version_from(app: dict, filename: str) -> str:
    pattern = app.get("tweak_version_regex") or ""
    try:
        m = re.search(pattern, filename) if pattern else None
    except re.error:
        m = None
    return m.group(1) if m and m.groups() else ""


async def refresh_appstore(app: dict) -> dict:
    info = await enrich.appstore_lookup(app.get("appstore_bundle_id") or app["bundleIdentifier"])
    if info:
        # Don't copy the App Store description into the app: store apps such as
        # FlareStore already show their own "On the App Store" card, so it
        # would appear twice. The source builds a tweak description instead.
        app["appstore"] = info
        store.save()
    return info


def same_version(a: str, b: str) -> bool:
    """'454.1' and '454.1.0' are the same version; trailing zeros don't count."""
    def parts(s):
        nums = [int(x) for x in re.findall(r"\d+", s or "")]
        while nums and nums[-1] == 0:
            nums.pop()
        return nums
    return bool(a and b) and parts(a) == parts(b)


async def enrich_version(app: dict, v: dict) -> None:
    rel, info = await asyncio.gather(
        enrich.github_release(app.get("github_repo", ""), v.get("tweakVersion")),
        refresh_appstore(app),
        return_exceptions=True,
    )
    v["github_release"] = rel if isinstance(rel, dict) else None
    # App Store notes describe the *current* store version, so only attach them
    # when this IPA is built on that exact version.
    if isinstance(info, dict) and same_version(info.get("version", ""), v["version"]):
        v["appstore_notes"] = {"version": info["version"], "releaseNotes": info.get("releaseNotes", "")}
    else:
        v.setdefault("appstore_notes", None)
    v["localizedDescription"] = enrich.compose_notes(app, v)


async def ingest(tmp_path: Path, original_name: str, custom_notes: str = "", origin: str = "upload") -> dict:
    meta = await asyncio.to_thread(inspect_ipa, tmp_path)
    if not meta["bundleIdentifier"] or not meta["version"]:
        raise ValueError("The .ipa is missing a bundle ID or version in Info.plist.")
    sha = await asyncio.to_thread(sha256_file, tmp_path)

    existing = next((v for v in store.data["versions"] if v["sha256"] == sha), None)
    if existing:
        tmp_path.unlink(missing_ok=True)
        return {**existing, "duplicate": True}

    app = store.ensure_app(meta["bundleIdentifier"], meta["name"])
    dest_dir = IPA_DIR / slug(app["bundleIdentifier"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slug(app['name'])}_{slug(meta['version'])}_{sha[:8]}.ipa"
    await asyncio.to_thread(shutil.move, str(tmp_path), dest_dir / filename)

    v = {
        "id": new_id(),
        "bundleIdentifier": app["bundleIdentifier"],
        "version": meta["version"],
        "buildVersion": meta["buildVersion"],
        "minOSVersion": meta["minOSVersion"],
        "tweakVersion": tweak_version_from(app, original_name),
        "date": now_iso(),
        "size": os.path.getsize(dest_dir / filename),
        "sha256": sha,
        "file": f"{slug(app['bundleIdentifier'])}/{filename}",
        "original_name": original_name,
        "origin": origin,
        "privacy": meta["privacy"],
        "entitlements": meta["entitlements"],
        "custom_notes": custom_notes,
        "published": True,
    }
    try:
        await enrich_version(app, v)
    except Exception as e:  # enrichment is a nice-to-have; never lose an upload over it
        v["localizedDescription"] = enrich.compose_notes(app, v)
        v["enrich_error"] = str(e)
    store.data["versions"].append(v)
    store.save()
    return v
