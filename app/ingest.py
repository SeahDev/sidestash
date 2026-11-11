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


# The naming scheme builds use and Sidestash publishes under:
#   <Tweak>_<tweak version>_<App>_<app version>[_<variant>].ipa
#   e.g. Sparkle_1.6.1_Instagram_454.1.0.ipa, YTKACE_1.1.5_YouTube_21.33.6_iOS16.ipa
# Published copies add _<8 hex hash> at the end, which is never a variant.
NAME_SCHEME = re.compile(
    r"^(?P<tweak>[^_/]+)_v?(?P<tweak_version>\d+(?:\.\d+)*)_(?P<app>[^_/]+)_v?(?P<app_version>\d+(?:\.\d+)*)"
    r"(?:_(?!(?:[0-9a-f]{8})\.t?ipa$)(?P<variant>[A-Za-z0-9.-]+?))?(?:_[0-9a-f]{8})?\.t?ipa$")


def parse_name(filename: str) -> dict | None:
    m = NAME_SCHEME.match(Path(filename).name)
    return m.groupdict() if m else None


def tweak_version_from(filename: str) -> str:
    """The tweak version in a standard file name, or "" for any other name
    (set it by hand under Versions → Edit)."""
    parsed = parse_name(filename)
    return parsed["tweak_version"] if parsed else ""


def is_own(app: dict) -> bool:
    """An app you made, not a modified App Store app."""
    return app.get("kind") == "own"


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
    own = is_own(app)

    async def no_appstore():
        return None
    # Your own app: its GitHub release is matched by the app's version, and
    # there's no App Store listing to look up.
    rel, info = await asyncio.gather(
        enrich.github_release(app.get("github_repo", ""), v["version"] if own else v.get("tweakVersion")),
        no_appstore() if own else refresh_appstore(app),
        return_exceptions=True,
    )
    v["github_release"] = rel if isinstance(rel, dict) else None
    # App Store notes describe the *current* store version, so only attach them
    # when this IPA is built on that exact version. Your own app has none.
    if own:
        v["appstore_notes"] = None
    elif isinstance(info, dict) and same_version(info.get("version", ""), v["version"]):
        v["appstore_notes"] = {"version": info["version"], "releaseNotes": info.get("releaseNotes", "")}
    else:
        v.setdefault("appstore_notes", None)
    v["localizedDescription"] = enrich.compose_notes(app, v)


def published_name(app: dict, version: str, tweak_version: str, variant: str | None, sha: str) -> str:
    """<Tweak>_<tweak version>_<App>_<app version>[_<variant>]_<hash>.ipa. The hash
    keeps every published file name unique, so caches can keep files for good."""
    base = app.get("base_app_name") or (app.get("appstore") or {}).get("trackName", "")
    parts = [app["name"], tweak_version, base, version, variant or ""]
    return "_".join(slug(p).replace("_", "-") for p in parts if p) + f"_{sha[:8]}.ipa"


async def ingest(tmp_path: Path, original_name: str, custom_notes: str = "", origin: str = "upload") -> dict:
    meta = await asyncio.to_thread(inspect_ipa, tmp_path)
    if not meta["bundleIdentifier"] or not meta["version"]:
        raise ValueError("The .ipa is missing a bundle ID or version in Info.plist.")
    sha = await asyncio.to_thread(sha256_file, tmp_path)

    existing = next((v for v in store.data["versions"] if v["sha256"] == sha), None)
    if existing:
        tmp_path.unlink(missing_ok=True)
        return {**existing, "duplicate": True}

    created = meta["bundleIdentifier"] not in store.data["apps"]
    app = store.ensure_app(meta["bundleIdentifier"], meta["name"])
    if created and await enrich.appstore_listed(meta["bundleIdentifier"]) is False:
        # A new bundle ID the App Store says it doesn't have is an app you made.
        # (If the App Store couldn't be asked, it stays a tweak; change it on the Listing tab.)
        app["kind"] = "own"
        store.save()
    dest_dir = IPA_DIR / slug(app["bundleIdentifier"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    tweak_version = "" if is_own(app) else tweak_version_from(original_name)
    filename = published_name(app, meta["version"], tweak_version, (parse_name(original_name) or {}).get("variant"), sha)
    await asyncio.to_thread(shutil.move, str(tmp_path), dest_dir / filename)

    v = {
        "id": new_id(),
        "bundleIdentifier": app["bundleIdentifier"],
        "version": meta["version"],
        "buildVersion": meta["buildVersion"],
        "minOSVersion": meta["minOSVersion"],
        "tweakVersion": tweak_version,
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
