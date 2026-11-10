"""Tiny JSON-file database. One writer (this process), atomic saves."""
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
IPA_DIR = DATA_DIR / "ipas"
ICON_DIR = DATA_DIR / "icons"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "db.json"

for d in (IPA_DIR, ICON_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# The two apps you asked about are pre-configured; anything else is created
# automatically the first time you upload an IPA with a new bundle ID.
PRESETS = {
    "com.google.ios.youtube": {
        "name": "YTKACE",
        "subtitle": "YouTube with downloads, SponsorBlock and Liquid Glass",
        "developerName": "itzzace",
        "tintColor": "#E5322D",
        "category": "entertainment",
        "github_repo": "itzzace/ytkace",
        "appstore_bundle_id": "com.google.ios.youtube",
        "base_app_name": "YouTube",
        "tweak_version_regex": r"YTKACE_v?(\d+(?:\.\d+)+)",
        "build": {
            "fork_repo": "",
            "workflow": "build-ipa.yml",
            "ref": "main",
            "inputs": {"ipa_url": "", "ipa_url_ios17": ""},
        },
    },
    "com.burbn.instagram": {
        "name": "Sparkle",
        "subtitle": "Instagram with downloads, a private gallery and more",
        "developerName": "efibalogh",
        "tintColor": "#C13584",
        "category": "social",
        "github_repo": "efibalogh/sparkle-ig",
        "appstore_bundle_id": "com.burbn.instagram",
        "base_app_name": "Instagram",
        "tweak_version_regex": r"Sparkle[^/]*?_v(\d+(?:\.\d+)+)_IG",
        "build": {
            "fork_repo": "",
            "workflow": "buildapp.yml",
            "ref": "main",
            "inputs": {
                "decrypted_instagram_url": "",
                "upload_artifact": "true",
                "for_sidestore": "false",
                "with_flex": "false",
                "bundle_id": "",
            },
        },
    },
}

DEFAULT_APP = {
    "name": "",
    "subtitle": "",
    "developerName": "",
    "localizedDescription": "",
    "tintColor": "#3A6EA5",
    "category": "",                     # blank: use the App Store's category
    "iconURL": "",
    "screenshots": [],
    "use_appstore_screenshots": True,
    "github_repo": "",
    "appstore_bundle_id": "",
    "base_app_name": "",
    "tweak_version_regex": r"v?(\d+(?:\.\d+)+)",
    "build": {"fork_repo": "", "workflow": "", "ref": "main", "inputs": {}},
    "appstore": {},
    "order": 0,
}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    def __init__(self):
        self._lock = threading.RLock()
        if DB_PATH.exists():
            self.data = json.loads(DB_PATH.read_text())
        else:
            self.data = {
                "source": {
                    "name": "My Apps",
                    "identifier": "com.example.apps",
                    "subtitle": "Personal builds for my devices",
                    "description": "",
                    "iconURL": "",
                    "website": "",
                    "tintColor": "#3A6EA5",
                },
                "apps": {},
                "versions": [],
                "builds": [],
            }
            self.save()

    def save(self):
        with self._lock:
            tmp = DB_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, DB_PATH)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    # ---- apps ----
    def ensure_app(self, bundle_id: str, ipa_name: str = "") -> dict:
        with self._lock:
            apps = self.data["apps"]
            if bundle_id not in apps:
                app = copy.deepcopy(DEFAULT_APP)
                preset = PRESETS.get(bundle_id, {})
                app.update(copy.deepcopy(preset))
                app["bundleIdentifier"] = bundle_id
                app["name"] = app["name"] or ipa_name or bundle_id
                app["appstore_bundle_id"] = app["appstore_bundle_id"] or bundle_id
                app["order"] = len(apps)
                apps[bundle_id] = app
                self.save()
            return apps[bundle_id]

    def seed_presets(self):
        for bid in PRESETS:
            self.ensure_app(bid)
        # Older builds copied the App Store description into the app. Clear any
        # that are still verbatim copies so the tweak description is used.
        changed = False
        for app in self.data["apps"].values():
            store_desc = (app.get("appstore") or {}).get("description", "")
            if store_desc and app.get("localizedDescription", "").strip() == store_desc.strip():
                app["localizedDescription"] = ""
                changed = True
        if changed:
            self.save()

    def versions_for(self, bundle_id: str):
        return [v for v in self.data["versions"] if v["bundleIdentifier"] == bundle_id]

    def find_version(self, vid: str):
        return next((v for v in self.data["versions"] if v["id"] == vid), None)

    def find_build(self, bid: str):
        return next((b for b in self.data["builds"] if b["id"] == bid), None)


store = Store()
