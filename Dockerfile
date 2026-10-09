# syntax=docker/dockerfile:1
# ===== BEGIN hostim-root-mirror-note =====
# This root Dockerfile is a byte-for-byte MIRROR of hostim/Dockerfile.
#
# چرا این فایل در ریشهٔ مخزن هست؟
# Hostim یک build از منبع Git را به BuildKit به شکل «<repo>.git#<commit>»
# می‌دهد. در این syntax، fragment می‌تواند «#<ref>:<subdir>» باشد تا context
# یک زیرپوشه شود، ولی Hostim فقط «#<commit>» می‌فرستد؛ پس context ریشهٔ مخزن
# است و نام Dockerfile هم پیش‌فرضِ «Dockerfile» در همان ریشه. اولین build
# واقعی دقیقاً به همین دلیل شکست خورد:
#   failed to read dockerfile: open Dockerfile: no such file or directory
# چون فایل ما hostim/Dockerfile بود.
#
# این فایل فقط برای باز کردن همان مسیر پیش‌فرض است. **منبع حقیقت
# hostim/Dockerfile است**؛ هر تغییری را اول آنجا بدهید و بعد این فایل را
# بازتولید کنید (دستورش در hostim/GUIDE.fa.md، بخش «mirror ریشه»). اگر این دو
# از هم جدا شوند تست
# tests/test_hostim_deploy.py::TestRootMirrorOfHostimDockerfile
# بلافاصله شکست می‌خورد، پس نمی‌تواند بی‌سروصدا کهنگه بماند.
#
# مسیر Google Colab هیچ تغییری نکرده است: Colab هیچ image ای build نمی‌کند و
# install.sh / start_colab_browser.sh / stop_colab_browser.sh دست‌نخورده‌اند.
# ===== END hostim-root-mirror-note =====
#
# خلاصهٔ فارسی: image کانتینر مخصوص مسیر Hostim. Ubuntu 24.04 + میزکار مجازی
# (Xvfb، fluxbox، x11vnc، noVNC) + Google Chrome + سرور پایتون، بدون cloudflared.
# فقط یک پورت (6080) منتشر می‌شود؛ 5901 و 9222 خصوصی می‌مانند. کانتینر با
# کاربر غیرroot اجرا می‌شود و tini به‌عنوان PID 1 زامبی‌ها را جمع می‌کند.
# build context ریشهٔ مخزن است:
#   docker build -f hostim/Dockerfile -t majid-automation-hostim:local .
# در Hostim: Command Override را خالی بگذارید (وگرنه tini و سوپروایزر از بین
# می‌روند). مسیر Dockerfile را می‌توانید روی پیش‌فرض یعنی `Dockerfile` ریشه
# بگذارید، چون `Dockerfile` ریشه یک mirror بایت‌به‌بایت از همین فایل است
# (توضیح و دلیلش در همان فایل و در hostim/GUIDE.fa.md).
# راهنمای کامل: hostim/GUIDE.fa.md
#
# Container image for the Hostim (hostim.dev) deployment track.
#
# This file belongs to the Hostim add-on only. The Google Colab path keeps
# using install.sh + start_colab_browser.sh + a Cloudflare Quick Tunnel and is
# not touched by anything inside hostim/.
#
# The build context is the REPOSITORY ROOT, because the image needs
# automation/ and browser_control.sh:
#
#   docker build -f hostim/Dockerfile -t majid-automation-hostim:local .
#
# On Hostim, deploy from Git. This file is the canonical one; the repository
# root carries "Dockerfile" as a byte-for-byte mirror of it, because Hostim
# hands BuildKit a git source of the form "<repo>.git#<commit>", whose context
# is the repository root and whose Dockerfile name is the default "Dockerfile".
# Both paths therefore work; the root mirror is the one that is proven to.
# If you edit this file, regenerate the mirror - the test suite fails if they
# drift apart (tests/test_hostim_deploy.py::TestRootMirrorOfHostimDockerfile).
#
# Deliberate differences from the Colab launcher:
#   * no cloudflared and no Cloudflare Quick Tunnel - Hostim terminates HTTPS
#     on its own ingress and gives the app a domain,
#   * the single public port is 0.0.0.0:$PORT instead of 127.0.0.1:6080,
#   * x11vnc (5901) and Chrome CDP (9222) stay on loopback and are NOT exposed,
#   * persistent state lives on a mounted volume at /data instead of .runtime/,
#   * the container runs as a non-root user.

