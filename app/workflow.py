"""Read a workflow file from your fork and list its workflow_dispatch inputs."""
import httpx
import yaml

from .enrich import gh_headers

API = "https://api.github.com"


class WorkflowError(Exception):
    pass


def parse_inputs(text: str) -> list[dict]:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise WorkflowError(f"The workflow file isn't valid YAML: {e}")
    if not isinstance(doc, dict):
        raise WorkflowError("The workflow file isn't a GitHub Actions workflow.")
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {k: None for k in on}
    if not isinstance(on, dict) or "workflow_dispatch" not in on:
        raise WorkflowError("This workflow can't be started from outside GitHub: it has no workflow_dispatch trigger.")
    raw = (on.get("workflow_dispatch") or {}).get("inputs") or {}

    inputs = []
    for name, spec in raw.items():
        spec = spec or {}
        kind = str(spec.get("type") or "string")
        default = spec.get("default")
        if isinstance(default, bool):
            default = "true" if default else "false"
        elif default is None:
            default = "false" if kind == "boolean" else ""
        inputs.append({
            "name": str(name),
            "description": str(spec.get("description") or "").strip(),
            "required": bool(spec.get("required")),
            "type": kind,
            "default": str(default),
            "options": [str(o) for o in spec.get("options") or []],
        })
    return inputs


async def fetch_inputs(fork: str, workflow: str, ref: str) -> list[dict]:
    if not fork or not workflow:
        raise WorkflowError("Fill in your fork and the workflow file first.")
    url = f"{API}/repos/{fork}/contents/.github/workflows/{workflow}"
    headers = {**gh_headers(), "Accept": "application/vnd.github.raw+json"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as c:
        try:
            r = await c.get(url, params={"ref": ref or "main"})
        except httpx.TransportError:
            raise WorkflowError("Couldn't reach GitHub from the server.")
    if r.status_code == 404:
        raise WorkflowError(f"GitHub can't find .github/workflows/{workflow} on branch '{ref}' in {fork}. "
                            "Check the names, and that your token includes this fork.")
    if r.status_code in (401, 403):
        raise WorkflowError("The token can't read this fork. Add the fork to your token with Contents: read.")
    if r.status_code != 200:
        raise WorkflowError(f"GitHub returned an error ({r.status_code}) reading the workflow.")
    return parse_inputs(r.text)
