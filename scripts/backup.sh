#!/usr/bin/env bash
#
# Take a backup of an Aleph database (and, if asked, the asset volume).
#
# A backup nobody has restored from is a file, not a backup. So this script's
# real contract is not "produce a dump" — it is "produce a dump AND the evidence
# needed to prove a restore of it is faithful". `scripts/restore.sh` reads that
# evidence back and refuses to call a restore successful without it.
#
# THE PART THAT IS EASY TO GET WRONG, AND WHY IT IS DONE THIS WAY
#
#   1. The manifest is taken INSIDE THE DUMP'S OWN SNAPSHOT.
#      pg_dump runs in a REPEATABLE READ transaction against a point-in-time
#      snapshot. Row counts queried from a separate connection a second later
#      are counts of a DIFFERENT database state — on a live stack, ingest and
#      the research loop are writing the whole time. Measured while writing
#      this: `document_chunks` moved by thousands of rows inside one minute.
#      A verifier built on those counts fails for the wrong reason, and a check
#      that cries wolf gets switched off. So the transaction here exports its
#      snapshot with pg_export_snapshot(), hands it to pg_dump via
#      `--snapshot=`, and computes the inventory in the SAME transaction. The
#      manifest and the dump describe one instant.
#
#   2. The inventory is a content digest, not just a row count.
#      Per table: exact count(*) AND md5 over the per-row md5s of the row's
#      full text form. Order-independent (the digests are sorted), so it does
#      not depend on physical row order — which a dump/restore does not
#      preserve. It covers every column, which is how the pgvector embeddings
#      get covered: `embedding::text` is part of the row's text form, so a
#      float that changed in the round trip changes the table digest. The
#      embeddings also get their OWN named entry, because "compare a vector"
#      should be legible in the manifest rather than implied by a table hash.
#
#   3. The digest is only reproducible if the output settings are pinned.
#      `t::text` renders timestamps, floats and bytea using session GUCs. A
#      restore verified under a different DateStyle, TimeZone,
#      extra_float_digits or bytea_output produces a different digest for
#      identical data. Those four are set here AND recorded in the manifest, so
#      restore.sh re-applies the values this backup used rather than whatever
#      its own session defaults to.
#
#   4. `--snapshot` requires the exporting session to stay open for the whole
#      dump. That is why pg_dump is invoked from inside psql with `\!` rather
#      than as a sibling process: one session, held open by the transaction it
#      is running.
#
# Usage:
#   scripts/backup.sh [--out DIR] [--database-url URL]
#                     [--assets-volume NAME | --no-assets] [--dry-run]
#   scripts/backup.sh --inventory-only [--database-url URL]
#
# `--inventory-only` prints the inventory JSON for a database and writes
# nothing. restore.sh uses it to inventory the RESTORED database with the exact
# same SQL as the backup used — the comparison is only meaningful if both sides
# are measured by one instrument.
#
# Exit codes: 0 backup written · 1 the backup failed · 2 it could not run here
# (no psql/pg_dump, unreachable database, client older than the server). 2 is
# distinct on purpose: "could not measure" must never look like "measured, fine".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT=""
DB_URL_ARG=""
ASSETS_VOLUME="aleph_assets"
WANT_ASSETS=1
DRY_RUN=0
INVENTORY_ONLY=0

die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
cant() { printf '\033[33mcannot run here:\033[0m %s\n' "$*" >&2; exit 2; }
say()  { printf '%s\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out)             OUT="${2:?--out needs a directory}"; shift 2 ;;
    --database-url)    DB_URL_ARG="${2:?--database-url needs a URL}"; shift 2 ;;
    --assets-volume)   ASSETS_VOLUME="${2:?--assets-volume needs a name}"; shift 2 ;;
    --no-assets)       WANT_ASSETS=0; shift ;;
    --dry-run)         DRY_RUN=1; shift ;;
    --inventory-only)  INVENTORY_ONLY=1; shift ;;
    -h|--help)         sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                 die "unknown argument: $1" ;;
  esac
