#!/usr/bin/env bash
#
# Restore an Aleph backup — and prove the restore is faithful before saying so.
#
# The criterion this script exists to satisfy is not "pg_restore exited 0".
# Measured against pg_restore 17.10 rather than assumed: restoring this dump
# into a database holding one conflicting table ignored 6 errors, exited 1 —
# and left behind a database where `wiki_pages` had 0 rows while
# `document_chunks` had 3,724. So the default exit code does say "something went
# wrong", and says nothing whatever about WHAT, while leaving a half-restored
# database sitting there looking complete. `--exit-on-error --single-transaction`
# turns that into "no database rather than a wrong one", and the row-count and
# content-digest comparison below is what says which parts arrived.
# Three specific traps are handled by name:
#
#   1. pgvector. The dump carries `CREATE EXTENSION vector`, but the extension
#      has to be AVAILABLE in the target cluster (a different postgres image
#      does not have it) and the restoring role has to be allowed to create it.
#      If that one statement fails, the `vector` TYPE does not exist, every
#      CREATE TABLE with an embedding column fails after it, and you get a
#      schema with no `document_chunks`. Checked before the restore starts, with
#      a fallback for the hardened case where a DBA installed the extension and
#      the app role may not create one.
#
#   2. The append-only triggers. `action_ledger_events` and `wiki_revisions`
#      are append-only by TRIGGER, and so are three more tables the plan does
#      not name (`interactive_card_versions`, `hypothesis_versions`,
#      `artifact_versions`) — ten immutability triggers in all. In a
#      custom-format dump they are POST-DATA, so a full restore creates them
#      after the COPY and the data loads fine. But that is a property of THIS
#      restore path, not of restores in general: `pg_restore --data-only
#      --disable-triggers` interrupted part way leaves them `tgenabled='D'` —
#      a database that looks complete and has quietly lost the guarantee the
#      ledger's evidentiary value rests on. So this script does not trust the
#      path: after restoring it asserts each trigger exists, is enabled, has the
#      same definition, and — where the table has a row to try it on — ACTUALLY
#      REFUSES an UPDATE and a DELETE. A row in pg_trigger is not a guarantee;
#      a rejected UPDATE is.
#
#   3. Silent drift. Row counts alone cannot tell a faithful restore from one
#      that lost a column's contents, and a float that changed in the round trip
#      shows up nowhere in a count. Every table is compared by exact count AND
#      content digest, and the embeddings are compared as vectors — a digest
#      over every component of every one of them, not a count of the rows that
#      have one.
#
# Usage:
#   scripts/restore.sh <backup-dir> --into <dbname|url> [--drop-existing]
#                      [--assets-volume NAME] [--dry-run] [--force]
#   scripts/restore.sh <backup-dir> --into <dbname|url> --verify-only
#
# Exit codes: 0 restored and verified · 1 the restore or the verification failed
# · 2 it could not run here. As in backup.sh, 2 is distinct because "could not
# measure" must never be reported the same way as "measured, and it was fine".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BACKUP=""
INTO=""
CLUSTER_URL_ARG=""
ASSETS_VOLUME=""
DROP_EXISTING=0
DRY_RUN=0
VERIFY_ONLY=0
FORCE=0

die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
cant() { printf '\033[33mcannot run here:\033[0m %s\n' "$*" >&2; exit 2; }
say()  { printf '%s\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m%-6s\033[0m %s\n' "ok" "$*"; }
bad()  { printf '  \033[31m%-6s\033[0m %s\n' "FAIL" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --into)          INTO="${2:?--into needs a database name or URL}"; shift 2 ;;
    --cluster-url)   CLUSTER_URL_ARG="${2:?--cluster-url needs a URL}"; shift 2 ;;
    --assets-volume) ASSETS_VOLUME="${2:?--assets-volume needs a name}"; shift 2 ;;
    --drop-existing) DROP_EXISTING=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --verify-only)   VERIFY_ONLY=1; shift ;;
    --force)         FORCE=1; shift ;;
    -h|--help)       sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)              die "unknown argument: $1" ;;
    *)               [ -z "$BACKUP" ] || die "only one backup directory"; BACKUP="$1"; shift ;;
  esac
done

