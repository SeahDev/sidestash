# Sidestash

A self-hosted AltStore source plus an admin dashboard, for tweaked apps you
build yourself or in your fork of a tweak's repo. Works with any .ipa.

- **Public:** `/source.json` (AltStore v2 format, with the flat legacy fields
  most signer apps also read), `/ipa/...` downloads, and a small landing page at `/`.
- **Private:** `/admin` dashboard. Drop an .ipa and it reads the version, build,
  minimum iOS, entitlements and privacy strings out of the file, pulls the
  matching GitHub release notes, and adds the App Store "What's New" text when
  the IPA is built on the current App Store version.
- **Builds:** run an app's GitHub Actions workflow in your fork, with its
  inputs read from the workflow file, and watch it go from started to published.
  The server downloads the artifact and adds it to the source. Each app also has
  a listing editor with a live preview and its version history.

## Why builds run on GitHub, not on Unraid

Packaging an iOS tweak usually needs macOS (`xcrun`, codesigning, or a job on
`macos-latest`). A Linux container can't do that, so the server orchestrates and
GitHub builds. Forks of public repos get free Actions minutes, macOS included.
Your Mac stays the fallback (see below).

## 1. Try it on your Mac

1. `brew install uv` (runs the app with the right Python version; no Docker needed).
2. In this folder, run `./run-local.sh`. The first time, it creates `.env` and stops.
3. Edit `.env`: set `ADMIN_PASSWORD`, and set `PUBLIC_URL` to your Mac's LAN
   address (e.g. `http://192.168.1.50:8686`) so your iPhone can reach it.
4. Run `./run-local.sh` again and open http://localhost:8686/admin.
   If macOS asks whether Python may accept incoming connections, allow it.

Stop it with Ctrl-C. Everything it stores goes in `./data`.

Builds work from the Mac too: the server only makes outgoing requests to
GitHub, so nothing needs to be exposed. To test the source on your iPhone, add
`http://<mac-ip>:8686/source.json` while on the same Wi-Fi. Some signer apps
insist on HTTPS; if FlareStore refuses the plain-http URL, that part waits for
the Unraid setup.

Prefer Docker? With Docker Desktop or OrbStack running, `docker compose up --build`
uses the same `.env` and stores data in `./data`.

## 2. Move it to Unraid

**Compose Manager plugin (simplest):** copy this folder to
`/mnt/user/appdata/sidestash-src`, then in `.env` set
`DATA_PATH=/mnt/user/appdata/sidestash` and `PUBLIC_URL` to the public address.
Compose Up builds the image. To bring your test data along, copy the Mac's
`data` folder to `/mnt/user/appdata/sidestash`, then update `PUBLIC_URL`.

**Or the Unraid template:** the included workflow publishes
`ghcr.io/<you>/sidestash:latest` on every push to `main` (make the package
public, or log Unraid into GHCR). Copy [`unraid/sidestash.xml`](unraid/sidestash.xml)
to `/boot/config/plugins/dockerMan/templates-user/my-sidestash.xml`, then
Docker › Add Container › Template: sidestash. It maps port 8686, `/data` to
`/mnt/user/appdata/sidestash`, and has a field for each variable below. In a fork,
change the image name and the URLs in it to your own.

| Variable | What it's for |
|---|---|
| `PUBLIC_URL` | The address of the source as your phone sees it. Download links are built from it, so change it when you move servers. |
| `ADMIN_USER` / `ADMIN_PASSWORD` | Dashboard sign-in. |
| `ADMIN_TOKEN` | Optional. Long random string for `scripts/upload.sh`. |
| `PORT` | Port the server listens on inside the container (default `8686`). Only matters with host or custom networking. |
| `GITHUB_TOKEN` | Fine-grained token limited to **Only select repositories** (your forks): **Actions: read and write** and **Contents: read**. Only if you use Sync fork, raise **Contents** to read and write and add **Workflows: read and write**. Give it nothing else. Also lifts the API rate limit for changelogs. |
| `UPLOAD_LINK_HOURS` / `UPLOAD_LINK_DOWNLOADS` | Lifetime of the private links for decrypted IPAs you upload for builds (default 6 hours, 5 downloads). |
| `PUID` / `PGID` | Docker only: the user the server runs as (default `99`/`100`, Unraid's nobody:users). It never runs as root. |
| `UPDATE_CHECK_HOURS` | How often to check the original repos for releases and your forks for new commits (default `6`; `0` turns it off). |

## 3. Expose only the public part

The dashboard has a password, but there's no reason to put it on the internet.
Forward only these paths publicly: `/`, `/source.json`, `/apps.json`, `/ipa/`,
`/icons/`, `/fonts/`, the favicons (`/favicon.ico`, `/favicon.svg`,
`/apple-touch-icon.png`), and `/base/` if you upload decrypted IPAs for builds (GitHub
downloads them from there). Use the LAN address (`http://<unraid-ip>:8686/admin`) or
Tailscale for the dashboard.

Cloudflare Tunnel example (`config.yml`):

```yaml
ingress:
  - hostname: apps.example.com
    path: ^/(source\.json|apps\.json|favicon\.(ico|svg)|apple-touch-icon(-precomposed)?\.png|ipa/.*|icons/.*|fonts/.*|base/.*)?$
    service: http://192.168.1.10:8686
  - service: http_status:404
```

Upload IPAs over the LAN: Cloudflare's free plan rejects request bodies over
100 MB, and many app IPAs are bigger than that. Downloads aren't affected.

## 4. Add an app and its build

1. Fork the tweak's repo on GitHub and enable Actions in your fork (GitHub turns
   them off in new forks).
