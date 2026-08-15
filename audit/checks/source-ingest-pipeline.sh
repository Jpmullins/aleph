#!/usr/bin/env bash
# claim: source-ingest-pipeline (dataflow, slow) — upload -> normalize -> chunk+embed -> wiki-ingest
source "$(dirname "$0")/lib.sh"
TIMEOUT="${INGEST_TIMEOUT:-180}"

docker ps --format '{{.Names}}' 2>/dev/null | grep -q workers || skip "workers container not running"

pid=$(create_project "ingest probe" "Chain-of-thought prompting in large language models.")
trap 'delete_project "$pid"' EXIT
[ -n "$pid" ] && [ "$pid" != "null" ] || fail "could not create project"

read -r -d '' DOC <<'EOF'
# Chain-of-Thought Prompting

Wei et al. (2022) showed that prompting LLMs to emit intermediate reasoning steps
("chain of thought") substantially improves multi-step reasoning. On GSM8K, CoT with
PaLM 540B reaches 56.9% vs 17.9% standard prompting. The effect emerges only at scale
(above ~62B parameters) and generalizes across arithmetic and commonsense tasks.
EOF

sid=$(curl -sS -H "$AUTH_HDR" -F "file=@-;filename=cot.md;type=text/markdown" \
  "$API/v1/projects/$pid/sources/upload" <<<"$DOC" | jq -r '.source_id // .id // empty')
[ -n "$sid" ] || fail "source upload did not return an id"

start=$(date +%s); pages=0
while :; do
  st=$(api GET "/v1/projects/$pid/sources/$sid" | jq -r '.status // empty')
  pages=$(api GET "/v1/projects/$pid/wiki/pages" | jq 'length')
  # Terminal for the full pipeline: wiki-ingest produced pages (or failed).
  [ "$st" = "wiki_done" ] && break
  [ "$st" = "wiki_failed" ] && break
  [ "${pages:-0}" -ge 1 ] && break
  now=$(date +%s); [ $((now - start)) -ge "$TIMEOUT" ] && break
  sleep 4
done
elapsed=$(( $(date +%s) - start ))

if [ "$st" = "wiki_failed" ]; then
  fail "pipeline ran (normalize+embed) but wiki-ingest failed (status=wiki_failed)"
fi
if [ "${pages:-0}" -ge 1 ] || [ "$st" = "wiki_done" ]; then
  pass "upload->normalize->embed->wiki-ingest completed in ${elapsed}s (source=$st, wiki pages=$pages)"
fi
# Reached embedding but no wiki output within the window.
case "$st" in
  indexed|normalized)
    fail "pipeline stalled at status='$st' with 0 wiki pages after ${TIMEOUT}s (wiki-ingest did not complete)" ;;
  *)
    fail "source stuck at status='$st' after ${TIMEOUT}s (pipeline did not complete)" ;;
esac
