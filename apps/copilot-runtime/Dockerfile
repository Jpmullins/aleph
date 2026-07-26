FROM node:22-alpine

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

CMD ["npm", "start"]
