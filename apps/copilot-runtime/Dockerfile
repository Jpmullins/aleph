FROM node:22-alpine

WORKDIR /app

COPY apps/copilot-runtime/package.json ./package.json

# Standalone npm install (no workspace); npm runs esbuild's build script
# that tsx needs without pnpm 10's build-script gating.
RUN npm install

COPY apps/copilot-runtime/ ./

EXPOSE 4000

CMD ["npm", "start"]
