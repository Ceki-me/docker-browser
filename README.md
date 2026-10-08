# Ceki headless-browser provider (Docker)

Run a **provider** browser for Ceki from Docker. The container starts a real
browser (Chromium by default, or Yandex / pseudo-Yandex flavors) with the Ceki
extension installed, injects your provider token and keeps the browser
**online** so other people can rent it as a public browser.

## Prerequisites

- **Docker** (any recent version)
- A **provider token** (`CEKI_PROVIDER_TOKEN`) — a one-time browser token from
  your Ceki dashboard (the "rent out my browser" flow). One token = one
  schedule = one container.

## Quick start (single browser, `app` mode)

```bash
docker run --rm \
  -e CEKI_PROVIDER_TOKEN=<your-token> \
  ceki/provider
```

The container starts a virtual display (Xvfb), launches the browser with the
extension, brings your browser **online** and keeps it there until someone rents
it or you stop the container (default `app` mode, one browser per provider).

### With docker compose

> The image bundles the browser-extension dist, which must be staged **before**
> building. Run `./build.sh` first (it downloads the latest extension release
> into the git-ignored `extension/` directory). If you skip this, `docker
> compose up --build` fails at the `COPY extension/` step.

```bash
export CEKI_PROVIDER_TOKEN=<your-token>
./build.sh                          # stage the extension dist first (required)
docker compose up -d --build
docker compose logs -f provider
docker compose stop provider
```

## Daemon mode (recommended, multi-session)

Set `CEKI_DAEMON=1` to run the **provider daemon**: one long-lived provider-WS
connection to the relay per schedule, and a **dedicated browser instance per
incoming rent** (its own Xvfb display, its own CDP port, its own profile under
`/sessions`). Multiple concurrent rentals run in parallel browsers instead of
one shared browser.

```bash
docker run --rm \
  -e CEKI_PROVIDER_TOKEN=<your-token> \
  -e CEKI_PROVIDER_SCHEDULE_ID=<schedule-id> \
  -e CEKI_DAEMON=1 \
  ceki/provider
```

Key daemon env (all optional except the token):

| Env var | Default | Description |
|---|---|---|
| `CEKI_DAEMON` | `0` | Set to `1` to run the SDK provider daemon instead of the single-browser `app` launcher. |
| `CEKI_DAEMON_PORT` | `17890` | Local WS endpoint the extension inside each browser connects to (`ws://127.0.0.1:<port>`). |
| `CEKI_DAEMON_MAX_SESSIONS` | `1` | Max concurrent rental sessions (each spawns its own browser). |
| `CEKI_DAEMON_CDP_START` | `9223` | First CDP debug port of the pool (increments per session). |
| `CEKI_DAEMON_DISPLAY_START` | `101` | First Xvfb display number of the pool (increments per session). |
| `CEKI_DAEMON_STORAGE_KEY` | `session_id` | Profile-dir key: `session_id`, or a `billable_type:billable_id`-style composite key from the match payload. |
| `CEKI_DAEMON_BOOT_STAGGER_S` | `2` | Delay between parallel session spawns (spreads the RAM/CPU boot burst). |
| `CEKI_DAEMON_CONNECT_TIMEOUT` | `90` | Give-up time for a session's extension presence-WS to connect before the watchdog reaps it. |
| `CEKI_SESSION_DIR` | `/sessions` | Base dir for ephemeral (incognito) session profiles. |
| `CEKI_SESSION_PERSIST_DIR` | `/sessions-persist` | Base dir for persistent `main`-profile rentals. |

Profiles are wiped by default. `profile_mode` from the match decides retention:
a `main`-mode rent keeps its profile under `/sessions-persist`, an incognito /
unset rent is ephemeral and removed on session end. The daemon implements no
quota/retention for the persist dir — host-side cleanup owns that.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `CEKI_PROVIDER_TOKEN` | — | **Required.** One-time browser token from your dashboard. |
| `CEKI_PROVIDER_SCHEDULE_ID` | derived | Browser/schedule ID. Usually derived from `/api/browser/me`; set it explicitly to pin the schedule. |
| `CEKI_PROVIDER_BROWSER` | `chromium` | Browser flavor: `chromium` (Playwright Chromium), `yandex` (real Yandex Browser corporate build), `pseudo-yandex` (Chromium with a YaBrowser UA). |
| `CEKI_PROVIDER_VIEWPORT` | `1920x1080` | Browser viewport / resolution (WxH). Full HD by default; drives both the Chromium viewport and the Xvfb screen. |
| `CEKI_PROVIDER_EXT_DIR` | `/opt/ceki/extension` | Path to the unpacked extension dist (used by the bundled `--load-extension` fallback). |
| `CEKI_PROVIDER_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (also set by `--verbose`). |
| `CEKI_WS_URL` | `wss://browser.ceki.me/ws/provider` | Provider relay WS endpoint (prod default). Internal overrides only — the extension build is patched to match at launch. |
| `CEKI_API_URL` | `https://api.ceki.me` | Backend API base URL (prod default). |
| `CEKI_PROVIDER_IDLE_URL` | `https://ceki.me` | Page shown while the provider is idle (no active rent). |
| `CEKI_PROVIDER_OPEN_NORMAL` | `1` | Open rental windows in normal (not minimized) state. |
| `CEKI_PROVIDER_OPEN_FOCUSED` | `1` | Open rental windows focused. |
| `CEKI_PROVIDER_RESTORE_FOCUS_ON_RENTAL` | `0` | Restore focus to the rental window on each rent. |
| `DISPLAY` | `:99` | X display for the virtual screen. |
| `TZ` | host timezone | Browser timezone (keeps it consistent with your location). |

