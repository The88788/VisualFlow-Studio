<div align="center">

# VisualFlow Studio

**A self-hosted workspace for generating, refining, reviewing, and downloading AI images and videos through Google Flow.**

Version **1.1.2.34** · [Русская версия](README.ru.md) · [Deployment](DEPLOYMENT.md) · [Contributing](CONTRIBUTING.md)

</div>

> [!IMPORTANT]
> VisualFlow Studio is an unofficial community project. It is not affiliated
> with, endorsed by, or supported by Google. It drives the Google Flow web
> application with a real browser and may require maintenance whenever that UI
> changes. You are responsible for your account, credits, media, generated
> content, and compliance with applicable terms and laws.

## Why VisualFlow Studio?

Google Flow is designed for interactive browser use. VisualFlow Studio adds a
practical self-hosted layer for design teams: one web dashboard, persistent
tasks, local result storage, clear errors, and repeatable refinement workflows
without giving every user shell or remote-desktop access.

A designer can generate a product image, open the result, attach more
references, describe the next change, create a numbered revision, compare it
in the gallery, and download the original or a prepared 2K version.

## Project origin

VisualFlow Studio grew from a production prototype and twelve days of focused
work on real design-team requirements: removing RDP from daily use, adding a
usable dashboard, reference-driven generation, revision workflows, reliable
downloads, recovery, cleanup, and operational safeguards. It is published as
open source so that this work remains useful and can evolve through community
contributions rather than disappear inside a closed one-off deployment.

## Highlights

### Images

- text-to-image generation through Google Flow;
- image-to-image generation with up to 10 uploaded references;
- sequential refinement of an existing result into numbered revisions;
- model, aspect-ratio, and output-count controls;
- reference thumbnails, manual slider navigation, and enlarged preview;
- result lightbox with navigation, download, refinement, and deletion;
- original PNG download and Flow-powered 2K preparation;
- retry with the same prompt, settings, and retained reference files;
- pre-submit verification of the prompt, model, and all reference IDs in the
  actual Flow request;
- persistent submit status, human-readable errors, and copyable diagnostics;
- collapsible long prompts with a copy button at the top.

### Video

- text-to-video generation with supported Veo models and orientations;
- persistent task state and restart recovery;
- automatic browser-based download from the generated media page;
- 720p labelling for plans where higher resolutions are unavailable;
- recovery of generated-but-not-downloaded clips from the gflow catalog;
- local trim and merge operations through FFmpeg;
- synchronized cleanup of expired files and database records.

### Operations

- FastAPI backend and responsive dashboard;
- SQLite task history and local or S3-compatible storage;
- Docker image with Chrome, Xvfb, FFmpeg, and a non-root user;
- private persistent Chrome profiles mounted outside the image;
- serialized profile access to prevent Chrome profile corruption;
- health checks, structured logs, backups, Nginx, TLS, and Fail2ban examples;
- legacy multi-account/proxy code kept separate from the recommended provider.

## What this project is not

- It is not an official Google API or a way to bypass account limits.
- It does not include accounts, subscriptions, credits, cookies, or proxies.
- It cannot guarantee uninterrupted operation when Google changes Flow.
- It grants no rights to third-party media. Upload only content you may use.

## Architecture

```text
Designer browser
      │ HTTPS + authentication
      ▼
Nginx / reverse proxy
      │ 127.0.0.1:4000
      ▼
VisualFlow Studio (FastAPI)
      ├── Dashboard and REST API
      ├── SQLite task catalog
      ├── Local/S3-compatible media storage
      ├── FFmpeg processing
      └── gflow-cli → Playwright → system Chrome → flow.google.com
                              │
                              └── persistent private Chrome profile
```

Jobs sharing one Chrome profile are intentionally serialized. For parallel
work, isolate independent authorized profiles; never point concurrent Chrome
processes at one profile directory.

## Requirements

- Linux x86-64 (Ubuntu 24.04 LTS is the tested production target);
- Docker Engine and Docker Compose v2;
- 4 GB RAM minimum, 8 GB recommended;
- a Google account with legitimate Google Flow access and sufficient credits;
- one interactive login to create the persistent Chrome profile.

The production image installs Python 3.11, Chrome, Xvfb, Node.js, and FFmpeg.

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/The88788/VisualFlow-Studio.git
cd visualflow-studio
cp config/setting.example.toml config/setting.toml
```

```toml
[generation]
provider_mode = "gflow"
max_concurrent_videos = 1

