"""Run a fork's IPA workflow on GitHub Actions, wait for it, import the result."""
import asyncio
import datetime as dt
import logging
import shutil
import zipfile
from pathlib import Path

import httpx

from . import uploads
from .enrich import GITHUB_TOKEN, gh_headers
from .ingest import ingest
from .ipa import INFO_RE
from .store import TMP_DIR, new_id, now_iso, store

log = logging.getLogger("builds")
API = "https://api.github.com"
POLL_SECONDS = 30
TIMEOUT = dt.timedelta(hours=3)
_tasks: dict[str, asyncio.Task] = {}


class BuildError(Exception):
    pass


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def friendly(e: Exception) -> str:
    """Turn GitHub API failures into something you can act on."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "GitHub rejected the token. Check GITHUB_TOKEN on the container and restart it."
        if code == 403:
            return "The token can't do this. It needs Actions read and write access on your fork."
        if code == 404:
            return "GitHub can't find the fork, workflow file or run. Check the fork name and that the token covers that repo."
        return f"GitHub returned an error ({code})."
    if isinstance(e, httpx.TransportError):
        return "Couldn't reach GitHub from the server. It will need network access to api.github.com."
    return str(e)


def _set(build: dict, **kw):
    build.update(kw)
    build["updated"] = now_iso()
    store.save()


async def start_build(bundle_id: str, inputs: dict) -> dict:
    if not GITHUB_TOKEN:
        raise BuildError("Set GITHUB_TOKEN on the container to run builds on GitHub Actions.")
    app = store.data["apps"].get(bundle_id)
    if not app:
        raise BuildError("Unknown app.")
    cfg = app.get("build") or {}
    fork, wf, ref = cfg.get("fork_repo"), cfg.get("workflow"), cfg.get("ref") or "main"
    if not fork or not wf:
        raise BuildError("Set the fork repo and workflow file for this app first.")

    schema = cfg.get("schema") or {}
    if schema and (schema.get("fork_repo"), schema.get("workflow"), schema.get("ref")) == (fork, wf, ref):
        # Only send inputs the workflow declares: GitHub rejects the run otherwise.
        known = {i["name"]: i for i in schema["inputs"]}
        inputs = {k: v for k, v in inputs.items() if k in known}
        missing = [i.get("description") or i["name"] for i in known.values()
                   if i["required"] and not i["default"] and not str(inputs.get(i["name"], "")).strip()]
        if missing:
            raise BuildError("Fill in: " + ", ".join(missing) + ".")
    clean_inputs = {k: str(v) for k, v in inputs.items() if str(v).strip() != ""}
    started = dt.datetime.now(dt.timezone.utc)
    async with httpx.AsyncClient(timeout=30, headers=gh_headers()) as c:
        r = await c.post(f"{API}/repos/{fork}/actions/workflows/{wf}/dispatches",
                         json={"ref": ref, "inputs": clean_inputs})
    if r.status_code == 422:
        raise BuildError(f"GitHub refused the inputs or branch: {r.json().get('message', r.text[:200])}")
    if r.status_code not in (200, 204):
        raise BuildError(friendly(httpx.HTTPStatusError("", request=r.request, response=r)))

    cfg["inputs"] = {**cfg.get("inputs", {}), **inputs}  # remember what you last used
    build = {
        "id": new_id(), "bundleIdentifier": bundle_id, "fork": fork, "workflow": wf,
        "status": "queued", "conclusion": None, "run_id": None, "run_url": None,
        "started": started.strftime("%Y-%m-%dT%H:%M:%SZ"), "updated": now_iso(),
        "imported": [], "error": None, "inputs": clean_inputs,
    }
    store.data["builds"].insert(0, build)
    del store.data["builds"][30:]
    store.save()
    watch(build)
    return build


def watch(build: dict):
    if build["id"] not in _tasks or _tasks[build["id"]].done():
        _tasks[build["id"]] = asyncio.create_task(_watch(build))


def resume_unfinished():
    for b in store.data["builds"]:
        if b["status"] not in ("done", "failed"):
            watch(b)


async def _find_run(c: httpx.AsyncClient, build: dict) -> dict | None:
    claimed = {b["run_id"] for b in store.data["builds"] if b["run_id"]}
    r = await c.get(f"{API}/repos/{build['fork']}/actions/workflows/{build['workflow']}/runs",
                    params={"event": "workflow_dispatch", "per_page": 10})
    r.raise_for_status()
    since = _parse(build["started"]) - dt.timedelta(seconds=15)
    for run in sorted(r.json().get("workflow_runs", []), key=lambda x: x["created_at"]):
        if run["id"] not in claimed and _parse(run["created_at"]) >= since:
            return run
    return None


async def _watch(build: dict):
    try:
        async with httpx.AsyncClient(timeout=60, headers=gh_headers(), follow_redirects=True) as c:
            while _parse(build["started"]) + TIMEOUT > dt.datetime.now(dt.timezone.utc):
                if not build["run_id"]:
                    run = await _find_run(c, build)
                    if run:
                        _set(build, run_id=run["id"], run_url=run["html_url"], status=run["status"])
                else:
                    r = await c.get(f"{API}/repos/{build['fork']}/actions/runs/{build['run_id']}")
                    r.raise_for_status()
                    run = r.json()
                    if run["status"] != "completed":
                        if run["status"] != build["status"]:
                            _set(build, status=run["status"])
                    elif run["conclusion"] != "success":
                        _set(build, status="failed", conclusion=run["conclusion"],
                             error=f"Workflow finished with '{run['conclusion']}'. Open the run on GitHub for the log.")
                        # The run is over, and its log may show the upload link: drop the file.
                        uploads.remove_linked((build.get("inputs") or {}).values())
                        return
                    else:
                        _set(build, status="importing", conclusion="success")
                        await _import_artifacts(c, build)
                        return
                await asyncio.sleep(POLL_SECONDS)
            _set(build, status="failed", error="Gave up waiting after 3 hours.")
    except Exception as e:
        log.exception("build watcher failed")
        _set(build, status="failed", error=friendly(e))


async def _import_artifacts(c: httpx.AsyncClient, build: dict):
    r = await c.get(f"{API}/repos/{build['fork']}/actions/runs/{build['run_id']}/artifacts")
    r.raise_for_status()
    artifacts = [a for a in r.json().get("artifacts", [])
                 if not a.get("expired") and "deb" not in a["name"].lower()]
    if not artifacts:
        raise BuildError("The run succeeded but produced no IPA artifacts.")

    imported, seen = [], []
    for art in artifacts:
        zpath = TMP_DIR / f"artifact-{art['id']}.zip"
        async with c.stream("GET", art["archive_download_url"]) as resp:
            resp.raise_for_status()
            with open(zpath, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
        try:
            found, names = await asyncio.to_thread(_extract_ipas, zpath, art["name"])
        except zipfile.BadZipFile:
            found, names = [], [f"{art['name']} (not a zip file)"]
        finally:
            zpath.unlink(missing_ok=True)
        seen += names
        for ipa_path, name in found:
            v = await ingest(ipa_path, name, origin=f"actions:{build['run_id']}")
            imported.append({"version": v["version"], "file": name, "duplicate": v.get("duplicate", False)})

    if not imported:
        listing = ", ".join(seen[:6]) + (" …" if len(seen) > 6 else "")
        raise BuildError(f"Artifacts downloaded, but none contained an .ipa. They held: {listing or 'nothing'}.")
    _set(build, status="done", imported=imported)
    # The decrypted IPA it was built from isn't needed any more.
    uploads.remove_linked((build.get("inputs") or {}).values())


def _ipa_name(name: str) -> str:
    """Keep the build's file name (the tweak version is read from it), always ending in .ipa."""
    stem = Path(name).name
    for ext in (".ipa", ".tipa", ".zip"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
    return stem + ".ipa"


def _copy_member(z: zipfile.ZipFile, name: str) -> Path:
    target = TMP_DIR / f"{new_id()}.ipa"
    with z.open(name) as src, open(target, "wb") as dst:
        while chunk := src.read(1 << 20):
            dst.write(chunk)
    return target


def _extract_ipas(zpath: Path, artifact_name: str, depth: int = 0) -> tuple[list[tuple[Path, str]], list[str]]:
    """Find IPAs in a downloaded artifact. Returns (ipas, names of everything inside)."""
    out = []
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        # upload-artifact with `archive: false` serves the file itself, and an IPA is a zip.
        if any(INFO_RE.match(n) for n in names):
            target = TMP_DIR / f"{new_id()}.ipa"
            shutil.copyfile(zpath, target)
            return [(target, _ipa_name(artifact_name))], [artifact_name]
        for name in names:
            low = name.lower()
            if low.endswith((".ipa", ".tipa")):
                out.append((_copy_member(z, name), _ipa_name(name)))
            elif low.endswith(".zip") and depth == 0:
                inner = _copy_member(z, name)
                try:
                    found, _ = _extract_ipas(inner, name, depth + 1)
                    out += found
                except zipfile.BadZipFile:
                    pass
                finally:
                    inner.unlink(missing_ok=True)
    return out, [Path(n).name for n in names]
