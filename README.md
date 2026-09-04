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

## Configuration

| Env var | Default | Description |
|---|---|---|
| `CEKI_PROVIDER_TOKEN` | — | **Required.** One-time browser token from your dashboard. |
| `CEKI_PROVIDER_VIEWPORT` | `1920x1080` | Browser viewport / resolution (WxH). Full HD by default; drives both the Chromium viewport and the Xvfb screen (+120px height margin for full-page screenshots). |
| `CEKI_PROVIDER_EXT_DIR` | `/opt/ceki/extension` | Path to the unpacked extension dist (used by the bundled `--load-extension` fallback). Only needed when staging the extension somewhere else. |
| `CEKI_PROVIDER_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` (also set by `--verbose`). |
| `DISPLAY` | `:99` | X display for the virtual screen. |
| `TZ` | host timezone | Browser timezone (keeps it consistent with your location). |

The image ships with the **PROD** environment baked in and defaults to prod —
no environment configuration is needed beyond the token.

```bash
docker run --rm -e CEKI_PROVIDER_TOKEN=<your-token> ceki/provider
```

### Extension auto-update (external policy)

By default the container asks **Chrome itself** to install and keep the Ceki
extension updated. The entrypoint writes an external-extension policy file
(`/usr/share/chromium/extensions/<id>.json`) that points at the prod update
channel (`https://browser.ceki.me/ext/updates.xml`). On first launch Chrome
installs the extension from the channel, and afterwards **re-checks the channel
every few hours and updates the running extension automatically** — no rebuild,
no container restart, even for a provider that has been online for months.

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
./build.sh --url https://host.example.com/ext/ceki-browser-extension-latest.zip   # download zip
./build.sh --url https://host.example.com/ext/ceki-browser-extension-latest.crx   # download crx
./build.sh --zip ./ceki-browser-extension-latest.zip                              # local zip
./build.sh --crx ./ceki-browser-extension-latest.crx                             # local crx
./build.sh --dir /path/to/unpacked/dist                                          # local build
./build.sh /path/to/unpacked/dist                                                 # same as --dir
```

Environment equivalents: `CEKI_EXT_URL`, `CEKI_EXT_ZIP`, `CEKI_EXT_CRX`,
`CEKI_EXT_DIST`. Without any source the script falls back to a local clone of
`browser-extension` if one is found next to this repo.

This produces the `ceki/provider:latest` image locally. The published image on
Docker Hub is built automatically from tagged releases.

## Notes

- One browser per container. To run several providers, start several containers,
  each with its own token.
- The token is bound to the specific browser it was issued for; it cannot be
  reused for another browser.