[ -n "$BACKUP" ] || die "usage: scripts/restore.sh <backup-dir> --into <dbname|url>"
[ -n "$INTO" ]   || die "--into is required: name the database to restore INTO. This script never writes to the database it was told to read the backup from."

# A bare .dump has no manifest, so nothing can be verified against it. Say that
# rather than restoring it and reporting success — an unverified restore
# reported as verified is the failure mode this whole workstream is about.
[ -d "$BACKUP" ] || die "$BACKUP is not a backup directory. A bare .dump file carries no manifest, so a restore from it cannot be verified; re-take the backup with scripts/backup.sh."
MANIFEST="$BACKUP/manifest.json"
DUMP="$BACKUP/database.dump"
[ -f "$MANIFEST" ] || die "$MANIFEST is missing — this directory is not an aleph backup"
[ -f "$DUMP" ]     || die "$DUMP is missing"

command -v psql       >/dev/null 2>&1 || cant "psql is not on PATH"
command -v pg_restore >/dev/null 2>&1 || cant "pg_restore is not on PATH"
command -v python3    >/dev/null 2>&1 || cant "python3 is not on PATH"

# --- where is the cluster ----------------------------------------------------

resolve_cluster_url() {
  if [ -n "$CLUSTER_URL_ARG" ]; then printf '%s' "$CLUSTER_URL_ARG"; return; fi
  if [ -n "${ALEPH_BACKUP_DATABASE_URL:-}" ]; then printf '%s' "$ALEPH_BACKUP_DATABASE_URL"; return; fi
  if [ -n "${DATABASE_URL:-}" ]; then printf '%s' "$DATABASE_URL"; return; fi
  if [ -n "${ALEPH_DATABASE_URL:-}" ]; then printf '%s' "$ALEPH_DATABASE_URL"; return; fi
  local envf="$ROOT/deploy/compose/.env" pw port
  if [ -f "$envf" ]; then
    pw="$(grep -E '^POSTGRES_PASSWORD=' "$envf" | head -1 | cut -d= -f2-)"
    port="$(grep -E '^POSTGRES_PORT=' "$envf" | head -1 | cut -d= -f2-)"
    [ -n "$pw" ] && { printf 'postgresql://aleph:%s@localhost:%s/aleph' "$pw" "${port:-5432}"; return; }
  fi
  return 1
}

libpq_url() { printf '%s' "$1" | sed -E 's#^([a-z]+)\+[a-z0-9]+://#\1://#'; }
redact()    { printf '%s' "$1" | sed -E 's#://([^:/@]+):[^@]*@#://\1:***@#'; }

CLUSTER_URL="$(resolve_cluster_url)" || cant "no cluster URL — pass --cluster-url, or set DATABASE_URL, or fill deploy/compose/.env"
ADMIN_URL="$(libpq_url "$CLUSTER_URL")"

# `--into` is either a bare database name (swap it onto the cluster URL, which
# is how a drill names a scratch database) or a full URL (a different cluster
# entirely, which is what a real disaster recovery looks like).
case "$INTO" in
  *://*) TARGET_URL="$(libpq_url "$INTO")" ;;
  *)     TARGET_URL="$(python3 - "$ADMIN_URL" "$INTO" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
url, name = sys.argv[1], sys.argv[2]
print(urlunsplit(urlsplit(url)._replace(path=f"/{name}")))
PY
)" ;;
esac
TARGET_DB="$(python3 -c 'import sys;from urllib.parse import urlsplit;print(urlsplit(sys.argv[1]).path.lstrip("/"))' "$TARGET_URL")"
SOURCE_DB="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["database"])' "$MANIFEST")"
ADMIN_DB="$(python3 -c 'import sys;from urllib.parse import urlsplit;print(urlsplit(sys.argv[1]).path.lstrip("/"))' "$ADMIN_URL")"

# The one guard that matters more than any verification below: do not restore
# over the database you are backing up. A drill that overwrites production is
# not a drill.
if [ "$TARGET_DB" = "$ADMIN_DB" ] && [ "$FORCE" -eq 0 ]; then
  die "refusing: --into names '$TARGET_DB', which is the database this script is connected to. Pass --force only if you really mean to overwrite it."
fi

