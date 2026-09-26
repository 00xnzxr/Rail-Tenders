#!/bin/sh
# Bring up the processes the platform needs inside one Vercel container.
#
# Redis is local and ephemeral on purpose: nothing here treats it as a store of
# record. It carries the RQ queues (`drpl-runs`, `collect`), the run event
# streams the SSE endpoints read, and the short web-search cache -- all of
# which are rebuilt from Postgres or re-fetched. What it buys is that a GeM
# sweep and an agent run execute off the request thread instead of holding an
# HTTP connection open for their whole duration. Measured on this platform:
# the container keeps running between requests and its background threads keep
# ticking, so a queued job really does make progress while nobody is looking.
#
# Postgres is started ONLY when DATABASE_URL is unset, and it dies with the
# container. Point DATABASE_URL at a managed Postgres for durable data.
set -e

: "${PORT:=8080}"
: "${REDIS_URL:=redis://127.0.0.1:6379/0}"
: "${WORKER_CONCURRENCY:=2}"
: "${WEB_CONCURRENCY:=2}"
export REDIS_URL WORKER_CONCURRENCY

echo "[start] redis-server on 127.0.0.1:6379"
redis-server --daemonize yes --save '' --appendonly no --bind 127.0.0.1 --port 6379
for _ in 1 2 3 4 5 6 7 8 9 10; do
    redis-cli -h 127.0.0.1 ping >/dev/null 2>&1 && break
    sleep 0.3
done

if [ -z "${DATABASE_URL}" ]; then
    echo "[start] DATABASE_URL unset -- starting the container-local Postgres"
    echo "[start] WARNING: this database dies with the container. Set"
    echo "[start]          DATABASE_URL to keep data across redeploys."
    PGBIN="$(ls -d /usr/lib/postgresql/*/bin | head -1)"
    export PGDATA=/var/lib/postgresql/drpl
    mkdir -p "$PGDATA" /var/run/postgresql
    chown -R postgres:postgres "$PGDATA" /var/run/postgresql

    su postgres -c "$PGBIN/initdb -D $PGDATA -A trust --encoding=UTF8" >/dev/null 2>&1 || true
    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c listen_addresses=127.0.0.1 -p 5432' -w -t 60 start"
    su postgres -c "$PGBIN/createdb drpl" 2>/dev/null || true
    DATABASE_URL="postgresql://postgres@127.0.0.1:5432/drpl"
    export DATABASE_URL
fi
echo "[start] database host: $(echo "$DATABASE_URL" | sed 's#//[^@]*@#//***@#')"

# app.main does its own create_all + drift fixes on import, so the schema is
# built by the first uvicorn worker. Do that here instead, before anything else
# starts, so the seeding below has tables to write into and two uvicorn workers
# are not racing the same ALTERs.
echo "[start] schema + platform defaults"
python -c "import app.main" >/dev/null 2>&1 || python -c "import app.main"
python -m app.seed || echo "[start] WARNING: app.seed failed; continuing"
python scripts/seed_demo.py || echo "[start] WARNING: seed_demo failed; continuing"

echo "[start] RQ worker (concurrency=${WORKER_CONCURRENCY})"
python worker.py &
WORKER_PID=$!

# Relay shutdown so the worker drains instead of being killed with the
# container.
trap 'kill -TERM "$WORKER_PID" 2>/dev/null; exit 0' TERM INT

echo "[start] uvicorn on 0.0.0.0:${PORT}"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers "${WEB_CONCURRENCY}" --timeout-keep-alive 75