done

# --- where is the database ---------------------------------------------------

# Order: explicit flag, then the env the rest of the repo uses, then the compose
# .env — an operator running this on the host has the password there and nowhere
# else, and a backup script that demands an env var they have never set is a
# backup script that does not get run.
resolve_db_url() {
  if [ -n "$DB_URL_ARG" ]; then printf '%s' "$DB_URL_ARG"; return; fi
  if [ -n "${ALEPH_BACKUP_DATABASE_URL:-}" ]; then printf '%s' "$ALEPH_BACKUP_DATABASE_URL"; return; fi
  if [ -n "${DATABASE_URL:-}" ]; then printf '%s' "$DATABASE_URL"; return; fi
  if [ -n "${ALEPH_DATABASE_URL:-}" ]; then printf '%s' "$ALEPH_DATABASE_URL"; return; fi
  local envf="$ROOT/deploy/compose/.env" pw port
  if [ -f "$envf" ]; then
    pw="$(grep -E '^POSTGRES_PASSWORD=' "$envf" | head -1 | cut -d= -f2-)"
    port="$(grep -E '^POSTGRES_PORT=' "$envf" | head -1 | cut -d= -f2-)"
    if [ -n "$pw" ]; then
      printf 'postgresql://aleph:%s@localhost:%s/aleph' "$pw" "${port:-5432}"
      return
    fi
  fi
  return 1
}

DB_URL="$(resolve_db_url)" || cant "no database URL — pass --database-url, or set DATABASE_URL, or fill deploy/compose/.env"

# psql/pg_dump speak libpq URLs. SQLAlchemy's `+asyncpg` dialect suffix is not
# one, and libpq's error for it ("invalid URI scheme") names neither the tool
# nor the fix.
libpq_url() { printf '%s' "$1" | sed -E 's#^([a-z]+)\+[a-z0-9]+://#\1://#'; }
redact()    { printf '%s' "$1" | sed -E 's#://([^:/@]+):[^@]*@#://\1:***@#'; }
PGURL="$(libpq_url "$DB_URL")"

# --- preflight ---------------------------------------------------------------

command -v psql    >/dev/null 2>&1 || cant "psql is not on PATH"
command -v pg_dump >/dev/null 2>&1 || cant "pg_dump is not on PATH"
command -v python3 >/dev/null 2>&1 || cant "python3 is not on PATH (used to assemble the manifest)"

SERVER_VERSION_NUM="$(psql "$PGURL" -X -tAc 'show server_version_num' 2>/dev/null)" \
  || cant "cannot reach $(redact "$PGURL")"
SERVER_MAJOR=$(( SERVER_VERSION_NUM / 10000 ))
CLIENT_MAJOR="$(pg_dump --version | sed -E 's/[^0-9]*([0-9]+).*/\1/')"

# pg_dump refuses to dump a server whose MAJOR version is newer than its own,
# and it refuses late — after the connection, with a message about "server
# version" that reads like a server problem. Catch it here, and say what to do:
# the stack ships a matching client inside the postgres container.
if [ "$CLIENT_MAJOR" -lt "$SERVER_MAJOR" ]; then
  cant "pg_dump is $CLIENT_MAJOR, the server is $SERVER_MAJOR — pg_dump refuses to dump a newer server.
  Use the client that ships in the stack instead:
    docker compose -f deploy/compose/docker-compose.yml exec -T postgres \\
      pg_dump -U aleph -d aleph --format=custom > db.dump
  (that path has no manifest, so restore.sh cannot verify it — prefer upgrading the client)"
fi

