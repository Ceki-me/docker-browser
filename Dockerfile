# syntax=docker/dockerfile:1
#
# Ceki headless-browser provider image.
#
# Runs the provider launcher (src/ceki_browser_provider):
#   * Chromium (Playwright) — the rented public browser
#   * the ceki browser extension (dist) — the provider agent inside the browser
#   * the provider launcher — launches Chromium + extension, injects the token
#     and keeps the browser online until a renter connects or the process stops
#
# The browser token is passed at runtime via CEKI_PROVIDER_TOKEN.
#
# Build context = repo root. `build.sh` stages the extension dist into
# extension/ (git-ignored) before `docker build`.

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99 \
    PYTHONPATH=/opt/ceki/src

WORKDIR /opt/ceki

# OCI annotations link the GHCR image to this repository, so the image shows
# under the repo's Packages tab and Actions' GITHUB_TOKEN can manage it
# (visibility, deletion). Without a source label the image is orphaned.
LABEL org.opencontainers.image.source=https://github.com/Ceki-me/docker-browser \
      org.opencontainers.image.title=ceki-browser-provider \
      org.opencontainers.image.description="Ceki headless-browser provider image: Chromium + browser extension + provider launcher"

# Chromium runtime libraries + Xvfb virtual display (Chromium needs a display to
# run as a "visible" provider browser; Xvfb provides it headlessly).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
        libglib2.0-0 libgdk-pixbuf-2.0-0 xvfb xauth x11-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps: the SDK (API client + config) and Playwright (Chromium driver).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Provider launcher module (this repo).
COPY src/ /opt/ceki/src/

# Chromium pinned by Playwright (used only by the provider browser).
RUN python -m playwright install chromium

# Bundled browser extension dist (staged into extension/ by build.sh).
COPY extension/ /opt/ceki/extension/

# CEKI_PROVIDER_TOKEN is supplied at runtime (docker run -e ...).
ENV CEKI_PROVIDER_EXT_DIR=/opt/ceki/extension \
    CEKI_PROVIDER_LOG_LEVEL=INFO

# Entrypoint starts Xvfb (if needed) and then execs the command as PID 1 so a
# `docker stop` (SIGTERM to PID 1) reaches the provider for a clean shutdown.
COPY entrypoint.sh /usr/local/bin/ceki-entrypoint
RUN chmod +x /usr/local/bin/ceki-entrypoint

ENTRYPOINT ["/usr/local/bin/ceki-entrypoint"]
CMD ["python", "-m", "ceki_browser_provider.app"]