bold "aleph restore"
say "  backup     $BACKUP"
say "  dump       $(python3 -c 'import json,sys;m=json.load(open(sys.argv[1]));print("%s bytes, taken %s, from database %s (alembic %s)"%(m["dump"]["bytes"],m["taken_at"],m["database"],m["alembic_version"]))' "$MANIFEST")"
say "  target     $(redact "$TARGET_URL")"
say "  source db  $SOURCE_DB"

if [ "$DRY_RUN" -eq 1 ]; then
  say ""
  say "  --dry-run: nothing written. It would:"
  say "    verify $DUMP against the manifest's sha256"
  say "    require the 'vector' extension in the target cluster"
  [ "$DROP_EXISTING" -eq 1 ] && say "    DROP DATABASE \"$TARGET_DB\" WITH (FORCE)"
  say "    CREATE DATABASE \"$TARGET_DB\""
  say "    pg_restore --exit-on-error --no-owner --no-acl  ($(pg_restore --list "$DUMP" | grep -c ';' ) archive entries)"
  say "    inventory the result and compare every table's row count and content digest"
  say "    assert every immutability trigger is present, enabled, and still refuses an UPDATE"
  [ -n "$ASSETS_VOLUME" ] && say "    unpack assets.tar.gz into docker volume $ASSETS_VOLUME"
  exit 0
fi

# --- the dump itself ---------------------------------------------------------

if [ "$VERIFY_ONLY" -eq 0 ]; then
  bold "checking the archive"
  python3 - "$MANIFEST" "$DUMP" <<'PY' || exit 1
import hashlib, json, os, sys
m = json.load(open(sys.argv[1]))
want, path = m["dump"], sys.argv[2]
size = os.path.getsize(path)
if size != want["bytes"]:
    print(f"  \033[31mFAIL\033[0m   dump is {size} bytes, the manifest says {want['bytes']} — the backup is truncated, not the restore's fault")
    sys.exit(1)
h = hashlib.sha256()
with open(path, "rb") as fh:
    for b in iter(lambda: fh.read(1 << 20), b""):
        h.update(b)
if h.hexdigest() != want["sha256"]:
    print(f"  \033[31mFAIL\033[0m   dump sha256 {h.hexdigest()[:16]} != manifest {want['sha256'][:16]} — the backup is corrupt")
    sys.exit(1)
print(f"  \033[32mok\033[0m     dump matches its manifest sha256 ({size} bytes)")
PY

  # --- pgvector, before anything is created ---------------------------------
  psql "$ADMIN_URL" -X -tAc "select 1 from pg_available_extensions where name='vector'" | grep -q 1 \
    || cant "the 'vector' extension is not available on this cluster.
  Nothing here can restore an Aleph dump: the schema declares vector columns, so
  CREATE EXTENSION fails, the vector TYPE never exists, and every table holding an
  embedding is silently absent from the result.
  The stack's postgres image is pgvector/pgvector:pg17, which has it."
  ok "the 'vector' extension is available on this cluster"

  # --- create the target ------------------------------------------------------
  EXISTS="$(psql "$ADMIN_URL" -X -tAc "select 1 from pg_database where datname = '$TARGET_DB'")"
  if [ -n "$EXISTS" ]; then
    if [ "$DROP_EXISTING" -eq 1 ]; then
      say "  dropping existing database \"$TARGET_DB\""
      psql "$ADMIN_URL" -X -q -c "DROP DATABASE \"$TARGET_DB\" WITH (FORCE)"
    else
      die "database \"$TARGET_DB\" already exists. Pass --drop-existing to replace it. Restoring into a database that already has rows produces a mixture of two databases and reports success."
    fi
  fi
  psql "$ADMIN_URL" -X -q -c "CREATE DATABASE \"$TARGET_DB\""
  ok "created database \"$TARGET_DB\""

  # Pre-create the extension. The dump's own statement is
  # `CREATE EXTENSION IF NOT EXISTS`, so this is a no-op when the restoring role
  # may create extensions — and it is the whole restore when it may not, which
  # is the normal hardened arrangement: a DBA installs extensions, the app role
  # owns nothing else.
  psql "$TARGET_URL" -X -q -c "CREATE EXTENSION IF NOT EXISTS vector" \
    || die "could not create the 'vector' extension in $TARGET_DB — the restoring role needs it, or a superuser must create it first"

  # --- restore ---------------------------------------------------------------
  bold "restoring"
  RESTORE_LOG="$BACKUP/pg_restore.log"
  # --exit-on-error stops at the first failure instead of ignoring it and
  # carrying on; --single-transaction additionally means a failure leaves NO
  # half-restored database behind. Without them, a restore that lost one table's
  # entire contents leaves a database that is indistinguishable at a glance from
  # a good one — measured: 6 ignored errors, `wiki_pages` at 0 rows,
  # `document_chunks` at 3,724, and one line of warning to notice it by.
  if ! pg_restore --dbname "$TARGET_URL" --no-owner --no-acl --exit-on-error \
        --single-transaction "$DUMP" > "$RESTORE_LOG" 2>&1; then
    say ""
    tail -20 "$RESTORE_LOG" >&2
    die "pg_restore failed — full log at $RESTORE_LOG"
  fi
  ok "pg_restore --exit-on-error completed"