# --- the inventory query -----------------------------------------------------
#
# ONE definition, used for both sides of the comparison. restore.sh does not
# have a copy: it calls this script with --inventory-only. Two hand-kept copies
# of a comparison's two halves is how a check ends up comparing a thing to
# itself; CLAUDE.md rule 5 exists because three copies of the A2UI catalog
# disagreed and no test noticed.
#
# `query_to_xml` is how a single static statement gets an exact count and digest
# for every table without knowing the table list at parse time. `to_regclass`
# guards the named per-column checks so a schema without `document_chunks` still
# produces a manifest instead of a parse error.
read -r -d '' INVENTORY_SQL <<'SQL' || true
with tbl as (
  select c.relname as name,
    (xpath('/row/c/text()', query_to_xml(
       format('select count(*) as c from public.%I', c.relname), false, true, '')))[1]::text::bigint as n,
    (xpath('/row/d/text()', query_to_xml(
       format('select coalesce(md5(string_agg(h, '''' order by h)), ''empty'') as d
                 from (select md5(r::text) as h from public.%I r) s', c.relname),
       false, true, '')))[1]::text as digest
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'
)
select json_build_object(
  'aleph_backup_format', 1,
  'taken_at', now(),
  'database', current_database(),
  'server_version', current_setting('server_version'),
  'output_settings', json_build_object(
     'DateStyle', current_setting('DateStyle'),
     'TimeZone', current_setting('TimeZone'),
     'extra_float_digits', current_setting('extra_float_digits'),
     'bytea_output', current_setting('bytea_output')),
  'alembic_version', case when to_regclass('public.alembic_version') is null then null else
     (xpath('/row/v/text()', query_to_xml(
        'select version_num as v from public.alembic_version limit 1', false, true, '')))[1]::text end,
  'extensions', (select json_object_agg(extname, extversion) from pg_extension),
  'tables', (select json_object_agg(name, json_build_object('rows', n, 'digest', digest)) from tbl),
  'table_count', (select count(*) from tbl),
  'total_rows', (select coalesce(sum(n), 0) from tbl),
  -- The named embedding check. The table digest above already covers it, but a
  -- pgvector round trip is the one thing in this schema that fails silently and
  -- expensively, so it gets an entry an operator can read: how many vectors,
  -- how wide, one digest over every component of every vector, and the head of
  -- one real vector to eyeball.
  'embeddings', case when to_regclass('public.document_chunks') is null then null else
     json_build_object(
        'column', 'document_chunks.embedding',
        'rows', (xpath('/row/v/text()', query_to_xml(
           'select count(*) as v from public.document_chunks', false, true, '')))[1]::text::bigint,
        'embedded', (xpath('/row/v/text()', query_to_xml(
           'select count(*) as v from public.document_chunks where embedding is not null',
           false, true, '')))[1]::text::bigint,
        'dims', (xpath('/row/v/text()', query_to_xml(
           'select max(vector_dims(embedding)) as v from public.document_chunks', false, true, '')))[1]::text,
        'digest', (xpath('/row/v/text()', query_to_xml(
           'select coalesce(md5(string_agg(md5(id::text || '':'' || embedding::text), '''' order by id::text)), ''empty'') as v from public.document_chunks where embedding is not null',
           false, true, '')))[1]::text,
        'sample', (xpath('/row/v/text()', query_to_xml(
           'select coalesce((select id::text || '' '' || left(embedding::text, 96) from public.document_chunks where embedding is not null order by id::text limit 1), ''none'') as v',
           false, true, '')))[1]::text
     ) end,
  -- Triggers are post-data in a custom-format dump, so a full restore recreates
  -- them AFTER the COPY. That is what makes a full restore safe. It is also why
  -- their presence must be asserted rather than assumed: a data-only restore
  -- run with --disable-triggers that dies mid-way leaves them `tgenabled='D'`,
  -- which is a database that looks complete and has lost its append-only
  -- guarantee. `enabled` is recorded, not just the name.
  'triggers', (select json_agg(json_build_object(
       'name', tgname,
       'table', tgrelid::regclass::text,
       'enabled', tgenabled,
       'def_md5', md5(pg_get_triggerdef(oid)))
     order by tgrelid::regclass::text, tgname)
     from pg_trigger where not tgisinternal)
);
SQL

# Both callers run the inventory the same way: pinned output settings, no
# .psqlrc, unaligned tuples only, so stdout is exactly the JSON document.
PINS="set local DateStyle='ISO, MDY'; set local TimeZone='UTC';
      set local extra_float_digits=3; set local bytea_output='hex';"

