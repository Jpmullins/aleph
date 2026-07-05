#!/usr/bin/env bash
#
# WP-7 — docs-drift guard (Final State F6 acceptance grep, enforced).
#
# The live docs describe only the system that exists: AIQ is gone (native
# research loop) and MinIO is opt-in (fs is the default asset backend). So the
# words "aiq" and "minio" may appear ONLY in historical material:
#   * anything under docs/archive/        (the preserved pre-WP record)
#   * docs/implementation-log.md           (append-only shipped log)
#
# Any hit elsewhere in docs/, CLAUDE.md, or README.md is drift and fails CI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# All hits, case-insensitive, with file paths (-I skips binary).
hits="$(grep -rniEI "aiq|minio" docs CLAUDE.md README.md 2>/dev/null || true)"

# Drop the allowed historical locations.
offending="$(printf '%s\n' "$hits" \
  | grep -v '^docs/archive/' \
  | grep -v '^docs/implementation-log.md:' \
  | sed '/^$/d' || true)"

if [ -n "$offending" ]; then
  echo "✗ docs drift — 'aiq'/'minio' outside docs/archive/ + docs/implementation-log.md:"
  echo "$offending" | sed 's/^/    /'
  echo ""
  echo "The live docs must describe only the current system (native research, fs-default"
  echo "storage). Move stale mentions to docs/archive/ or remove them."
  exit 1
fi

echo "✓ docs drift: 'aiq'/'minio' appear only under docs/archive/ + docs/implementation-log.md"
