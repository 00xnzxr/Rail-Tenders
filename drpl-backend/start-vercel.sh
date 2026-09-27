#!/bin/sh
# Bring up the platform inside one Vercel container.
#
# The port is bound FIRST, by boot_diag.py, and only handed to uvicorn once the
# app has been shown to import. Two reasons, both learned here:
#
#   * the platform kills a container that has not bound $PORT, and this app's
#     import alone (create_all, ~30 drift ALTERs, six seeders) is a minute of
#     work before uvicorn would otherwise listen;
#   * Vercel's runtime-log API answers 403 for this project, so a crash at
#     startup is invisible from outside -- every request just returns
#     FUNCTION_INVOCATION_FAILED. boot_diag serves the boot log instead, which
#     turns an opaque 500 into the actual traceback.
#
# No `set -e`: a failed seeder is a log line, not a dead container.

: "${PORT:=8080}"
: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${WORKER_CONCURRENCY:=2}"
export REDIS_URL WORKER_CONCURRENCY

BOOT_LOG=/tmp/boot.log
: > "$BOOT_LOG"
log() { echo "[start] $*" | tee -a "$BOOT_LOG"; }

# Bind the port immediately so the container is never killed for being slow.
python /app/boot_diag.py "$BOOT_LOG" &
DIAG_PID=$!
log "boot diagnostics listening on :${PORT} (pid $DIAG_PID)"

log "redis-server on 127.0.0.1:6379"
redis-server --daemonize yes --save '' --appendonly no --bind 127.0.0.1 --port 6379 \
    >>"$BOOT_LOG" 2>&1 || log "WARNING: redis failed to start"

if [ -z "${DATABASE_URL}" ]; then
    log "DATABASE_URL unset -- starting the container-local Postgres (dies with the container)"
    PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | head -1)"
    log "postgres binaries: ${PGBIN:-NONE FOUND}"
    export PGDATA=/var/lib/postgresql/drpl
    mkdir -p "$PGDATA" /var/run/postgresql
    chown -R postgres:postgres "$PGDATA" /var/run/postgresql
    chmod 0775 /var/run/postgresql
    su postgres -c "$PGBIN/initdb -D $PGDATA -A trust --encoding=UTF8" >>"$BOOT_LOG" 2>&1
    log "initdb exit=$?"
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1 -p 5432' -w -t 60 start" >>"$BOOT_LOG" 2>&1
    log "pg_ctl exit=$?"
    su postgres -c "$PGBIN/createdb drpl" >>"$BOOT_LOG" 2>&1
    log "createdb exit=$?"

    if su postgres -c "$PGBIN/pg_isready -h 127.0.0.1 -p 5432" >>"$BOOT_LOG" 2>&1; then
        DATABASE_URL="postgresql://postgres@127.0.0.1:5432/drpl"
        log "local Postgres ready"
    else
        # A degraded demo beats a container that will not boot.
        DATABASE_URL="sqlite:////app/drpl_local.db"
        log "WARNING: local Postgres did not come up; falling back to SQLite"
    fi
    export DATABASE_URL
fi
log "database: $(echo "$DATABASE_URL" | sed 's#//[^@/]*@#//***@#')"

# The expensive part, with its traceback captured where it can be read.
log "importing app.main (schema + seeders) ..."
python -c "import app.main; print('[start] app.main imported OK')" >>"$BOOT_LOG" 2>&1
IMPORT_RC=$?
log "app.main import exit=$IMPORT_RC"

if [ "$IMPORT_RC" -ne 0 ]; then
    log "FATAL: the app could not be imported. Holding the port open so this log is readable."
    log "----- last 120 lines above are the traceback -----"
    wait $DIAG_PID
    exit 1
fi

# Seeding and the queue worker do not gate a first request.
(
    python -m app.seed          >>"$BOOT_LOG" 2>&1 || log "WARNING: app.seed failed"
    python scripts/seed_demo.py >>"$BOOT_LOG" 2>&1 || log "WARNING: seed_demo failed"
    # Sample tenders, so the list, funnel and dashboard have content on a fresh
    # database. Seeded per instance on purpose: the platform may run several
    # containers, each with its own local Postgres, and a fixed random seed
    # means every one of them holds the identical set rather than a different
    # one. Every row is marked 'drpl-demo-seed' -- see the script's docstring
    # for the one statement that removes them all.
    python scripts/seed_demo_tenders.py >>"$BOOT_LOG" 2>&1 || log "WARNING: seed_demo_tenders failed"
    log "seeding done; starting RQ worker (concurrency=${WORKER_CONCURRENCY})"
    python worker.py >>"$BOOT_LOG" 2>&1
) &

log "handing the port to uvicorn"
kill "$DIAG_PID" 2>/dev/null
sleep 1

# One web worker: each re-runs app.main's whole startup against the same
# database and races the other through the same DDL.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" \
     --workers 1 --timeout-keep-alive 75