[gflow]
home = "/app/gflow_profiles"
profile = "client"
project_id = "YOUR_FLOW_PROJECT_UUID"
executable = "/usr/local/bin/gflow"
timeout = 1200
```

### 2. Authorize the profile

In an interactive desktop session run
`gflow auth login --profile client --browser chrome`, finish the Flow sign-in,
close Chrome cleanly, and place `profile_client` under `gflow_profiles/`.

That profile is a reusable authenticated Google session. Never commit it,
share it, include it in an image, or expose it through a web server.

### 3. Build and start

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
curl http://127.0.0.1:4000/health
```

```json
{"status":"ok","service":"VisualFlow Studio","version":"1.1.2.34"}
```

Production binds only to `127.0.0.1:4000`. Publish it through an authenticated
HTTPS reverse proxy. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Configuration

| Section | Key | Purpose |
|---|---|---|
| `generation` | `provider_mode` | Use `gflow` for current Flow accounts |
| `generation` | `max_concurrent_videos` | Application generation concurrency |
| `gflow` | `home` | Private browser-profile directory |
| `gflow` | `profile` | gflow profile name |
| `gflow` | `project_id` | Existing Flow project |
| `gflow` | `timeout` | Provider command timeout |
| `cleanup` | `max_age_days` | Result retention (default: 60 days) |
| `cleanup` | `tmp_max_hours` | Temporary-file retention |
| `ip_whitelist` | `enabled`, `ips` | Optional application IP restriction |

Use [`config/setting.example.toml`](config/setting.example.toml) as the source
of truth. Never commit `config/setting.toml`.

The optional legacy refresh-token flow additionally requires
`VEOAPI_GOOGLE_OAUTH_CLIENT_ID` and `VEOAPI_GOOGLE_OAUTH_CLIENT_SECRET` in the
process environment. Do not place OAuth credentials in TOML files, Compose
files, images, or Git.

## API overview

Protected installations expose OpenAPI documentation at `/docs`.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Health and version |
| `GET` | `/api/dashboard/data` | Dashboard state |
| `POST` | `/api/images` | Create or refine images |
| `POST` | `/api/images/{id}/retry` | Retry a failed image task |
| `GET` | `/api/images/{id}` | Image-task status |
| `GET` | `/api/images/{id}/download/{index}` | Original image |
| `POST` | `/api/images/{id}/upscale/{index}?scale=2k` | Prepare 2K |
| `GET` | `/api/images/{id}/download/{index}/2k` | Download 2K |
| `DELETE` | `/api/images/{id}` | Delete task files and record |
| `POST` | `/api/videos` | Create a video |
| `GET` | `/api/videos/{id}` | Video status |
| `GET` | `/api/videos/{id}/download` | Download MP4 |

```bash
curl -X POST http://127.0.0.1:4000/api/images \
  -F 'prompt=Editorial product photo on a dark background' \
  -F 'model=nano2' -F 'aspect=4:3' -F 'count=1' \
  -F 'references=@./product.png'
```

## Security before publication

1. Keep port 4000 on loopback and terminate HTTPS at a reverse proxy.
2. Protect the dashboard with authentication; it is not an anonymous endpoint.
3. Keep `config/`, `data/`, `logs/`, `browser_profiles/`, and
   `gflow_profiles/` out of public artifacts.
4. Never commit cookies, passwords, keys, proxy credentials, client media,
   SQLite files, incident bundles, HAR files, or debug screenshots.
5. Scan the complete Git history before making a private repository public.
   Deleted secrets remain in history and must be rotated.

See [SECURITY.md](SECURITY.md).

## Providers and licensing

The recommended provider invokes
[`gflow-cli`](https://github.com/ffroliva/gflow-cli), an unofficial
MIT-licensed tool. The build applies version-guarded compatibility patches and
fails closed when the expected upstream source layout changes.

The optional legacy provider can use CloakBrowser. Its wrapper is MIT-licensed,
but its downloaded Chromium binary has separate terms and may require an
OEM/SaaS agreement for customer-facing browser services. The binary is not
distributed here. Review upstream terms before enabling that provider.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Contributing

VisualFlow Studio is an independent early-stage project built against a
changing third-party web application. Reliability, tests, documentation,
provider compatibility, UI, and deployment contributions are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
The planned direction is documented in [ROADMAP.md](ROADMAP.md).

## License

VisualFlow Studio is released under the [MIT License](LICENSE). Third-party
components and downloaded browser binaries remain subject to their own terms.