fi

# --- verify ------------------------------------------------------------------

bold "verifying the restored database"

# Measured with the SAME instrument that produced the manifest. backup.sh owns
# the inventory SQL; this script does not keep a second copy of it, because two
# hand-kept halves of one comparison drift and the comparison then proves
# nothing.
ACTUAL="$(mktemp)"
trap 'rm -f "$ACTUAL"' EXIT INT TERM
"$ROOT/scripts/backup.sh" --inventory-only --database-url "$TARGET_URL" > "$ACTUAL" \
  || die "could not inventory the restored database"

python3 - "$MANIFEST" "$ACTUAL" <<'PY' || VERIFY_FAILED=1
import json, sys

want = json.load(open(sys.argv[1]))
got = json.load(open(sys.argv[2]))
fails: list[str] = []
oks: list[str] = []

def ok(msg): oks.append(msg)
def bad(msg): fails.append(msg)

# The digests are only comparable if both sides rendered rows with the same
# session settings. A mismatch here is not a data problem and must not be
# reported as one — it means the measurement is invalid.
if want["output_settings"] != got["output_settings"]:
    bad("output settings differ between backup and verify (%r vs %r) — the content "
        "digests are not comparable, so this restore is UNVERIFIED rather than bad"
        % (want["output_settings"], got["output_settings"]))
else:
    ok("digest settings match (%s)" % ", ".join(f"{k}={v}" for k, v in sorted(want["output_settings"].items())))

if want["alembic_version"] != got["alembic_version"]:
    bad("schema version: backup %s, restored %s" % (want["alembic_version"], got["alembic_version"]))
else:
    ok("schema version %s" % want["alembic_version"])

# Extension VERSIONS too: a dump restored onto an older pgvector can change how
# a vector renders, and then the embedding digest is wrong for a reason that has
# nothing to do with the backup.
for name, ver in sorted((want["extensions"] or {}).items()):
    have = (got["extensions"] or {}).get(name)
    if have is None:
        bad("extension %s (%s) is missing from the restored database" % (name, ver))
    elif have != ver:
        bad("extension %s is %s in the restore, %s in the backup" % (name, have, ver))
if not any(f.startswith("extension ") for f in fails):
    ok("extensions match (%s)" % ", ".join(f"{k} {v}" for k, v in sorted((want["extensions"] or {}).items())))

wt, gt = want["tables"], got["tables"]
missing = sorted(set(wt) - set(gt))
extra = sorted(set(gt) - set(wt))
for t in missing:
    bad("table %s is missing from the restore (had %s rows)" % (t, wt[t]["rows"]))
for t in extra:
    bad("table %s exists in the restore and not in the backup (%s rows)" % (t, gt[t]["rows"]))

rowdiff, digestdiff = [], []
for t in sorted(set(wt) & set(gt)):
    if wt[t]["rows"] != gt[t]["rows"]:
        rowdiff.append("%s: %s -> %s" % (t, wt[t]["rows"], gt[t]["rows"]))
    elif wt[t]["digest"] != gt[t]["digest"]:
        digestdiff.append("%s: %s rows, content digest %s -> %s"
                          % (t, wt[t]["rows"], wt[t]["digest"][:12], gt[t]["digest"][:12]))
for d in rowdiff:
    bad("row count changed — " + d)
for d in digestdiff:
    bad("same row count, DIFFERENT CONTENT — " + d)
