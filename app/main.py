import asyncio
import base64
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import builds, upstream, uploads, workflow
from .enrich import compose_notes
from .ingest import enrich_version, ingest, refresh_appstore, slug
from .ipa import IPAError, inspect_ipa
from .source import CATEGORIES, app_category, build_source, category_id
from .store import ICON_DIR, IPA_DIR, TMP_DIR, new_id, now_iso, store

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).parent / "static"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


@asynccontextmanager
async def lifespan(_app):
    if not ADMIN_PASSWORD:
        logging.warning("ADMIN_PASSWORD is not set: the dashboard will refuse every login.")
    store.migrate()
    uploads.prune()
    builds.resume_unfinished()
    checker = asyncio.create_task(upstream.loop())
    yield
    checker.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- auth ----------------

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
FAIL_LIMIT, FAIL_WINDOW = 10, 15 * 60          # 10 wrong passwords in 15 minutes locks that address out
_failures: dict[str, list[float]] = {}


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _locked_out(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW]
    _failures[ip] = recent
    return len(recent) >= FAIL_LIMIT


def _from_other_site(request: Request) -> bool:
    """True for requests another web page made your browser send (cross-site request forgery).

    The browser re-sends the dashboard password on every request to this address,
    whichever page started it, so changes are only accepted from the dashboard itself:
    it sends an X-Sidestash header, which a page on another site can't add without
    a CORS preflight this server never approves.
    """
    if request.method in SAFE_METHODS:
        return False
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return True
    return request.headers.get("x-sidestash") != "1"


def require_admin(request: Request):
    auth = request.headers.get("authorization", "")
    # The upload token is never sent automatically by a browser, so scripts can skip the CSRF check.
    if auth.startswith("Bearer ") and ADMIN_TOKEN and _same(auth[7:], ADMIN_TOKEN):
        return
    ip = _client(request)
    if _locked_out(ip):
        raise HTTPException(429, "Too many wrong passwords. Try again in 15 minutes.")
    if auth.startswith("Basic ") and ADMIN_PASSWORD:
        try:
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            user = pw = ""
        if _same(user, ADMIN_USER) and _same(pw, ADMIN_PASSWORD):
            _failures.pop(ip, None)
            if _from_other_site(request):
                raise HTTPException(403, "Changes are only accepted from the Sidestash dashboard.")
            return
        _failures.setdefault(ip, []).append(time.monotonic())
        logging.warning("Wrong dashboard password from %s", ip)
    raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": 'Basic realm="sidestash"'})


admin = Depends(require_admin)


def base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


async def save_upload(upload: UploadFile) -> Path:
    tmp = TMP_DIR / f"{new_id()}.part"
    with open(tmp, "wb") as f:
        while chunk := await upload.read(1 << 20):
            f.write(chunk)
    return tmp


# ---------------- public ----------------

@app.get("/source.json")
@app.get("/apps.json")
def source(request: Request):
    return JSONResponse(build_source(store.snapshot(), base_url(request)),
                        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})


@app.get("/")
def landing():
    return FileResponse(STATIC / "index.html")


