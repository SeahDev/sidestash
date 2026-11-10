"""Build the public AltStore source JSON."""
import os
import re

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


# The categories AltStore-format sources recognize (anything else shows as Other),
# with the names the dashboard shows for them.
CATEGORIES = {
    "developer": "Developer", "entertainment": "Entertainment", "games": "Games",
    "lifestyle": "Lifestyle", "other": "Other", "photo-video": "Photo & Video",
    "social": "Social", "utilities": "Utilities",
}
# App Store genres (primaryGenreName) that have a match; the rest are Other.
GENRES = {
    "Developer Tools": "developer", "Entertainment": "entertainment", "Music": "entertainment",
    "Games": "games", "Lifestyle": "lifestyle", "Health & Fitness": "lifestyle",
    "Food & Drink": "lifestyle", "Shopping": "lifestyle", "Travel": "lifestyle",
    "Photo & Video": "photo-video", "Graphics & Design": "photo-video",
    "Social Networking": "social", "Utilities": "utilities", "Productivity": "utilities",
}


def category_id(value: str) -> str:
    """'Social', 'photo & video' or 'photo-video' -> 'photo-video'; unknown -> '' ."""
    v = (value or "").strip().lower()
    if v in CATEGORIES:
        return v
    by_name = {name.lower(): cid for cid, name in CATEGORIES.items()}
    return by_name.get(v, "")


def app_category(app: dict) -> str:
    """The category you picked, else the App Store's (mapped), else other."""
    chosen = category_id(app.get("category", ""))
    if chosen:
        return chosen
    genre = (app.get("appstore") or {}).get("primaryGenreName", "")
    return GENRES.get(genre, "other")


def screenshot_entry(url: str) -> dict:
    """AltStore 2 screenshot object. Sizes help store apps lay them out before loading;
    App Store image links end in e.g. /392x696bb.jpg, which gives them."""
    entry = {"imageURL": url}
    m = re.search(r"/(\d+)x(\d+)[a-z]*\.(?:jpe?g|png|webp)$", url)
    if m:
        entry["width"], entry["height"] = int(m.group(1)), int(m.group(2))
    return entry


def version_key(v):
    """Newest version number first; upload date breaks ties (same base, newer tweak)."""
    nums = tuple(int(x) for x in re.findall(r"\d+", v["version"]))
    return (nums, v["date"])


def default_description(app: dict, latest: dict) -> str:
    """Used when you haven't written one: says what the tweak is, not what the base app is."""
    lines = []
    if app.get("subtitle"):
        lines.append(app["subtitle"].rstrip(".") + ".")
    base = app.get("base_app_name")
    by = f" by {app['developerName']}" if app.get("developerName") else ""
    if base:
        lines.append(f"A modified {base}{by}, built on {base} {latest['version']}.")
    if app.get("github_repo"):
        lines.append(f"Source: https://github.com/{app['github_repo']}")
    return "\n\n".join(lines)


def build_source(data: dict, base_url: str) -> dict:
    base = PUBLIC_URL or base_url.rstrip("/")
    src = data["source"]
    apps_out = []
    apps = sorted(data["apps"].values(), key=lambda a: a.get("order", 0))
    for app in apps:
        versions = sorted(
            (v for v in data["versions"] if v["bundleIdentifier"] == app["bundleIdentifier"] and v.get("published")),
            key=version_key, reverse=True,
        )
        if not versions:
            continue
        latest = versions[0]
        store_info = app.get("appstore") or {}
        icon = app.get("iconURL") or store_info.get("artworkUrl512", "")
        if icon.startswith("/"):
            icon = base + icon
        screenshots = app.get("screenshots") or (
            store_info.get("screenshotUrls", []) if app.get("use_appstore_screenshots") else []
        )
        vs = []
        for v in versions:
            entry = {
                "version": v["version"],
                "buildVersion": v["buildVersion"],
                "date": v["date"],
                "localizedDescription": v.get("localizedDescription", ""),
                "downloadURL": f"{base}/ipa/{v['file']}",
                "size": v["size"],
            }
            if v.get("minOSVersion"):
                entry["minOSVersion"] = v["minOSVersion"]
            vs.append(entry)

        apps_out.append({
            "name": app["name"],
            "bundleIdentifier": app["bundleIdentifier"],
            "developerName": app.get("developerName", ""),
            "subtitle": app.get("subtitle", ""),
            "localizedDescription": app.get("localizedDescription", "").strip() or default_description(app, latest),
            "iconURL": icon,
            "tintColor": app.get("tintColor", "").lstrip("#"),
            "category": app_category(app),
            # AltStore 2 objects, plus the AltStore 1 list of links that many store apps still read.
            "screenshots": [screenshot_entry(u) for u in screenshots],
            "screenshotURLs": screenshots,
            "versions": vs,
            "appPermissions": {
                "entitlements": latest.get("entitlements", []),
                "privacy": latest.get("privacy", {}),
            },
            # Flat legacy fields: older AltStore builds and most signer apps
            # (Feather, ESign-style "AltStore repo" importers) read these.
            "version": latest["version"],
            "versionDate": latest["date"],
            "versionDescription": latest.get("localizedDescription", ""),
            "downloadURL": f"{base}/ipa/{latest['file']}",
            "size": latest["size"],
        })

    icon = src.get("iconURL", "")
    if icon.startswith("/"):
        icon = base + icon
    return {
        "name": src.get("name", "Apps"),
        "identifier": src.get("identifier", "com.example.apps"),
        "subtitle": src.get("subtitle", ""),
        "description": src.get("description", ""),
        "iconURL": icon,
        "website": src.get("website") or base,
        "tintColor": src.get("tintColor", "").lstrip("#"),
        "sourceURL": f"{base}/source.json",
        "featuredApps": [a["bundleIdentifier"] for a in apps_out],
        "apps": apps_out,
        "news": [],
    }
