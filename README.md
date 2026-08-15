# Ceki headless-browser provider (Docker)

Run a **provider** browser for [browser.ceki.me](https://browser.ceki.me) from Docker.

The container starts a real Chromium browser with the Ceki extension installed,
injects your browser token and keeps the browser **online** so other people can
rent it as a public browser while you're not using your machine.

This image is a thin wrapper around the provider launcher — a small program that
sets up the browser, connects it to the Ceki network and stays alive,
auto-accepting incoming rentals.

## Prerequisites

- **Docker** (any recent version)
- A **provider token** — a one-time browser token from your account dashboard on
  [browser.ceki.me](https://browser.ceki.me) (the "call a browser" / "rent out my
  browser" flow). One token = one browser = one container.

## Quick start

```bash
docker run --rm \
  -e CEKI_PROVIDER_TOKEN=<your-token> \
  ceki/provider
```

The container starts a virtual display, launches Chromium with the extension,
brings your browser **online** and keeps it there until someone rents it or you
stop the container.

### With docker compose

> The image bundles the browser-extension dist, which must be staged **before**
> building. Run `./build.sh` first (it copies the extension dist into the
> git-ignored `extension/` directory). If you skip this, `docker compose
> up --build` fails at the `COPY extension/` step.

```bash
export CEKI_PROVIDER_TOKEN=<your-token>
./build.sh                          # stage the extension dist first (required)
docker compose up -d --build
docker compose logs -f provider
docker compose stop provider
```

## Configuration

| Env var | Default | Description |
|---|---|---|
| `CEKI_PROVIDER_TOKEN` | — | **Required.** One-time browser token from your dashboard. |
| `CEKI_WS_URL` | `wss://browser.ceki.me/ws/provider` | Relay WebSocket URL. Overrides the PROD URL baked into the extension bundle at runtime (see below). |
| `CEKI_API_URL` | `https://api.ceki.me` | API base URL — used by the launcher (token handshake) and substituted into the extension bundle at runtime. |
| `CEKI_PROVIDER_VIEWPORT` | `1920x1080` | Browser viewport / resolution (WxH). Full HD by default; drives both the Chromium viewport and the Xvfb screen (+120px height margin for full-page screenshots). |
| `DISPLAY` | `:99` | X display for the virtual screen. |
| `TZ` | host timezone | Browser timezone (keeps it consistent with your location). |

### One image, environment at runtime

The image ships with the **PROD** environment baked in and defaults to prod.
The extension is static JS and cannot read container env, so the entrypoint
replaces the PROD URL strings in the bundled extension config before Chromium
starts when the env is set (and differs from the default). No separate dev
image is needed (`ceki/provider:dev` is deprecated).

```bash
# prod (default) — no env needed beyond the token
docker run --rm -e CEKI_PROVIDER_TOKEN=<your-token> ceki/provider

# dev stand — override the relay + API URLs
docker run --rm \
  -e CEKI_PROVIDER_TOKEN=<your-token> \
  -e CEKI_WS_URL=wss://browser.ittribe.org/ws/provider \
  -e CEKI_API_URL=https://clawapi.ittribe.org \
  ceki/provider
```

## Stopping / cleanup

`docker stop` sends a clean shutdown signal: the rented browser is closed and
your browser goes **offline**. `docker compose stop` does the same.

## Building from source

The image bundles the browser-extension dist, so the build script stages it
from a local copy of the extension before running `docker build`:

```bash
./build.sh                              # finds browser-extension/dist automatically
./build.sh /path/to/browser-extension/dist
```

This produces the `ceki/provider:latest` image locally. The published image on
Docker Hub is built automatically from tagged releases.

## Notes

- One browser per container. To run several providers, start several containers,
  each with its own token.
- The token is bound to the specific browser it was issued for; it cannot be
  reused for another browser.