if not rowdiff and not digestdiff and not missing and not extra:
    ok("%d tables, %d rows: every count and every content digest identical"
       % (want["table_count"], want["total_rows"]))

we, ge = want.get("embeddings"), got.get("embeddings")
if we is None and ge is None:
    ok("no embedding column in this schema")
elif we is None or ge is None:
    bad("the embedding column exists on one side only")
else:
    for k in ("rows", "embedded", "dims", "digest"):
        if we[k] != ge[k]:
            bad("embeddings %s: %s -> %s" % (k, we[k], ge[k]))
    if we["sample"] != ge["sample"]:
        bad("the sampled vector changed:\n         %s\n         %s" % (we["sample"], ge["sample"]))
    if not any(f.startswith("embeddings ") or f.startswith("the sampled") for f in fails):
        ok("%s of %s pgvector embeddings, %s dims, every component identical (digest %s)"
           % (we["embedded"], we["rows"], we["dims"], we["digest"][:12]))
        ok("sampled vector round-tripped verbatim: %s…" % we["sample"][:78])

wtr = {(t["table"], t["name"]): t for t in (want["triggers"] or [])}
gtr = {(t["table"], t["name"]): t for t in (got["triggers"] or [])}
for key, t in sorted(wtr.items()):
    g = gtr.get(key)
    if g is None:
        bad("trigger %s on %s is GONE after the restore" % (key[1], key[0]))
    elif g["enabled"] != t["enabled"]:
        bad("trigger %s on %s is tgenabled=%s, was %s — a disabled trigger is a trigger that is not there"
            % (key[1], key[0], g["enabled"], t["enabled"]))
    elif g["def_md5"] != t["def_md5"]:
        bad("trigger %s on %s has a different definition after the restore" % (key[1], key[0]))
imm = [k for k in wtr if k[1].endswith(("_no_update", "_no_delete"))]
if not any(f.startswith("trigger ") for f in fails):
    ok("%d triggers restored and enabled, %d of them append-only guards on %s"
       % (len(wtr), len(imm), ", ".join(sorted({k[0] for k in imm}))))

for m in oks:
    print("  \033[32mok\033[0m     " + m)
for m in fails:
    print("  \033[31mFAIL\033[0m   " + m)
sys.exit(1 if fails else 0)
PY
VERIFY_FAILED="${VERIFY_FAILED:-0}"

# --- the triggers, exercised rather than counted -----------------------------
#
# pg_trigger says a trigger exists. It does not say the trigger works. This
# fires a real UPDATE and a real DELETE at every append-only table and requires
# both to be refused, inside a transaction that is rolled back either way — so
# even a database that HAS lost the guarantee is not modified by the check that
# finds out.
TRIGGER_OUT="$(psql "$TARGET_URL" -X -v ON_ERROR_STOP=1 -Atq <<'SQL' 2>&1 || true
begin;
do $chk$
declare
  r record; col text; tbl text; n bigint; refused boolean; tested int := 0; skipped int := 0;
begin
  for r in
    select t.tgname, t.tgrelid::regclass::text as relname, t.tgenabled,
           case when t.tgname like '%\_no\_update' then 'update' else 'delete' end as kind
    from pg_trigger t
    where not t.tgisinternal and (t.tgname like '%\_no\_update' or t.tgname like '%\_no\_delete')
    order by 2, 1
  loop
    if r.tgenabled <> 'O' then
      raise exception 'IMMUTABILITY LOST: % on % is tgenabled=% (disabled triggers are how an interrupted --disable-triggers restore silently drops the guarantee)',
        r.tgname, r.relname, r.tgenabled;
    end if;
    tbl := r.relname;
    execute format('select count(*) from %s', tbl) into n;
    if n = 0 then
      skipped := skipped + 1;
      continue;
    end if;
    refused := false;
    if r.kind = 'update' then
      select a.attname into col from pg_attribute a
        where a.attrelid = tbl::regclass and a.attnum > 0 and not a.attisdropped
        order by a.attnum limit 1;
      begin
        execute format('update %s set %I = %I where ctid = (select ctid from %s limit 1)', tbl, col, col, tbl);
      exception when others then refused := true;
      end;
    else
      begin
        execute format('delete from %s where ctid = (select ctid from %s limit 1)', tbl, tbl);
      exception when others then refused := true;
      end;
    end if;
    if not refused then
      raise exception 'IMMUTABILITY LOST: % accepted a real % after the restore (trigger % exists and did nothing)',
        tbl, r.kind, r.tgname;
    end if;
    tested := tested + 1;
  end loop;
  raise notice 'EXERCISED % refused, % skipped (empty table)', tested, skipped;
