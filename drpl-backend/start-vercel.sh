#!/bin/sh
# Bring up the platform inside one Vercel container.
#
# ORDER MATTERS. The first version did Postgres initdb, then `import app.main`
# (create_all + ~30 ALTERs + six seeders), then app.seed, then seed_demo, and
# only then bound the port. That is a minute or more before anything is
# listening, and the platform kills a container that has not bound $PORT --
# which showed up as FUNCTION_INVOCATION_FAILED on every request with a
# perfectly healthy image behind it. So: the datastores come up, uvicorn binds,
# and the seeding that does not gate a first request happens behind it.
#
# No `set -e` either. A failure in any one of these is worth a log line, not a
# dead container -- a demo that cannot write its scope profile is still a demo,
# and a container that exits tells you nothing about which step failed.

: "${PORT:=8080}"
: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${WORKER_CONCURRENCY:=2}"
export REDIS_URL WORKER_CONCURRENCY

echo "[start] redis-server on 127.0.0.1:6379"
redis-server --daemonize yes --save '' --appendonly no --bind 127.0.0.1 --port 6379 \
    || echo "[start] WARNING: redis failed to start; queued work will run inline"

if [ -z "${DATABASE_URL}" ]; then
    echo "[start] DATABASE_URL unset -- starting the container-local Postgres."
    echo "[start] WARNING: it dies with the container. Set DATABASE_URL to keep data."
    PGBIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | head -1)"
    export PGDATA=/var/lib/postgresql/drpl
    mkdir -p "$PGDATA" /var/run/postgresql
    chown -R postgres:postgres "$PGDATA" /var/run/postgresql
    chmod 0775 /var/run/postgresql
    su postgres -c "$PGBIN/initdb -D $PGDATA -A trust --encoding=UTF8" >/tmp/initdb.log 2>&1
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1 -p 5432' -w -t 60 start" \
        >/tmp/pgctl.log 2>&1
    su postgres -c "$PGBIN/createdb drpl" >/dev/null 2>&1

    if su postgres -c "$PGBIN/pg_isready -h 127.0.0.1 -p 5432" >/dev/null 2>&1; then
        DATABASE_URL="postgresql://postgres@127.0.0.1:5432/drpl"
        echo "[start] local Postgres ready"
    else
        # Better a degraded demo than a container that will not boot. SQLite
        # takes concurrent writers badly, which is why uvicorn drops to one
        # worker below when we land here.
        DATABASE_URL="sqlite:////app/drpl_local.db"
        echo "[start] WARNING: local Postgres did not come up; falling back to SQLite"
        echo "[start] --- initdb ---"; tail -5 /tmp/initdb.log 2>/dev/null
        echo "[start] --- pg_ctl ---"; tail -5 /tmp/pgctl.log 2>/dev/null
    fi
    export DATABASE_URL
fi
echo "[start] database: $(echo "$DATABASE_URL" | sed 's#//[^@/]*@#//***@#')"

# One web worker. Every worker re-runs app.main's whole startup (create_all,
# the drift ALTERs, six seeders) against the same database, so a second one
# doubles the boot cost and races the first through the same DDL for no
# throughput this demo needs.
WEB_WORKERS=1

# Everything that does not gate the first request. It waits for the schema --
# app.main builds that on import, inside uvicorn -- then seeds and starts the
# queue worker.
(
    i=0
    while [ $i -lt 120 ]; do
        if python -c "
import sys
from sqlalchemy import inspect
from app.core.database import engine
sys.exit(0 if 'users' in inspect(engine).get_table_names() else 1)
" >/dev/null 2>&1; then
            break
        fi
        i=$((i + 2)); sleep 2
    done
    echo "[start] schema present after ${i}s; seeding"
    python -m app.seed          || echo "[start] WARNING: app.seed failed"
    python scripts/seed_demo.py || echo "[start] WARNING: seed_demo failed"
    echo "[start] RQ worker (concurrency=${WORKER_CONCURRENCY})"
    python worker.py
) &

echo "[start] uvicorn binding 0.0.0.0:${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" \
     --workers "${WEB_WORKERS}" --timeout-keep-alive 75
