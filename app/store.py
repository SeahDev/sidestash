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

# Apps are created when you add one in the dashboard or upload an IPA with a new bundle ID.
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
    "featured": True,                   # listed in the source's featuredApps
    # "tweak": a modified App Store app (tweak version, base app, App Store info).
    # "own": an app you made; its releases are compared with the app's own version.
    "kind": "tweak",
    "github_repo": "",
    "appstore_bundle_id": "",
    "base_app_name": "",
    # fork_repo and ref left blank use the default build repo and branch (build_defaults)
    "build": {"fork_repo": "", "workflow": "", "ref": "", "inputs": {}},
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
                app["bundleIdentifier"] = bundle_id
                app["name"] = app["name"] or ipa_name or bundle_id
                app["appstore_bundle_id"] = app["appstore_bundle_id"] or bundle_id
                app["order"] = len(apps)
                apps[bundle_id] = app
                self.save()
            return apps[bundle_id]

    def migrate(self):
        """Bring data saved by older versions up to date. Runs at startup."""
        self.data.setdefault("news", [])
        changed = self._adopt_build_defaults()
        # Older builds copied the App Store description into the app. Clear any
        # that are still verbatim copies so the tweak description is used.
        for app in self.data["apps"].values():
            if app.pop("tweak_version_regex", None) is not None:   # replaced by the standard file name
                changed = True
            if "kind" not in app:           # every app before this was a tweak
                app["kind"] = "tweak"
                changed = True
            if "featured" not in app:       # before the switch existed, every app was featured
                app["featured"] = True
                changed = True
            store_desc = (app.get("appstore") or {}).get("description", "")
            if store_desc and app.get("localizedDescription", "").strip() == store_desc.strip():
                app["localizedDescription"] = ""
                changed = True
        if changed:
            self.save()

    def _adopt_build_defaults(self) -> bool:
        """Before there was a default build repo, each app named its own. If two or
        more apps share a repo and branch, make that the default and clear their copies."""
        if "build_defaults" in self.data:
            return False
        self.data["build_defaults"] = {"repo": "", "ref": "main"}
        pairs = {}
        for app in self.data["apps"].values():
            cfg = app.get("build") or {}
            if cfg.get("fork_repo"):
                pairs.setdefault((cfg["fork_repo"], cfg.get("ref") or "main"), []).append(cfg)
        if pairs:
            (repo, ref), users = max(pairs.items(), key=lambda kv: len(kv[1]))
            if len(users) >= 2:
                self.data["build_defaults"] = {"repo": repo, "ref": ref}
                for cfg in users:
                    cfg["fork_repo"], cfg["ref"] = "", ""
        return True

    def build_target(self, app: dict) -> tuple[str, str]:
        """(repo, branch) an app builds from: its own if set, else the defaults."""
        cfg = app.get("build") or {}
        d = self.data.get("build_defaults") or {}
        return (cfg.get("fork_repo") or d.get("repo") or ""), (cfg.get("ref") or d.get("ref") or "main")

    def versions_for(self, bundle_id: str):
        return [v for v in self.data["versions"] if v["bundleIdentifier"] == bundle_id]

    def find_version(self, vid: str):
        return next((v for v in self.data["versions"] if v["id"] == vid), None)

    def find_build(self, bid: str):
        return next((b for b in self.data["builds"] if b["id"] == bid), None)


store = Store()