end
$chk$;
rollback;
SQL
)"
if printf '%s' "$TRIGGER_OUT" | grep -q 'IMMUTABILITY LOST'; then
  bad "$(printf '%s' "$TRIGGER_OUT" | grep 'IMMUTABILITY LOST' | head -3)"
  VERIFY_FAILED=1
elif printf '%s' "$TRIGGER_OUT" | grep -q 'EXERCISED'; then
  ok "append-only enforced for real: $(printf '%s' "$TRIGGER_OUT" | sed -n 's/.*EXERCISED //p') — a real UPDATE and a real DELETE were rejected, in a rolled-back transaction"
else
  bad "the append-only check did not run: $TRIGGER_OUT"
  VERIFY_FAILED=1
fi

# --- assets ------------------------------------------------------------------

if [ -n "$ASSETS_VOLUME" ]; then
  ASSET_TAR="$BACKUP/assets.tar.gz"
  if [ ! -f "$ASSET_TAR" ]; then
    bad "--assets-volume was given but this backup has no assets.tar.gz"
    VERIFY_FAILED=1
  elif ! command -v docker >/dev/null 2>&1; then
    bad "--assets-volume was given but docker is not on PATH"
    VERIFY_FAILED=1
  else
    bold "restoring assets into docker volume $ASSETS_VOLUME"
    if [ "$FORCE" -eq 0 ] && [ -n "$(docker run --rm -v "$ASSETS_VOLUME":/dst alpine sh -c 'ls -A /dst' 2>/dev/null)" ]; then
      die "docker volume $ASSETS_VOLUME is not empty. Unpacking a backup over live assets mixes two generations of files, and the result passes every count. Pass --force if that is what you want."
    fi
    docker run --rm -i -v "$ASSETS_VOLUME":/dst alpine tar -xzf - -C /dst < "$ASSET_TAR"
    ACT_LIST="$(mktemp)"
    docker run --rm -v "$ASSETS_VOLUME":/src:ro alpine \
      sh -c 'cd /src && find . -type f | sort | xargs -r sha256sum' > "$ACT_LIST"
    if python3 - "$MANIFEST" "$BACKUP/assets.sha256" "$ACT_LIST" <<'PY'
import hashlib, json, sys
m = json.load(open(sys.argv[1]))["assets"]
want = open(sys.argv[2], "rb").read()
got = open(sys.argv[3], "rb").read()
wl = dict(reversed(l.split(None, 1)) for l in want.decode().splitlines())
gl = dict(reversed(l.split(None, 1)) for l in got.decode().splitlines())
missing = sorted(set(wl) - set(gl))
changed = sorted(k for k in set(wl) & set(gl) if wl[k] != gl[k])
if hashlib.sha256(want).hexdigest() != m["listing_sha256"]:
    print("  \033[31mFAIL\033[0m   the backup's own asset listing does not match its manifest hash")
    sys.exit(1)
if missing or changed:
    for k in missing[:5]:
        print("  \033[31mFAIL\033[0m   asset missing after restore: %s" % k.strip())
    for k in changed[:5]:
        print("  \033[31mFAIL\033[0m   asset content changed: %s" % k.strip())
    sys.exit(1)
print("  \033[32mok\033[0m     %d assets restored, every sha256 identical" % len(wl))
PY
    then :; else VERIFY_FAILED=1; fi
    rm -f "$ACT_LIST"
  fi
fi

say ""
if [ "$VERIFY_FAILED" -ne 0 ]; then
  printf '\033[31mFAILED\033[0m: %s was restored but does NOT match the backup. Do not treat it as a recovered database.\n' "$TARGET_DB"
  exit 1
fi
printf '\033[32mOK\033[0m: %s is a verified restore of %s taken at %s\n' \
  "$TARGET_DB" "$SOURCE_DB" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["taken_at"])' "$MANIFEST")"