@app.get("/ipa/{bundle}/{filename}")
def download_ipa(bundle: str, filename: str):
    path = (IPA_DIR / bundle / filename).resolve()
    if not path.is_relative_to(IPA_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


@app.get("/icons/{filename}")
def icon(filename: str):
    path = (ICON_DIR / filename).resolve()
    if not path.is_relative_to(ICON_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


FONT_DIR = STATIC / "fonts"


@app.get("/fonts/{filename}")
def font(filename: str):
    """The page fonts. They never change under a name, so browsers may keep them a year."""
    path = (FONT_DIR / filename).resolve()
    if not path.is_relative_to(FONT_DIR.resolve()) or not path.is_file() or path.suffix != ".woff2":
        raise HTTPException(404)
    return FileResponse(path, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.api_route("/base/{token}/{filename}", methods=["GET", "HEAD"])
def uploaded_ipa(token: str, filename: str, request: Request):
    """A decrypted IPA you uploaded for a build. Only reachable with its secret link."""
    rng = request.headers.get("range", "")
    new_download = request.method == "GET" and (not rng or rng.replace(" ", "").startswith("bytes=0-"))
    path = uploads.find(token, filename, count=new_download)
    if not path or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


# ---------------- dashboard ----------------

@app.get("/admin", dependencies=[admin])
def admin_page():
    return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/", dependencies=[admin])
def admin_slash():
    return RedirectResponse("/admin")


@app.get("/api/state", dependencies=[admin])
def state(request: Request):
    data = store.snapshot()
    base = base_url(request)
    data["public_url"] = base
    data["github_ready"] = bool(builds.GITHUB_TOKEN)
    data["public_reachable"] = uploads.reachable_from_internet(base)
    data["categories"] = CATEGORIES
    for a in data["apps"].values():
        # what "Automatic" resolves to: the App Store's category, mapped
        a["category_auto"] = app_category({**a, "category": ""})
    for u in data.get("uploads", []):
        u["url"] = uploads.link(base, u)
    return data


@app.post("/api/uploads", dependencies=[admin])
async def upload_for_build(request: Request, file: UploadFile = File(...)):
    """Store a decrypted IPA and return a secret link to paste into a build input."""
    if not (file.filename or "").lower().endswith(".ipa"):
        raise HTTPException(400, "Choose an .ipa file.")
    tmp = await save_upload(file)
    try:
        meta = await asyncio.to_thread(inspect_ipa, tmp)
    except IPAError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, str(e))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    uploads.prune()
    u = uploads.add(tmp, file.filename, meta)
    return {**u, "url": uploads.link(base_url(request), u)}


@app.delete("/api/uploads/{uid}", dependencies=[admin])
def delete_upload(uid: str):
    if not uploads.remove(uid):
        raise HTTPException(404)
    return {"ok": True}


@app.put("/api/source", dependencies=[admin])
async def update_source(request: Request):
    body = await request.json()
    allowed = {"name", "identifier", "subtitle", "description", "iconURL", "website", "tintColor"}
    store.data["source"].update({k: v for k, v in body.items() if k in allowed})
    store.save()
    return store.data["source"]


APP_FIELDS = {"name", "subtitle", "developerName", "localizedDescription", "tintColor", "category",
              "iconURL", "screenshots", "use_appstore_screenshots", "github_repo",
              "appstore_bundle_id", "base_app_name", "tweak_version_regex", "build", "order"}


@app.post("/api/apps", dependencies=[admin])
async def create_app(request: Request):
    """Add an app before it has any versions, e.g. to set up a GitHub build first."""
    body = await request.json()
    bundle_id = str(body.get("bundleIdentifier", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", bundle_id):
        raise HTTPException(400, "Enter the base app's bundle ID, like com.example.app.")
    if bundle_id in store.data["apps"]:
        raise HTTPException(409, "That bundle ID is already in the dashboard.")
    repo = str(body.get("github_repo", "")).strip().removeprefix("https://github.com/").strip("/")
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise HTTPException(400, "GitHub repo should look like owner/name.")
    app_ = store.ensure_app(bundle_id, str(body.get("name", "")).strip())
    app_["github_repo"] = repo or app_.get("github_repo", "")
    if repo and not app_.get("developerName"):
        app_["developerName"] = repo.split("/")[0]
    if body.get("base_app_name"):
        app_["base_app_name"] = str(body["base_app_name"]).strip()
    store.save()
    return app_


@app.put("/api/apps/{bundle_id}", dependencies=[admin])
async def update_app(bundle_id: str, request: Request):
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    body = await request.json()
    if "tweak_version_regex" in body:
        try:
            re.compile(body["tweak_version_regex"])
        except re.error as e:
            raise HTTPException(400, f"Tweak version pattern is not a valid regex: {e}")
    if "category" in body:
        # store the id AltStore understands; "" means use the App Store's
        body["category"] = category_id(body["category"])
    app_.update({k: v for k, v in body.items() if k in APP_FIELDS})
    store.save()
    return app_


@app.delete("/api/apps/{bundle_id}", dependencies=[admin])
def delete_app(bundle_id: str):
    if store.versions_for(bundle_id):
        raise HTTPException(400, "Delete this app's versions first.")
    store.data["apps"].pop(bundle_id, None)
    store.save()
    return {"ok": True}


@app.post("/api/apps/{bundle_id}/appstore", dependencies=[admin])
async def refresh_store(bundle_id: str):
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    info = await refresh_appstore(app_)
    if not info:
        raise HTTPException(404, "The App Store has no listing for that bundle ID in the US store.")
    return info


@app.post("/api/apps/{bundle_id}/icon", dependencies=[admin])
async def upload_icon(bundle_id: str, file: UploadFile = File(...)):
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "Icons must be PNG, JPEG or WebP.")
    name = f"{slug(bundle_id)}-{new_id()}{ext}"
    (ICON_DIR / name).write_bytes(await file.read())
    app_["iconURL"] = f"/icons/{name}"
    store.save()
    return app_


@app.post("/api/upload", dependencies=[admin])
async def upload(file: UploadFile = File(...), notes: str = Form("")):
    if not (file.filename or "").lower().endswith(".ipa"):
        raise HTTPException(400, "Choose an .ipa file.")
    tmp = await save_upload(file)
    try:
        return await ingest(tmp, file.filename, custom_notes=notes)
    except (IPAError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(400, str(e))


@app.patch("/api/versions/{vid}", dependencies=[admin])
async def patch_version(vid: str, request: Request):
    v = store.find_version(vid)
    if not v:
        raise HTTPException(404)
    body = await request.json()
    for k in ("published", "localizedDescription", "custom_notes", "tweakVersion"):
        if k in body:
            v[k] = body[k]
    if "custom_notes" in body and "localizedDescription" not in body:
        v["localizedDescription"] = compose_notes(store.data["apps"][v["bundleIdentifier"]], v)
    store.save()
    return v


@app.post("/api/versions/{vid}/refresh", dependencies=[admin])
async def refresh_version(vid: str):
    v = store.find_version(vid)
    if not v:
        raise HTTPException(404)
    await enrich_version(store.data["apps"][v["bundleIdentifier"]], v)
    store.save()
    return v


@app.delete("/api/versions/{vid}", dependencies=[admin])
def delete_version(vid: str):
    v = store.find_version(vid)
    if not v:
        raise HTTPException(404)
    (IPA_DIR / v["file"]).unlink(missing_ok=True)
    store.data["versions"].remove(v)
    store.save()
    return {"ok": True}



# ---- builds ----

@app.post("/api/updates/check", dependencies=[admin])
async def check_updates():
    """Check every app's original repo for releases and how far behind each fork is."""
    return await upstream.check_all()


@app.post("/api/apps/{bundle_id}/sync", dependencies=[admin])
async def sync_fork(bundle_id: str):
    """Pull the original repo's changes into your fork's build branch."""
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    try:
        return await upstream.sync_fork(app_)
    except upstream.SyncError as e:
        raise HTTPException(400, str(e))


@app.post("/api/apps/{bundle_id}/workflow", dependencies=[admin])
async def load_workflow(bundle_id: str):
    """Read the inputs the fork's workflow accepts, so the dashboard can show them."""
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    cfg = app_.setdefault("build", {})
    fork, wf, ref = cfg.get("fork_repo", ""), cfg.get("workflow", ""), cfg.get("ref") or "main"
    try:
        inputs = await workflow.fetch_inputs(fork, wf, ref)
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e))
    cfg["schema"] = {"fork_repo": fork, "workflow": wf, "ref": ref, "inputs": inputs, "fetched": now_iso()}
    store.save()
    return cfg["schema"]


@app.post("/api/apps/{bundle_id}/build", dependencies=[admin])
async def start_build(bundle_id: str, request: Request):
    body = await request.json()
    try:
        return await builds.start_build(bundle_id, body.get("inputs", {}))
    except builds.BuildError as e:
        raise HTTPException(400, str(e))


@app.post("/api/builds/{build_id}/retry", dependencies=[admin])
def retry_watch(build_id: str):
    b = store.find_build(build_id)
    if not b:
        raise HTTPException(404)
    b.update(status="queued" if not b["run_id"] else "in_progress", error=None)
    if b["run_id"]:
        b["started"] = now_iso()  # restart the 3-hour watch window
    store.save()
    builds.watch(b)
    return b