### Extension source / update

The extension is normally installed and auto-updated by the browser itself from
an update channel (external policy / ExtensionInstallForcelist). The following
env control that and the bundled-copy fallback:

| Env var | Default | Description |
|---|---|---|
| `CEKI_EXT_POLICY_URL` | prod `/ext/updates.xml` | update channel for the external-extension policy. |
| `CEKI_EXT_UPDATE_URL` | prod `/ext/ceki-browser-extension-latest.crx` | CRX channel used to keep the bundled unpacked copy fresh at start. |
| `CEKI_EXT_SKIP_UPDATE` | unset | Set to `1` to disable the external policy AND the startup update (offline / local run). |
| `CEKI_MANAGED_RELAY_WS` | `ws://127.0.0.1:<daemon_port>` | `relay_ws` written into the extension's managed policy (daemon mode). |
| `CEKI_MANAGED_BACKEND_API` / `CEKI_MANAGED_CHAT_API` | unset | Optional backend/chat API overrides in the same managed policy. |

### Build-time env (used by `build.sh`)

| Env var | Default | Description |
|---|---|---|
| `CEKI_BROWSER_FLAVOR` | `chromium` | `chromium` \| `yandex` \| `pseudo-yandex` — selects which Dockerfile + image tag to build. |
| `CEKI_IMAGE` | `ceki/provider:<flavor>` | Override the output image tag. |
| `CEKI_EXT_URL` / `CEKI_EXT_ZIP` / `CEKI_EXT_CRX` / `CEKI_EXT_DIST` | — | Extension source for the build (URL / local zip / local crx / unpacked dir). |

### Debug capture (opt-in)

Set `CEKI_PROVIDER_DEBUG_LOG` to a file path to enable CDP console/exception
capture for the provider extension plus a service-worker liveness probe.

| Env var | Default | Description |
|---|---|---|
| `CEKI_PROVIDER_DEBUG_LOG` | — | Enable; path to append captured extension console logs to (`1`/`true` → `/var/log/ceki-provider/ext-console.log`). |
| `CEKI_PROVIDER_DEBUG_PORT` | `9333` | Additional CDP port for debug capture. |
| `CEKI_PROVIDER_DEBUG_SW_PING` | `30` | Service-worker liveness ping interval (seconds). |
| `CEKI_PROVIDER_DEBUG_SW_TIMEOUT` | `15` | Seconds to wait for a ping ack before logging `SW-UNRESPONSIVE`. |

The image ships with the **PROD** environment baked in and defaults to prod —
no environment configuration is needed beyond the token.

## Stopping / cleanup

`docker stop` sends a clean shutdown signal: the rented browser is closed and
your browser goes **offline**. `docker compose stop` does the same.

## Building from source

The image bundles the browser-extension dist. The build script stages it into
the git-ignored `extension/` directory before running `docker build`:

```bash
./build.sh                       # default: download the latest extension release
```

By default `build.sh` downloads the latest published extension release bundle
(`ceki-browser-extension-latest.zip`) from the extension host, so a build works
on a fresh clone with no local extension checkout. You can point it at any other
source instead:

```bash
./build.sh --url https://browser.ceki.me/ext/ceki-browser-extension-latest.zip   # download zip
./build.sh --url https://browser.ceki.me/ext/ceki-browser-extension-latest.crx   # download crx
./build.sh --zip ./ceki-browser-extension-latest.zip                             # local zip
./build.sh --crx ./ceki-browser-extension-latest.crx                            # local crx
./build.sh --dir /path/to/unpacked/dist                                          # local build
./build.sh /path/to/unpacked/dist                                                 # same as --dir
```

Build a specific browser flavor:

```bash
CEKI_BROWSER_FLAVOR=yandex ./build.sh          # yandex image  → ceki/provider:yandex
CEKI_BROWSER_FLAVOR=pseudo-yandex ./build.sh   # pseudo-yandex → ceki/provider:pseudo-yandex
```

This produces the image locally. The published images (all three flavors) are
built from tagged releases by `.github/workflows/docker-publish.yml`.

## Notes

- In `app` mode (default) one container serves one browser. To run several
  providers, start several containers, each with its own token.
- In daemon mode (`CEKI_DAEMON=1`) one container can serve up to
  `CEKI_DAEMON_MAX_SESSIONS` concurrent rentals, each in its own browser.
- The token is bound to the specific browser/schedule it was issued for; it
  cannot be reused for another.
- Unbranded Chromium trusts the Russian Trusted CA on top of its own root store
  so RU bank / government sites connect without certificate errors. The yandex
  flavor ships that CA natively.