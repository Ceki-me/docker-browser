# syntax=docker/dockerfile:1
#
# Ceki headless-browser provider image — CHROMIUM flavor (default).
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
#
# Sibling images: Dockerfile.yandex (real Yandex Browser),
# Dockerfile.pseudo-yandex (Chromium with a YaBrowser UA).

FROM python:3.11-slim

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
# libnss3-tools: certutil for the NSS trust DB — see the Russian Trusted CA block.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libnss3-tools libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
        libglib2.0-0 libgdk-pixbuf-2.0-0 xvfb xauth x11-utils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Russian Trusted Root CA / Sub CA (Минцифры, НУЦ) — the CA that sanctioned
# RU banks/sites (sberbank.ru, tbank, gov services) switched to after Western
# CAs stopped serving them. Not in the Mozilla/Chrome root programs, so plain
# Chromium fails those sites with ERR_CERT_AUTHORITY_INVALID (Yandex Browser
# ships this root in its own build, which is why the yandex image opens them
# without any of this). Install into both trust paths:
#   - system CA bundle (update-ca-certificates) → curl/python/requests inside
#     the container, and Chromium when no NSS user DB exists
#   - the machine-wide NSS DB (/etc/pki/nssdb) → Chromium's "locally managed
#     roots" policy (Chrome on Linux reads NSS DBs in addition to its own
#     root store; local roots skip the CT/public-audit requirements)
# Chromium probes the *user's* ~/.pki/nssdb first and falls back to the
# machine-wide /etc/pki/nssdb — the container runs as root, so /root/.pki/nssdb
# is seeded too (both DBs, so a different USER works as well).
# Source: gu-st.ru (Mintsifry's own hosting). COPY from certs/ (in-repo) so
# builds are reproducible and don't depend on gu-st.ru uptime.
COPY certs/russian_trusted_root_ca.crt certs/russian_trusted_sub_ca.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates \
    && for db in /etc/pki/nssdb /root/.pki/nssdb; do \
        mkdir -p "$db" \
        && certutil -N -d "sql:$db" --empty-password \
        && certutil -A -d "sql:$db" -t "C,," -n "Russian Trusted Root CA" \
            -i /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
        && certutil -A -d "sql:$db" -t "C,," -n "Russian Trusted Sub CA" \
            -i /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt; \
    done

# Python deps: the SDK (API client + config) and Playwright (Chromium driver).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Provider launcher module (this repo).
COPY src/ /opt/ceki/src/

# Chromium pinned by Playwright — the provider browser on this image.
RUN python -m playwright install chromium

# This image rents out Playwright's Chromium (app.py reads the env to pick the
# binary; settable at runtime, e.g. CEKI_PROVIDER_BROWSER=pseudo-yandex is
# meaningless here — the YaBrowser UA patch lives on the pseudo-yandex image).
ENV CEKI_PROVIDER_BROWSER=chromium

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
