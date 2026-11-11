import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.background import BackgroundTask

from . import builds, news, tools, upstream, uploads, workflow
from .enrich import compose_notes
from .ingest import enrich_version, ingest, refresh_appstore, slug
from .ipa import IPAError, inspect_ipa
from .source import CATEGORIES, app_category, build_source, category_id
from .store import DATA_DIR, ICON_DIR, IPA_DIR, TMP_DIR, new_id, now_iso, store

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
    tasks = [asyncio.create_task(upstream.loop()), asyncio.create_task(uploads.prune_loop())]
    yield
    for t in tasks:
        t.cancel()


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


# Signing in sets a cookie holding an expiry time and a signature over it. The
# signing key mixes a random secret kept in /data with the dashboard password,
# so changing ADMIN_PASSWORD (or deleting session.key) signs every device out.
SESSION_COOKIE = "sidestash_session"
SESSION_DAYS = 30
_KEY_FILE = DATA_DIR / "session.key"


def _session_key() -> bytes:
    if not _KEY_FILE.exists():
        _KEY_FILE.write_text(secrets.token_hex(32))
        _KEY_FILE.chmod(0o600)
    secret = _KEY_FILE.read_text().strip()
    return hashlib.sha256(f"{secret}\0{ADMIN_USER}\0{ADMIN_PASSWORD}".encode()).digest()


def _new_session() -> str:
    msg = f"v1.{int(time.time()) + SESSION_DAYS * 86400}"
    return f"{msg}.{hmac.new(_session_key(), msg.encode(), 'sha256').hexdigest()}"