2. In the dashboard, open **Add app**. Either drop an .ipa you already have, or
   enter the base app's bundle ID, a name and the original repo.
3. On the app's page, under **Build**, fill in **Your fork**, the **Workflow file**
   (the one in `.github/workflows` that builds the IPA) and the **Branch**. Sidestash
   reads the workflow's inputs and shows them as fields.
4. Fill in the inputs. Many workflows want a link to a decrypted .ipa: press
   **Upload .ipa** next to that field and Sidestash fills in a private link to the
   file, or paste a direct link from elsewhere (a Dropbox link ending in `dl=1`
   works). GitHub downloads it, so the link must work from the internet: uploads
   need `PUBLIC_URL` set to your public address and `/base/` forwarded.
5. Make sure the workflow uploads the IPA as an artifact (`actions/upload-artifact`);
   that's where Sidestash picks it up. Artifacts with "deb" in their name are skipped.

Then press **Start build**. The run panel next to it shows each stage and links
to the log on GitHub. When it finishes, the new version appears under Versions.

If a run fails before it builds anything, check the runner images in the
workflow: a `runs-on` image that doesn't exist leaves the run queued, and
`ubuntu-latest` moving to a new Ubuntu has broken tweak toolchains before.
Pinning an image (e.g. `ubuntu-24.04`) fixes that.

**Keeping up with the original repos.** Every few hours Sidestash checks each
app's original repo for a new release and compares your fork's build branch with
it. The Overview's **Updates** list shows when a release is newer than the
version you've published, when the App Store has a newer base app, and when your
fork is behind, with a **Sync fork** button. Sync does what GitHub's Sync fork
button does; if your build branch only exists in your fork (say `sidestash`,
holding your own edits), it syncs the default branch and then merges that into
your build branch. A conflict is left for you to resolve on GitHub.
**Check for updates** on the Overview checks right away.

For routine updates you don't need the app page: every app on the Overview has
a **Build** button that opens the same inputs, Upload buttons included.

## 5. Building on your Mac instead

Build as the tweak's README describes, then either drag the .ipa onto the
dashboard or run:

```sh
SIDESTASH_URL=http://192.168.1.10:8686 SIDESTASH_TOKEN=<ADMIN_TOKEN> \
  ./scripts/upload.sh packages/MyTweak_v1.4.0.ipa "Built with FLEX"
```

Keep the file names the build produces: the tweak version (1.4.0 above) is read
from them with the app's **Tweak version pattern** to find the matching GitHub
release.

## Security

- **Keep the dashboard private.** Forward only the public paths listed above;
  reach `/admin` over your LAN or Tailscale. The password is the only lock on it.
- **Wrong passwords are throttled:** 10 in 15 minutes locks that address out
  for 15 minutes.
- **Cross-site requests are refused.** Your browser re-sends the dashboard
  password to every request for this address, even ones another site starts,
  so changes are only accepted with the `X-Sidestash` header the dashboard
  sends. `scripts/upload.sh` uses `ADMIN_TOKEN` instead, which browsers never
  send on their own.
- **Upload links are short-lived.** Public forks have public Actions logs, and
  workflows often print their inputs, so the private link to a decrypted IPA
  may be seen by others. Each one is deleted when the run that used it
  finishes, after `UPLOAD_LINK_DOWNLOADS` downloads, or after
  `UPLOAD_LINK_HOURS`, whichever is first.
- **The container doesn't run as root.** It fixes ownership of `/data`, then
  drops to `PUID`:`PGID`.

## Things to know

- **Version numbers come from the IPA**, which means the *base app* version
  (say Instagram 454.1), because AltStore checks the downloaded app against the
  source. A new tweak release on the same base version therefore looks like
  the same version to update checkers. Install it from the source manually,
  or bump to a newer base.
- **Latest = highest version number.** When a tweak ships separate builds for
  older iOS versions, the older-iOS build never shows as an "update" over the
  newer one; AltStore picks the newest build your iOS supports using `minOSVersion`.
- **App Store notes** only attach when the IPA's version equals the version
  currently live in the App Store. "Re-pull changelogs" on a version refetches both.
- **Everything in the source is public** to anyone with the URL, including the
  modified IPAs. Decrypted IPAs you upload for builds are never listed: each
  has a long random link and is deleted when the run that used it finishes, or
  after `UPLOAD_LINK_HOURS`.
- **Not affiliated** with Apple, AltStore, or the authors of any tweak or app
  you use it with.
- **No warranty.** This is a personal project; it comes as is.
- **The Docker image is published** to GHCR by `.github/workflows/docker.yml`
  on every push to `main`. It contains only the code, never `.env` or `/data`.
- **Fonts:** SF Pro and SF Mono, bundled as web fonts in `app/static/fonts` so every
  device gets them. `scripts/build-fonts.py` rebuilds them from the fonts installed on a Mac.
- Data lives in `/data`: `db.json`, `ipas/`, `icons/`, `uploads/`. Back up that folder.