if [ "$INVENTORY_ONLY" -eq 1 ]; then
  psql "$PGURL" -X -v ON_ERROR_STOP=1 -Atq <<SQL
begin isolation level repeatable read;
$PINS
$INVENTORY_SQL
commit;
SQL
  exit 0
fi

# --- plan --------------------------------------------------------------------

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
[ -n "$OUT" ] || OUT="$ROOT/data/backups/aleph-$STAMP"

DOCKER_OK=0
if [ "$WANT_ASSETS" -eq 1 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    say "note: docker is not on PATH — the asset volume will not be backed up"
  elif ! docker volume inspect "$ASSETS_VOLUME" >/dev/null 2>&1; then
    say "note: docker volume '$ASSETS_VOLUME' does not exist — assets will not be backed up"
  else
    DOCKER_OK=1
  fi
fi

bold "aleph backup"
say "  source     $(redact "$PGURL")  (postgres $SERVER_VERSION_NUM)"
say "  out        $OUT"
say "  assets     $([ $DOCKER_OK -eq 1 ] && echo "docker volume $ASSETS_VOLUME" || echo "skipped")"

if [ "$DRY_RUN" -eq 1 ]; then
  say ""
  say "  --dry-run: nothing written. It would run:"
  say "    pg_dump --format=custom --no-owner --no-acl --snapshot=<exported> -> $OUT/database.dump"
  say "    the inventory query, in the dump's own snapshot          -> $OUT/manifest.json"
  [ $DOCKER_OK -eq 1 ] && say "    tar of docker volume $ASSETS_VOLUME                      -> $OUT/assets.tar.gz"
  exit 0
fi

mkdir -p "$OUT"
DUMP="$OUT/database.dump"
DB_MANIFEST="$OUT/.manifest.db.json"
DUMP_LOG="$OUT/pg_dump.log"
DUMP_STATUS="$OUT/.dump.status"
rm -f "$DUMP" "$DB_MANIFEST" "$DUMP_LOG" "$DUMP_STATUS"

# --- dump + manifest, one snapshot ------------------------------------------
#
# `\!` inherits psql's environment, which inherits this shell's, so the values
# travel as env vars rather than through shell interpolation into SQL — a dump
# path or a password containing a quote cannot break the statement.
export ALEPH_BK_URL="$PGURL" ALEPH_BK_DUMP="$DUMP" ALEPH_BK_LOG="$DUMP_LOG" \
       ALEPH_BK_STATUS="$DUMP_STATUS"

bold "dumping"
# --no-owner/--no-acl: a dump that carries `ALTER TABLE ... OWNER TO aleph`
# cannot be restored into a cluster whose role is named anything else, and the
# failure is one error per object rather than one error. Ownership is a property
# of the destination, not of the data.
psql "$PGURL" -X -v ON_ERROR_STOP=1 -Atq > "$DB_MANIFEST" <<SQL
begin isolation level repeatable read;
$PINS
select pg_export_snapshot() as snap \\gset
\\setenv ALEPH_BK_SNAPSHOT :snap
\\! pg_dump "\$ALEPH_BK_URL" --format=custom --compress=6 --no-owner --no-acl --snapshot="\$ALEPH_BK_SNAPSHOT" --file="\$ALEPH_BK_DUMP" > "\$ALEPH_BK_LOG" 2>&1 && echo ok > "\$ALEPH_BK_STATUS" || echo fail > "\$ALEPH_BK_STATUS"
$INVENTORY_SQL
commit;
SQL

[ "$(cat "$DUMP_STATUS" 2>/dev/null || echo missing)" = "ok" ] \
  || die "pg_dump failed — see $DUMP_LOG"
rm -f "$DUMP_STATUS"
[ -s "$DUMP" ] || die "pg_dump produced no output at $DUMP"

# A dump that cannot be listed cannot be restored, and finding that out at
# restore time is finding it out too late.
pg_restore --list "$DUMP" > "$OUT/toc.txt" 2>>"$DUMP_LOG" \
  || die "the dump is not a readable pg_restore archive — see $DUMP_LOG"

# The `vector` extension must be in the archive, or the restore builds a schema
# with no `vector` type and every table holding an embedding is silently absent
# from the result. Assert it here, where the fix is cheap.
grep -qE '^[0-9]+; [0-9]+ [0-9]+ EXTENSION - vector' "$OUT/toc.txt" \
  || die "the dump's table of contents has no 'EXTENSION vector' entry — restore would produce a schema with no vector type"

# --- assets ------------------------------------------------------------------

ASSET_JSON='null'
if [ "$DOCKER_OK" -eq 1 ]; then
  bold "archiving assets"
  # Streamed to stdout rather than written through a bind mount: a container
  # writing into a mounted host directory leaves root-owned files behind, which
  # the next backup cannot overwrite.
  docker run --rm -v "$ASSETS_VOLUME":/src:ro alpine \
    tar -czf - -C /src . > "$OUT/assets.tar.gz"
  docker run --rm -v "$ASSETS_VOLUME":/src:ro alpine \
    sh -c 'cd /src && find . -type f | sort | xargs -r sha256sum' > "$OUT/assets.sha256"
  ASSET_COUNT="$(wc -l < "$OUT/assets.sha256" | tr -d ' ')"
  ASSET_JSON="$(python3 -c '
import hashlib, json, os, sys
d, vol, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
h = hashlib.sha256(open(os.path.join(d, "assets.sha256"), "rb").read()).hexdigest()
print(json.dumps({"volume": vol, "files": n, "listing_sha256": h,
                  "archive_bytes": os.path.getsize(os.path.join(d, "assets.tar.gz"))}))
' "$OUT" "$ASSETS_VOLUME" "$ASSET_COUNT")"
fi

# --- manifest ----------------------------------------------------------------

python3 - "$DB_MANIFEST" "$OUT/manifest.json" "$ASSET_JSON" "$DUMP" <<'PY'
import hashlib, json, os, sys

db_path, out_path, asset_json, dump_path = sys.argv[1:5]
with open(db_path) as fh:
    manifest = json.load(fh)

# The dump's own checksum. restore.sh compares it before restoring, so a dump
# truncated by a full disk is reported as a corrupt backup rather than as a
# restore that lost rows — two very different things to be told at 3am.
h = hashlib.sha256()
with open(dump_path, "rb") as fh:
    for block in iter(lambda: fh.read(1 << 20), b""):
        h.update(block)
manifest["dump"] = {
    "file": os.path.basename(dump_path),
    "bytes": os.path.getsize(dump_path),
    "sha256": h.hexdigest(),
    "format": "custom",
}
manifest["assets"] = json.loads(asset_json)
with open(out_path, "w") as fh:
    json.dump(manifest, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
rm -f "$DB_MANIFEST"

# --- report ------------------------------------------------------------------

python3 - "$OUT/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
emb = m.get("embeddings") or {}
print()
print("  schema        alembic %s" % m.get("alembic_version"))
print("  tables        %s, %s rows" % (m["table_count"], m["total_rows"]))
print("  embeddings    %s of %s chunks, %s dims, digest %s"
      % (emb.get("embedded"), emb.get("rows"), emb.get("dims"), (emb.get("digest") or "")[:12]))
print("  triggers      %s (%s immutability)"
      % (len(m["triggers"] or []),
         sum(1 for t in (m["triggers"] or []) if t["name"].endswith(("_no_update", "_no_delete")))))
a = m.get("assets")
print("  assets        %s" % ("%s files, %s bytes archived" % (a["files"], a["archive_bytes"]) if a else "not backed up"))
print("  dump          %s bytes" % m["dump"]["bytes"])
PY

say ""
bold "OK: backup written to $OUT"
say "  restore it with:  ./scripts/restore.sh $OUT --into <scratch-db-name>"
say "  a backup nobody has restored is a file. Run the restore."