def _signed_in(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        version, expires, sig = token.split(".")
        good = hmac.new(_session_key(), f"{version}.{expires}".encode(), "sha256").hexdigest()
        return bool(ADMIN_PASSWORD) and version == "v1" and int(expires) > time.time() and _same(sig, good)
    except ValueError:
        return False


def require_admin(request: Request):
    auth = request.headers.get("authorization", "")
    # The upload token is never sent automatically by a browser, so scripts can skip the CSRF check.
    if auth.startswith("Bearer ") and ADMIN_TOKEN and _same(auth[7:], ADMIN_TOKEN):
        return
    if _signed_in(request):
        if _from_other_site(request):
            raise HTTPException(403, "Changes are only accepted from the Sidestash dashboard.")
        return
    ip = _client(request)
    if _locked_out(ip):
        raise HTTPException(429, "Too many wrong passwords. Try again in 15 minutes.")
    # Username and password in the request still work for tools (curl -u), but
    # the browser is never asked for them: it uses the sign-in page.
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
    raise HTTPException(401, "Sign in required")


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

# IPAs and images are stored under names that are unique to their content
# (a hash or a random id), so a name never points at different bytes. Caches,
# including Cloudflare's, may keep them for good.
FOREVER = {"Cache-Control": "public, max-age=31536000, immutable"}


def source_response(request: Request, extra: dict | None = None) -> Response:
    """The source JSON with an ETag, so store apps refreshing an unchanged source
    get a tiny 304 instead of the whole file."""
    body = json.dumps(build_source(store.snapshot(), base_url(request)), separators=(",", ":")).encode()
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*", "ETag": etag, **(extra or {})}
    if etag in [t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@app.get("/source.json")
@app.get("/apps.json")
def source(request: Request):
    return source_response(request)


@app.get("/")
def landing(request: Request):
    """The site root works as the source address too.

    Browsers ask for HTML (their Accept header lists text/html) and get the landing
    page. Store apps such as AltStore and FlareStore don't (AltStore sends */*), so
    they get the source JSON, and https://your-host/ can be added directly.
    """
    vary = {"Vary": "Accept", "Cache-Control": "no-cache"}   # one URL, two answers: tell caches
    if "text/html" in request.headers.get("accept", ""):
        return FileResponse(STATIC / "index.html", headers=vary)
    return source_response(request, vary)


@app.get("/ipa/{bundle}/{filename}")
def download_ipa(bundle: str, filename: str):
    path = (IPA_DIR / bundle / filename).resolve()
    if not path.is_relative_to(IPA_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream", filename=filename, headers=FOREVER)


@app.get("/icons/{filename}")
def icon(filename: str):
    path = (ICON_DIR / filename).resolve()
    if not path.is_relative_to(ICON_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers=FOREVER)


FAVICONS = {  # path -> (file, type); built by scripts/build-icon.py
    "favicon.ico": ("favicon.ico", "image/x-icon"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
    "apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),  # older iOS asks for this
}


def _favicon_route(file: str, media: str):
    def serve():
        return FileResponse(STATIC / "favicon" / file, media_type=media, headers={"Cache-Control": "public, max-age=86400"})
    return serve


# Favicons live at the site root, where browsers and iOS look for them. Each path is
# registered on its own (not as /{name}), so they can't shadow /admin or other routes.
for _path, (_file, _media) in FAVICONS.items():
    app.add_api_route(f"/{_path}", _favicon_route(_file, _media), methods=["GET"], include_in_schema=False)


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
    # Never cached: a cached copy would outlive the link's download limit and expiry.
    return FileResponse(path, media_type="application/octet-stream", filename=filename,
                        headers={"Cache-Control": "private, no-store"})


# ---------------- dashboard ----------------

@app.get("/admin")
def admin_page(request: Request):
    if not _signed_in(request):
        return RedirectResponse("/login", 303)
    return FileResponse(STATIC / "admin.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/")
def admin_slash():
    return RedirectResponse("/admin")


@app.get("/login")
def login_page(request: Request):
    if _signed_in(request):
        return RedirectResponse("/admin", 303)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    """A plain form post, so password managers offer to fill and save it."""
    # Only this site's own sign-in page may sign you in (stops another site signing you in).
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        raise HTTPException(403)
    ip = _client(request)
    if _locked_out(ip):
        return RedirectResponse("/login?error=locked", 303)
    if not (ADMIN_PASSWORD and _same(username, ADMIN_USER) and _same(password, ADMIN_PASSWORD)):
        _failures.setdefault(ip, []).append(time.monotonic())
        logging.warning("Wrong dashboard password from %s", ip)
        return RedirectResponse("/login?error=wrong", 303)
    _failures.pop(ip, None)
    r = RedirectResponse("/admin", 303)
    r.set_cookie(SESSION_COOKIE, _new_session(), max_age=SESSION_DAYS * 86400, path="/",
                 httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return r


@app.post("/logout")
def logout(request: Request):
    if _from_other_site(request):
        raise HTTPException(403)
    r = Response(status_code=204)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r


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
        rel = (a.get("upstream") or {}).get("release")
        if rel:
            upstream.mark_newer(rel, a["bundleIdentifier"])
        if a.get("upstream"):
            a["upstream"]["fork"] = upstream.current_fork(a)
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
              "iconURL", "screenshots", "use_appstore_screenshots", "featured", "github_repo",
              "appstore_bundle_id", "base_app_name", "build", "order", "kind"}


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
    if body.get("kind") in ("tweak", "own"):
        app_["kind"] = body["kind"]
    store.save()
    return app_


@app.put("/api/apps/{bundle_id}", dependencies=[admin])
async def update_app(bundle_id: str, request: Request):
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    body = await request.json()
    if "kind" in body and body["kind"] not in ("tweak", "own"):
        raise HTTPException(400, "Type must be a tweaked app or your own app.")
    if body.get("kind") == "own":
        app_["appstore"] = {}   # no App Store listing: no icon, screenshots or genre from it
    if "category" in body:
        # store the id AltStore understands; "" means use the App Store's
        body["category"] = category_id(body["category"])
    before, kind_before = store.build_target(app_), app_.get("kind", "tweak")
    app_.update({k: v for k, v in body.items() if k in APP_FIELDS})
    store.save()
    if app_.get("kind", "tweak") != kind_before:
        # Same versions, new framing: rewrite each version's notes (release matched
        # by the app's version or the tweak's, App Store notes only for tweaks).
        await asyncio.gather(*(enrich_version(app_, v) for v in store.versions_for(bundle_id)),
                             return_exceptions=True)
        store.save()
    if store.build_target(app_) != before and builds.GITHUB_TOKEN:
        upstream.refresh_soon()   # the saved fork status was about the old repo
    return app_


@app.delete("/api/apps/{bundle_id}", dependencies=[admin])
def delete_app(bundle_id: str, versions: bool = False):
    """Remove an app. With ?versions=true its versions and their IPAs go too;
    otherwise an app that still has versions is refused."""
    if bundle_id not in store.data["apps"]:
        raise HTTPException(404)
    if any(b["bundleIdentifier"] == bundle_id and b["status"] not in ("done", "failed") for b in store.data["builds"]):
        raise HTTPException(400, "A build of this app is still running. Wait for it, or cancel it on GitHub.")
    mine = store.versions_for(bundle_id)
    if mine and not versions:
        raise HTTPException(400, "Delete this app's versions first.")
    for v in mine:
        (IPA_DIR / v["file"]).unlink(missing_ok=True)
        store.data["versions"].remove(v)
    folder = (IPA_DIR / slug(bundle_id)).resolve()
    if folder.is_relative_to(IPA_DIR.resolve()) and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
    store.data["builds"] = [b for b in store.data["builds"] if b["bundleIdentifier"] != bundle_id]
    store.data["apps"].pop(bundle_id, None)
    store.save()
    return {"ok": True, "versions_deleted": len(mine)}


@app.post("/api/apps/{bundle_id}/appstore", dependencies=[admin])
async def refresh_store(bundle_id: str):
    app_ = store.data["apps"].get(bundle_id)
    if not app_:
        raise HTTPException(404)
    info = await refresh_appstore(app_)
    if not info:
        raise HTTPException(404, "The App Store has no listing for that bundle ID in the US store.")
    return info


# ---- news ----

@app.post("/api/news", dependencies=[admin])
async def create_news(request: Request):
    try:
        return news.create(await request.json())
    except news.NewsError as e:
        raise HTTPException(400, str(e))


@app.put("/api/news/{nid}", dependencies=[admin])
async def update_news(nid: str, request: Request):
    try:
        return news.update(nid, await request.json())
    except KeyError:
        raise HTTPException(404)
    except news.NewsError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/news/{nid}", dependencies=[admin])
def delete_news(nid: str):
    if not news.delete(nid):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/news/image", dependencies=[admin])
async def upload_news_image(file: UploadFile = File(...)):
    """Images for news items live with the app icons, so /icons/ serves them publicly."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "Images must be PNG, JPEG or WebP.")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Keep images under 8 MB; store apps show them at phone size.")
    name = f"news-{new_id()}{ext}"
    (ICON_DIR / name).write_bytes(data)
    return {"url": f"/icons/{name}"}


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


@app.get("/api/backup", dependencies=[admin])
def backup():
    """Settings, versions, news and images in one zip. Not the IPAs: they're large,
    and GitHub can build them again. Back up /data as a whole for those."""
    stamp = time.strftime("%Y-%m-%d")
    out = TMP_DIR / f"backup-{new_id()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("db.json", json.dumps(store.snapshot(), indent=2))
        for f in sorted(ICON_DIR.iterdir()):
            if f.is_file():
                z.write(f, f"icons/{f.name}")
    return FileResponse(out, media_type="application/zip", filename=f"sidestash-backup-{stamp}.zip",
                        headers={"Cache-Control": "no-store"}, background=BackgroundTask(out.unlink, missing_ok=True))


@app.put("/api/build-defaults", dependencies=[admin])
async def set_build_defaults(request: Request):
    """The build repo and branch apps use unless they name their own.
    "apply_all": true also clears every app's own repo and branch."""
    body = await request.json()
    repo = str(body.get("repo", "")).strip().removeprefix("https://github.com/").strip("/")
    ref = str(body.get("ref", "")).strip() or "main"
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise HTTPException(400, "The build repo should look like owner/name.")
    store.data["build_defaults"] = {"repo": repo, "ref": ref}
    cleared = 0
    if body.get("apply_all"):
        for a in store.data["apps"].values():
            cfg = a.setdefault("build", {})
            if cfg.get("fork_repo") or cfg.get("ref"):
                cfg["fork_repo"], cfg["ref"] = "", ""
                cleared += 1
    store.save()
    if builds.GITHUB_TOKEN:
        upstream.refresh_soon()   # fork status and build tools for the new repo
    return {**store.data["build_defaults"], "cleared": cleared}


@app.post("/api/tools/update", dependencies=[admin])
async def update_tools(request: Request):
    """Move pinned build tools to their latest version (one commit to pins.json).
    Body: {"repo": "you/builds", "keys": ["cyan"]}; leave out keys for every pin not on hold."""
    body = await request.json()
    try:
        return await tools.update(str(body.get("repo", "")), body.get("keys"))
    except tools.ToolsError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach GitHub.")


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
    (fork, ref), wf = store.build_target(app_), cfg.get("workflow", "")
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


@app.post("/api/builds/{build_id}/rerun", dependencies=[admin])
async def rerun_build(build_id: str):
    try:
        return await builds.retry_build(build_id)
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
