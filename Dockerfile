# ─── Stage 1: build the React SPA ────────────────────────────────────
FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# Path prefix for the deployment (e.g. /canopy/ as a labs tenant). Defaults to
# "/" (root / GCP). Drives Vite base → import.meta.env.BASE_URL.
ARG VITE_BASE_PATH=/
# Django CSRF cookie name. Path-scoped per tenant on the shared labs host
# (csrftoken_canopy for /canopy) so writes send the right token; defaults to
# Django's "csrftoken" for the root deployment.
ARG VITE_CSRF_COOKIE_NAME=csrftoken
RUN VITE_BASE_PATH="$VITE_BASE_PATH" \
    VITE_CSRF_COOKIE_NAME="$VITE_CSRF_COOKIE_NAME" \
    npm run build


# ─── Stage 2: Python runtime ─────────────────────────────────────────
FROM python:3.12-slim

# Install Node.js for the optional Claude Code CLI backend
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Install Python deps first for better layer caching. canopy-web depends on the
# in-repo `canopy-runs` package (a uv path source — see [tool.uv.sources]), so
# copy it into the context and install via uv (which resolves path sources);
# plain `pip install .` can't find canopy-runs on PyPI.
COPY pyproject.toml uv.lock ./
COPY packages/ ./packages/
RUN pip install uv && uv pip install --system .

# Application code. This includes ./canopy when the deploy step has cloned the
# (private) canopy plugin into the build context — see deploy.sh. apps.system
# reads it at /app/canopy/plugins/canopy to render the /system catalog;
# settings' _resolve_canopy_plugin_path() locates it. When absent (local builds,
# missing token), the catalog degrades to an empty state with a warning.
COPY . .

# Built SPA from stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Collect static assets so WhiteNoise can serve them.
# SECRET_KEY/DATABASE_URL aren't used by collectstatic; dummy values keep Django happy.
ENV DJANGO_SETTINGS_MODULE=config.settings.production
RUN SECRET_KEY=build-placeholder \
    DATABASE_URL=sqlite:///tmp/build.sqlite3 \
    ALLOWED_HOSTS=* \
    GOOGLE_OAUTH_CLIENT_ID=placeholder \
    GOOGLE_OAUTH_CLIENT_SECRET=placeholder \
    python manage.py collectstatic --noinput

# The sha of the last commit that touched the laptop runner's own source, passed
# in by the deploy workflow (which needs fetch-depth: 0 to compute it). A runner
# reports the same quantity on its heartbeat and the supervisor alerts when they
# differ. Deliberately placed LATE: an ARG/ENV invalidates every layer below it,
# and this value changes on every runner commit — up top it would rebuild the
# whole image each time. Empty (local builds) disables the comparison.
ARG RUNNER_CODE_SHA=""
ENV RUNNER_CODE_SHA=$RUNNER_CODE_SHA
# Committer epoch of that same commit — the ordering the sha can't carry.
ARG RUNNER_CODE_COMMITTED_AT="0"
ENV RUNNER_CODE_COMMITTED_AT=$RUNNER_CODE_COMMITTED_AT

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
# --ws-ping-interval 5: uvicorn sends a WebSocket ping every 5s. This IS the idle
# keepalive for long-lived sockets (runner control channel, supervisor + turn
# tails): the server→client traffic keeps the shared ALB from idle-closing the
# connection, and clients auto-pong. Proven live: a fully idle WS holds 90s+ with
# only these pings. (The earlier erratic ~5-10s drops were NOT the proxy — they
# were the channels_redis layer's blocking reads timing out on ElastiCache and
# killing the consumer; fixed by moving the single-task channel layer to InMemory.
# See config/settings/connectlabs.py.) --ws-ping-timeout 20 closes a peer that
# stops ponging.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "5", "--ws-ping-timeout", "20"]
