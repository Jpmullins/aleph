# Pinned by digest, not by tag. `node:22-alpine` is a moving pointer: two
# builds a fortnight apart shipped different Node runtimes, different bundled npm
# and different Alpine packages with nothing in the diff to say so, and a CVE scan of
# "the image we build" named an image nobody could reproduce. The tag is kept in
# the line so a human can still read which base this is; the digest is what
# docker resolves. `.github/dependabot.yml`'s docker ecosystem is what moves it
# forward, and `.github/workflows/security.yml` scans exactly this digest.
# Resolved 2026-08-21 with `docker buildx imagetools inspect node:22-alpine`
# — the multi-arch INDEX digest, so it resolves on linux/amd64 and linux/arm64
# alike; a per-platform manifest digest would break every non-amd64 build.
FROM node:26-alpine@sha256:2d984a15c9b54fd0aeb608b8e0d0d83529eb34d2966db27a1fb4f1edc3d298a3

WORKDIR /app

COPY apps/copilot-runtime/package.json ./package.json
COPY apps/copilot-runtime/package-lock.json ./package-lock.json

# `npm ci` — NOT `npm install`. The manifest previously floated: it declared
# `@copilotkit/runtime ^1.58` and resolved 1.63.2, and `server.ts`'s critical
# `a2ui.defaultCatalogId` option does not exist in the middleware bundled with
# 1.58. The deployed container only worked because of that accidental float.
# The lockfile is now the source of truth; `npm ci` fails loudly on drift.
#
# Standalone install (no workspace); npm runs esbuild's build script that tsx
# needs without pnpm 10's build-script gating.
RUN npm ci

COPY apps/copilot-runtime/ ./

EXPOSE 4000

# The node images ship an unprivileged `node` user (uid 1000). Nothing here
# writes to disk at runtime, so there is no volume ownership to arrange — this
# image was root purely because nobody said otherwise.
USER node

CMD ["npm", "start"]