FROM ubuntu:24.04

# Hostim nodes are linux/amd64 only. An arm64 image fails with "exec format
# error" and produces no logs at all, so refuse to build one by accident.
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive

# One HTTP port carries noVNC, the sidebar, the JSON API and the VNC WebSocket.
# PORT must match the app's httpPort in the Hostim app settings.
ENV PORT=6080 \
    DISPLAY=:1 \
    VNC_PORT=5901 \
    CDP_PORT=9222 \
    SCREEN=1366x768x24 \
    APP_DIR=/app \
    DATA_DIR=/data \
    START_URL=https://www.google.com/ \
    HOME=/home/automation \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# ---------------------------------------------------------------------------
# Desktop, browser and VNC dependencies - the same package set install.sh uses
# on Colab, minus cloudflared, plus tini (PID 1 that reaps zombies) and
# python3 itself, which the bare ubuntu image does not ship.
# ---------------------------------------------------------------------------
RUN set -eux; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends \
        ca-certificates \
        curl \
        dbus-x11 \
        fluxbox \
        fontconfig \
        fonts-liberation \
        iproute2 \
        novnc \
        numlockx \
        openssl \
        procps \
        python3 \
        scrot \
        tini \
        websockify \
        wmctrl \
        x11-utils \
        x11-xkb-utils \
        x11vnc \
        xclip \
        xdotool \
        xvfb; \
    # automation/server.py imports websockify as a Python module (it drives
    # top_new_client itself), so the CLI package alone is not enough. Fall back
    # to pip only when the distribution package does not provide the module,
    # and fail the build loudly if it is still missing: a missing module means
    # the whole sidebar and API would be dead at runtime.
    if ! python3 -c 'import websockify.websocketproxy' 2>/dev/null; then \
        apt-get install -y -qq --no-install-recommends python3-pip; \
        python3 -m pip install --quiet --break-system-packages websockify \
            || python3 -m pip install --quiet websockify; \
    fi; \
    python3 -c 'import websockify.websocketproxy; print("websockify module: OK")'; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Persian (and any other non-Latin) glyph coverage.
#
# Without an Arabic-script font Chrome draws every Persian character as an
# empty box, and the operator cannot read the pages the automation works on.
# Ubuntu 24.04 has no dedicated Persian font package - there is no
# fonts-noto-naskh-arabic and nothing with "arabic" in its name that ships a
# usable outline font - so instead of trusting one guessed package name this
# layer installs candidates one at a time and gates the result on a real check:
# fc-list must report at least one font covering Persian (lang=fa). A renamed
# or missing package can then never ship a silently broken image; the build
# stops with a readable message instead.
# ---------------------------------------------------------------------------
RUN set -eux; \
    apt-get update -qq; \
    has_persian() { \
        fc-cache -f >/dev/null 2>&1 || true; \
        fc-list ':lang=fa' | grep -q .; \
    }; \
    for candidate in \
        fonts-noto-core \
        fonts-freefont-ttf \
        fonts-kacst \
        fonts-sil-scheherazade \
        fonts-noto-extra; do \
        if has_persian; then \
            echo "Persian coverage already satisfied; not installing $candidate"; \
            break; \
        fi; \
        echo "trying $candidate"; \
        apt-get install -y -qq --no-install-recommends "$candidate" \
            || echo "SKIP: $candidate is not available in this archive"; \
    done; \
    if ! has_persian; then \
        echo "ERROR: no Persian-capable font could be installed." >&2; \
        echo "       Chrome would render Persian text as empty boxes." >&2; \
        echo "       families known to fontconfig right now:" >&2; \
        fc-list : family 2>/dev/null | sort -u | head -20 >&2 || true; \
        exit 1; \
    fi; \
    echo "Persian-capable font families:"; \
    fc-list ':lang=fa' : family | sort -u | head -5; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Google Chrome, the same amd64 .deb install.sh downloads on Colab.
