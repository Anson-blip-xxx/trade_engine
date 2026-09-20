#!/usr/bin/env bash
# Run explicitly isolated data QA. Never uses a configured service DSN.
set -euo pipefail

qa_pg_bin="${V2_QA_POSTGRES_BIN:-/usr/lib/postgresql/16/bin}"
qa_python="${V2_QA_PYTHON:-python3}"
if [[ "$(id -u)" == 0 ]]; then
    echo 'Run data QA as an unprivileged user; initdb refuses root.' >&2
    exit 2
fi
for qa_program in "$qa_pg_bin/initdb" "$qa_pg_bin/pg_ctl" "$qa_pg_bin/pg_dump" "$qa_pg_bin/pg_restore" redis-server redis-cli clickhouse "$qa_python"; do
    command -v "$qa_program" >/dev/null || { echo "Missing QA dependency: $qa_program" >&2; exit 2; }
done

qa_root="$(mktemp -d /tmp/v2-data-qa.XXXXXX)"
mkdir "$qa_root/socket"
qa_cleanup() {
    if [[ -S "$qa_root/redis.sock" ]]; then
        redis-cli -s "$qa_root/redis.sock" shutdown nosave >/dev/null 2>&1 || true
    fi
    if [[ -f "$qa_root/pg/postmaster.pid" ]]; then
        "$qa_pg_bin/pg_ctl" -D "$qa_root/pg" -m fast stop >/dev/null 2>&1 || true
    fi
    echo "QA services stopped; temporary diagnostics retained at $qa_root"
}
trap qa_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$qa_pg_bin/initdb" -D "$qa_root/pg" --encoding=UTF8 --no-locale --auth=trust >"$qa_root/initdb.log"
"$qa_pg_bin/pg_ctl" -D "$qa_root/pg" -l "$qa_root/postgres.log" \
    -o "-k $qa_root/socket -p 55443 -c listen_addresses='' -c cluster_name=v2_isolated_qa" -w start
redis-server --port 0 --unixsocket "$qa_root/redis.sock" --unixsocketperm 700 \
    --save '' --appendonly no --daemonize yes --pidfile "$qa_root/redis.pid" \
    --logfile "$qa_root/redis.log" --dir "$qa_root"

export V2_CORE_TEST_DSN="dbname=postgres host=$qa_root/socket port=55443"
export V2_REDIS_TEST_SOCKET="$qa_root/redis.sock"
export V2_CORE_TEST_ISOLATED=YES
export V2_QA_CLUSTER_RESTART=YES
export PM_NO_WS=1
if [[ $# == 0 ]]; then
    set -- -q tests/v2_core
fi
"$qa_python" -m pytest "$@"
