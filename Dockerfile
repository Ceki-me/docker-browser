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
#
# Browser flavor (build arg FLAVOR, consumed by build.sh):
#   chromium (default) — Playwright's pinned Chromium, as before
#   yandex             — also installs Yandex Browser (repo.yandex.ru deb) and
#                        defaults CEKI_PROVIDER_BROWSER=yandex at runtime, so
#                        the provider rents out a real YaBrowser build (its UA
#                        carries YaBrowser/<ver>, trusted harder by Yandex
#                        services). Playwright Chromium stays installed as a
#                        fallback binary.
#   pseudo-yandex      — the Playwright Chromium with a YaBrowser UA (string +
#                        Client Hints, applied by app.py at launch). NOT a real
#                        Yandex build — no Yandex internals, config channels or
#                        ytrust — but cheaper than the yandex image and fine
#                        where only the UA matters. Same image as chromium plus
#                        the flavor env (no extra packages needed).
# All flavors include the Russian Trusted CA (Минцифры) in the system and NSS
# trust stores — sanctioned RU sites (sberbank etc.) resolve their TLS on it.

FROM python:3.11-slim AS runtime

ARG FLAVOR=chromium

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
# ships this root in its own build, which is why the yandex flavor opens them
# without any of this). Install into both trust paths:
#   - system CA bundle (update-ca-certificates) → curl/python/requests inside
#     the container, and Chromium when no NSS user DB exists
#   - the machine-wide NSS DB (/etc/pki/nssdb) → Chromium's "locally managed
#     roots" policy (Chrome on Linux reads NSS DBs in addition to its own
#     root store; local roots skip the CT/public-audit requirements)
# Source: gu-st.ru (Mintsifry's own hosting). COPY from certs/ (in-repo) so
# builds are reproducible and don't depend on gu-st.ru uptime.
# Both trust paths get the roots:
#   - system CA bundle (update-ca-certificates) → curl/python inside the container
#   - NSS DBs → Chromium's "locally managed roots" (Chrome on Linux reads NSS
#     DBs in addition to its own root store; local roots skip CT/public-audit
#     requirements). Chromium probes the *user's* ~/.pki/nssdb first and falls
#     back to the machine-wide /etc/pki/nssdb — the container runs as root, so
#     /root/.pki/nssdb is seeded too (both DBs, so a different USER works as well).
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

# Chromium pinned by Playwright (used only by the provider browser).
RUN python -m playwright install chromium

# Yandex flavor: the CORPORATE deb from the official repo, plus its deps.
# Pinned implicitly by the pool snapshot at build time; entrypoint writes the
# ExtensionInstallForcelist policy into /etc/opt/yandex/browser/policies/managed
# when CEKI_PROVIDER_BROWSER=yandex. The deb ships /usr/bin/yandex-browser.
#
# Why corporate and not stable: only the corporate build honors extension
# policies. stable/beta strip the --load-extension switch outright and gate
# every other install path behind their own experiment system (the deb's own
# Extensions/*.json carries "experiment":"cdt2"; without it the file is
# skipped, and the same goes for external_update_url files and
# ExtensionSettings). The corporate build reads the managed-policy root
# normally — verified live: ExtensionInstallForcelist installs the extension
# from the update channel and its MV3 service worker starts.
RUN if [ "$FLAVOR" = "yandex" ]; then \
        apt-get update && apt-get install -y --no-install-recommends wget gnupg \
        && wget -qO- https://repo.yandex.ru/yandex-browser/YANDEX-BROWSER-KEY.GPG \
            | gpg --dearmor -o /usr/share/keyrings/yandex-browser.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/yandex-browser.gpg] https://repo.yandex.ru/yandex-browser/deb stable main" \
            > /etc/apt/sources.list.d/yandex-browser.list \
        && apt-get update && apt-get install -y --no-install-recommends yandex-browser-corporate \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# Default the provider browser to the image flavor (both are settable at
# runtime; CEKI_PROVIDER_BROWSER=yandex on the chromium image falls back to
# Chromium with a warning since the Yandex binary is absent).
ENV CEKI_PROVIDER_BROWSER=${FLAVOR}

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