# ---------------------------------------------------------------------------
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "amd64" ]; then \
        echo "ERROR: this image must be built for linux/amd64 (Hostim nodes are amd64); got: $arch" >&2; \
        echo "       build with: docker buildx build --platform linux/amd64 -f hostim/Dockerfile ." >&2; \
        exit 1; \
    fi; \
    curl -fsSL -o /tmp/google-chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb; \
    apt-get update -qq; \
    apt-get install -y -qq --no-install-recommends /tmp/google-chrome.deb; \
    rm -f /tmp/google-chrome.deb; \
    rm -rf /var/lib/apt/lists/*; \
    google-chrome --version

# ---------------------------------------------------------------------------
# Non-root runtime user. Hostim's docs do not state which user an app runs as,
# so the image does not depend on being root: everything below works as uid
# 1000, and Chrome runs with --no-sandbox exactly like the Colab launcher.
#
# ubuntu:24.04 ships a stock "ubuntu" account already sitting on uid/gid 1000,
# so a plain `groupadd --gid 1000` dies with "GID '1000' already exists" (exit
# code 4) - which is exactly what the first real Hostim build did. Take the id
# over instead of inventing a new one, and do it generically (look the id up,
# never assume the account is called "ubuntu"): a deterministic uid keeps the
# files already on the /data volume owned by the same account after a rebuild,
# which matters because secrets.env is mode 0600 and unreadable to anyone else.
# ---------------------------------------------------------------------------
RUN set -eux; \
    if getent passwd 1000 >/dev/null; then \
        stock_user="$(getent passwd 1000 | cut -d: -f1)"; \
        echo "uid 1000 is taken by the stock account '$stock_user'; removing it"; \
        userdel -r "$stock_user" || userdel "$stock_user"; \
    fi; \
    if getent group 1000 >/dev/null; then \
        stock_group="$(getent group 1000 | cut -d: -f1)"; \
        echo "gid 1000 is taken by the stock group '$stock_group'; removing it"; \
        groupdel "$stock_group"; \
    fi; \
    groupadd --system --gid 1000 automation; \
    useradd --system --uid 1000 --gid 1000 --create-home --home-dir /home/automation \
        --shell /bin/bash automation; \
    id automation; \
    test -d /home/automation

WORKDIR /app

# Only what the runtime needs. The Colab launchers (install.sh,
# start_colab_browser.sh, stop_colab_browser.sh), the tests and the docs stay
# out of the image, so nothing in here can change Colab behaviour.
COPY automation/ /app/automation/
COPY browser_control.sh /app/browser_control.sh
COPY hostim/ /app/hostim/

RUN set -eux; \
    chmod 0755 /app/browser_control.sh /app/hostim/*.sh; \
    # /data is the volume mount point. Hostim makes a freshly attached volume
    # writable when the app starts; pre-creating it only helps `docker run`.
    mkdir -p /data /home/automation; \
    chown -R automation:automation /app /data /home/automation; \
    find /app -name '__pycache__' -type d -prune -exec rm -rf {} +

# The only port this image publishes. 5901 (x11vnc) and 9222 (Chrome CDP) are
# deliberately absent: Hostim exposes a single HTTP port per app anyway, and
# the automation server proxies both of them from inside the container.
EXPOSE 6080

# Kubernetes (and therefore Hostim) ignores a Dockerfile HEALTHCHECK; Hostim
# uses its own readiness probe, which should point at /automation/api/info.
# This line still makes `docker run` / `docker compose` report health locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["/app/hostim/healthcheck.sh"]

USER automation

# tini stays PID 1 so orphaned Chrome helpers are reaped instead of piling up
# as zombies, and forwards SIGTERM to the entrypoint, which shuts the whole
# process tree down in order. Do NOT set a Hostim "Command Override": it
# replaces ENTRYPOINT and CMD completely and would drop tini.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/hostim/docker-entrypoint.sh"]
