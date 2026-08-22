# The PRODUCTION web image: a compiled bundle behind a static server, no Node at
# runtime, non-root.
#
# `Dockerfile.dev` beside this one is what the compose stack runs, and that is
# deliberate — it is a Vite dev server with the source bind-mounted, so an edit
# on the host is live in the browser. This image is the opposite trade and the
# one to deploy: nothing here can be edited at runtime, there is no dev server,
# no source map server, no HMR socket, and no Node process to reach.
#
# THE ONE THING THAT SURPRISES PEOPLE: `import.meta.env.VITE_*` is substituted at
# BUILD time, not read at runtime. Seven modules under apps/web/src read
# VITE_API_BASE_URL. Setting it in the container's environment does nothing —
# the value was already baked into the bundle. Pass it as a build arg:
#
#   docker build -f apps/web/Dockerfile \
#     --build-arg VITE_API_BASE_URL=https://aleph.example.com \
#     --build-arg VITE_COPILOT_RUNTIME_URL=https://aleph.example.com/api/copilotkit \
#     -t aleph-web:prod .
#
# Getting this wrong is silent in exactly the way this repo keeps getting bitten
# by: the image builds, the page loads, and every request goes to
# http://localhost:8000 — the compiled-in default — which on a user's machine is
# nothing at all.

# ---- stage 1: build --------------------------------------------------------
# Pinned by digest, not by tag, and by the SAME digest as Dockerfile.dev, so the
# image that ships and the image that is developed against resolve the same
# Node. `node:22-alpine` is a moving pointer: two builds a fortnight apart
# shipped different Node runtimes, different bundled npm and different Alpine
# packages with nothing in the diff to say so, and a CVE scan of "the image we
# build" named an image nobody could reproduce.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build

WORKDIR /app

# Keep this version in step with Dockerfile.dev and with `pnpm/action-setup` in
# .github/workflows/ci.yml: the image and CI must resolve the same lockfile with
# the same resolver, or "CI is green" stops being a statement about what ships.
RUN npm install --global pnpm@11.17.0

# The workspace root files come first, and they come together. `pnpm-workspace.yaml`
# is not optional context — it is what makes `apps/web` a workspace member, and it
# carries the `overrides:` that force patched transitive versions and the
# `allowBuilds` entry that keeps @scarf/scarf's telemetry postinstall blocked.
COPY pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json

# `--frozen-lockfile`: the container installs exactly what CI verified, and the
# build fails on lockfile drift rather than quietly re-resolving every caret
# range to whatever was published since.
RUN pnpm --dir /app/apps/web install --frozen-lockfile

COPY apps/web ./apps/web

# Baked into the bundle. Defaults match the compose stack so a bare
# `docker build` produces something that works against a local stack; a real
# deployment MUST pass its own.
ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_COPILOT_RUNTIME_URL=http://localhost:4000/api/copilotkit
ARG VITE_AUTH_MODE=local
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_COPILOT_RUNTIME_URL=${VITE_COPILOT_RUNTIME_URL} \
    VITE_AUTH_MODE=${VITE_AUTH_MODE}

# `pnpm run build` is `tsc --noEmit && vite build`, so a type error fails the
# IMAGE. A production bundle that typechecks nowhere is how a broken build
# reaches a browser.
RUN pnpm --dir /app/apps/web run build

# ---- stage 2: serve --------------------------------------------------------
# Resolved 2026-08-21 with `docker buildx imagetools inspect nginx:1.29-alpine`
# — the multi-arch INDEX digest, so it resolves on linux/amd64 and linux/arm64
# alike; a per-platform manifest digest would break every non-amd64 build.
FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

# The whole config, written here rather than COPY'd, so this image is one file
# with nothing to drift against.
#
# Every writable path is moved to /tmp because the server runs as uid 101 and
# cannot write /var/cache/nginx or /var/run. nginx does not fail loudly about
# this: it starts, serves, and then 500s the first request that needs a temp
# file.
RUN cat > /etc/nginx/nginx.conf <<'NGINX'
worker_processes  auto;
pid               /tmp/nginx.pid;
error_log         /dev/stderr warn;

events { worker_connections 1024; }

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    access_log    /dev/stdout;

    client_body_temp_path /tmp/nginx-client-body;
    proxy_temp_path       /tmp/nginx-proxy;
    fastcgi_temp_path     /tmp/nginx-fastcgi;
    uwsgi_temp_path       /tmp/nginx-uwsgi;
    scgi_temp_path        /tmp/nginx-scgi;

    sendfile      on;
    tcp_nopush    on;
    server_tokens off;

    gzip          on;
    gzip_types    text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    server {
        listen      5173;
        root        /usr/share/nginx/html;
        index       index.html;

        # Vite emits content-hashed filenames under /assets, so those bytes can
        # never change meaning and are safe to cache forever.
        location /assets/ {
            expires    1y;
            add_header Cache-Control "public, immutable";
            try_files  $uri =404;
        }

        # index.html is the ONE file whose name never changes, so it is the one
        # file that must never be cached. A cached index.html points at the
        # previous deploy's hashed bundles, which the new image no longer has:
        # the browser gets a blank page and a 404 for a script, and a hard
        # reload does not fix it.
        location = /index.html {
            add_header Cache-Control "no-store";
        }

        # This is a single-page app: every route below / is resolved by the
        # client router, so an unknown path must serve index.html rather than
        # 404. Without this, a page reload anywhere except / is a 404 — which
        # looks like the deploy failed and is the single most common way an SPA
        # behind a static server is misconfigured.
        location / {
            try_files $uri $uri/ /index.html;
        }

        # Container liveness. Deliberately not the API's /readyz: this image
        # serves static files and knows nothing about whether the API is up.
        location = /healthz {
            access_log off;
            default_type text/plain;
            return 200 "ok\n";
        }
    }
}
NGINX

COPY --from=build /app/apps/web/dist /usr/share/nginx/html

# Non-root. uid 101 is the `nginx` user the alpine image already creates; the
# config above moved every writable path it needs into /tmp.
USER 101

EXPOSE 5173

# Foreground, because a container's process 1 must be the server itself — a
# daemonised nginx exits immediately and the container is reported as having
# completed successfully.
CMD ["nginx", "-g", "daemon off;"]
