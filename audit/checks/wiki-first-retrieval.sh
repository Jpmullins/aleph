#!/usr/bin/env bash
# claim: wiki-first-retrieval (http)
#
# KNOWN-ANSWER PROBE. The previous version of this check asserted only that
# `composed_body_md` was at least 40 characters long for the query
# "What is this project about?" — and it passed for the wrong reason. That
# query shares no tokens with any title or summary, so it misses FTS entirely,
# falls through to the `list_pages` fallback, and composes prose from arbitrary
# recent pages. The mechanism that masks the defect is what made the check green,
# and that is how a scorecard read 31/32 over a central hypothesis that was
# never true.
#
# This version asks the only question that matters: **can the system retrieve a
# page using words that appear in that page's body?** It lifts a distinctive
# phrase out of a real page body, queries for it, and requires that same page
# back. No ingest, no LLM authoring, no fixtures — it probes live data.
#
# It is expected to FAIL against the current wiki: `wiki_index.index_tsv` is
# built from title + summary + aliases and never the body, so a body phrase
# cannot match. That is the point. A check that cannot fail is not a check.
source "$(dirname "$0")/lib.sh"

command -v jq >/dev/null || skip "jq not available"

pid=$(pick_project_with wiki/pages) || skip "no project has wiki pages to retrieve over"

# Choose the substantive page with the longest body — most likely to carry a
# phrase that is genuinely body-only.
page_id=$(api GET "/v1/projects/$pid/wiki/pages" \
  | jq -r '[.[] | select(.is_stub != true and .current_revision_id != null)][0].id // empty')
[ -n "$page_id" ] || skip "project $pid has no non-stub page with a committed revision"

detail=$(api GET "/v1/projects/$pid/wiki/pages/$page_id")
title=$(echo "$detail" | jq -r '.title // ""')
summary=$(echo "$detail" | jq -r '.revision.summary // ""')
body=$(echo "$detail" | jq -r '.revision.body_md // ""')
[ ${#body} -ge 200 ] || skip "page $page_id body is only ${#body} chars — too short to source a body-only phrase"

# Build a probe phrase from the body: take content words that appear in the body
# but in NEITHER the title NOR the summary, so a title+summary index cannot
# match it. Skip markdown syntax, wikilinks, citation markers and short words.
probe=$(python3 - "$body" "$title" "$summary" <<'PY'
import re, sys
body, title, summary = sys.argv[1], sys.argv[2], sys.argv[3]
indexed = set(re.findall(r"[a-z]{4,}", (title + " " + summary).lower()))
body_txt = re.sub(r"\[\[[^\]]*\]\]|\[c\d+\]|```.*?```|`[^`]*`|https?://\S+", " ", body, flags=re.S)
seen, out = set(), []
for w in re.findall(r"[A-Za-z][A-Za-z-]{3,}", body_txt):
    lw = w.lower()
    if lw in indexed or lw in seen:
        continue
    seen.add(lw)
    out.append(w)
    if len(out) == 6:
        break
print(" ".join(out))
PY
)

[ -n "$probe" ] || skip "could not derive a body-only phrase from page $page_id"

resp=$(api POST "/v1/projects/$pid/retrieval/debug" \
  "$(jq -nc --arg q "$probe" '{query:$q, top_k:8}')")

selected=$(echo "$resp" | jq -r '[.selected_pages[]?.page_id] | join(",")')
reason=$(echo "$resp" | jq -r '.page_selection_reason // ""')
coverage=$(echo "$resp" | jq -r '.coverage_judgment // ""')

# 1. The retrieval must ground at all — the honest-miss path must not be taken.
case "$reason" in
  *"no confident match"*)
    fail "retrieval did not ground: '$reason' (probe='$probe' from page $page_id)" ;;
esac

# 2. The page the phrase came from must be among the selected pages.
echo "$selected" | tr ',' '\n' | grep -qx "$page_id" || fail \
  "body-phrase probe failed to retrieve its own source page.
     project : $pid
     page    : $page_id ($title)
     probe   : '$probe'   <- these words appear in the page BODY, not its title/summary
     selected: ${selected:-<none>}
     reason  : ${reason:-<none>}
   The retrieval index covers title+summary+aliases and never body_md, so a
   phrase that exists only in the body cannot match. See docs/decisions.md D1."

# 3. Having grounded, the composer must have produced a real answer.
len=$(echo "$resp" | jq -r '.composed_body_md // ""' | wc -c)
[ "$len" -ge 40 ] || fail "grounded on page $page_id but composed only $len chars"

pass "body-phrase probe '$probe' retrieved its source page $page_id (coverage=$coverage)"
