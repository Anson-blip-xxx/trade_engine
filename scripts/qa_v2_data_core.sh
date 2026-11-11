#!/usr/bin/env bash
# Run explicitly isolated data QA. Never uses a configured service DSN.
set -euo pipefail

# Dedicated V2 collection must not import tests/conftest.py, which still owns
# legacy credentials and fixtures. Resolve relative paths from this checkout,
# even when a caller launches this script from another working directory.
qa_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$qa_repo"
qa_confcutdir="$qa_repo/tests/v2_core"
qa_args=()
qa_has_selection=0
while (( $# )); do
    case "$1" in
        --confcutdir|--confcutdir=*)
            if [[ "$1" == --confcutdir ]]; then
                if (( $# < 2 )); then
                    echo '--confcutdir requires the dedicated V2 test directory.' >&2
                    exit 2
                fi
                qa_requested_cut="$2"
                shift
            else
                qa_requested_cut="${1#--confcutdir=}"
            fi
            if [[ -z "$qa_requested_cut" ]] \
                || ! qa_resolved_cut="$(cd -- "$qa_requested_cut" 2>/dev/null && pwd -P)" \
                || [[ "$qa_resolved_cut" != "$qa_confcutdir" ]]; then
                echo 'Data QA requires --confcutdir=tests/v2_core; legacy conftest loading is not supported.' >&2
                exit 2
            fi
            ;;
        --noconftest|--noconftest=*)
            echo 'Data QA requires the V2 conftest network boundary.' >&2
            exit 2
            ;;
        -r|--report-chars|-r?*|--report-chars=*)
            if [[ "$1" == -r || "$1" == --report-chars ]]; then
                if (( $# < 2 )); then
                    echo "$1 requires summary characters." >&2
                    exit 2
                fi
                qa_report_chars="$2"
                shift
            elif [[ "$1" == --report-chars=* ]]; then
                qa_report_chars="${1#--report-chars=}"
            else
                qa_report_chars="${1#-r}"
            fi
            if [[ ! "$qa_report_chars" =~ ^[fEsxXpPaAwN]*$ ]]; then
                echo 'Unsupported pytest summary characters; use fEsxXpPaAwN.' >&2
                exit 2
            fi
            qa_args+=("--report-chars=$qa_report_chars")
            ;;
        -k|-m|--maxfail|--tb|--capture|--durations|--durations-min|--color|--code-highlight|--verbosity)
            if (( $# < 2 )); then
                echo "$1 requires a value." >&2
                exit 2
            fi
            qa_args+=("$1" "$2")
            shift
            ;;
        -q|-qq|-v|-vv|-vvv|-x|-s|--quiet|--verbose|--exitfirst|--collect-only|--co|--disable-warnings|--disable-pytest-warnings|--showlocals|--full-trace|--no-header|--no-summary|--strict-markers|--strict-config)
            qa_args+=("$1")
            ;;
        --maxfail=*|--tb=*|--capture=*|--durations=*|--durations-min=*|--color=*|--code-highlight=*|--verbosity=*)
            qa_args+=("$1")
            ;;
        -*)
            echo "Unsupported isolated V2 QA option: $1. Custom config, plugins, and collection overrides are not supported." >&2
            exit 2
            ;;
        *)
            qa_selection="${1%%::*}"
            if ! qa_resolved_selection="$(realpath -e -- "$qa_selection" 2>/dev/null)" \
                || [[ "$qa_resolved_selection" != "$qa_confcutdir" && "$qa_resolved_selection" != "$qa_confcutdir/"* ]]; then
                echo "Data QA accepts only existing tests/v2_core selections: $qa_selection" >&2
                exit 2
            fi
            qa_args+=("$1")
            qa_has_selection=1
            ;;
    esac
    shift
done
if (( ${#qa_args[@]} == 0 )); then
    qa_args=(-q)
fi
if (( qa_has_selection == 0 )); then
    qa_args+=("$qa_confcutdir")
fi

# libpq can prefer hostaddr or a service definition over a Unix-socket DSN.
# Never inherit operator connection shortcuts into this isolated QA process.
unset PGHOSTADDR PGSERVICE PGSERVICEFILE

qa_pg_bin="${V2_QA_POSTGRES_BIN:-/usr/lib/postgresql/16/bin}"
qa_python="${V2_QA_PYTHON:-python3}"
if [[ "$(id -u)" == 0 ]]; then
    echo 'Run data QA as an unprivileged user; initdb refuses root.' >&2
    exit 2
fi
for qa_program in "$qa_pg_bin/initdb" "$qa_pg_bin/pg_ctl" "$qa_pg_bin/pg_dump" "$qa_pg_bin/pg_restore" redis-server redis-cli clickhouse flock "$qa_python"; do
    command -v "$qa_program" >/dev/null || { echo "Missing QA dependency: $qa_program" >&2; exit 2; }
done

# All invocations of this checkout share a lock. QA databases can grow past 1 GiB;
# never run multiple copies beside a market process with an 8 GiB disk reserve.
# Different checkouts share a host disk. Also serialize with the scheduled
# stopped-QA collector; never infer liveness from directory age alone.
qa_host_lock="${V2_QA_HOST_LOCK:-/tmp/trade-v2-qa-host-$(id -u).lock}"
if [[ -L "$qa_host_lock" ]]; then
    echo 'QA host lock must not be a symlink.' >&2
    exit 2
fi
exec 8>"$qa_host_lock"
flock -n 8 || { echo 'Another host QA or cleanup is already running.' >&2; exit 2; }
mkdir -p "$qa_repo/.pytest_cache"
exec 9>"$qa_repo/.pytest_cache/v2-data-qa.lock"
flock -n 9 || { echo 'Another isolated data QA is already running.' >&2; exit 2; }
qa_free_bytes="$(df -PB1 /tmp | awk 'NR==2 {print $4}')"
if [[ ! "$qa_free_bytes" =~ ^[0-9]+$ ]] || (( qa_free_bytes < 10737418240 )); then
    echo 'Data QA requires at least 10 GiB free in /tmp; refusing to pressure running services.' >&2
    exit 2
fi
if [[ "${V2_QA_KEEP_DATABASE:-0}" != 0 && "${V2_QA_KEEP_DATABASE:-0}" != 1 ]]; then
    echo 'V2_QA_KEEP_DATABASE must be 0 or 1.' >&2
    exit 2
fi
qa_root="$(mktemp -d /tmp/v2-data-qa.XXXXXX)"
if [[ "${V2_QA_KEEP_DATABASE:-0}" == 1 ]]; then
    touch "$qa_root/KEEP_DATABASE"
fi
mkdir "$qa_root/socket"
qa_cleanup() {
    local qa_status=$?
    if [[ -S "$qa_root/redis.sock" ]]; then
        redis-cli -s "$qa_root/redis.sock" shutdown nosave >/dev/null 2>&1 || true
    fi
    if [[ -f "$qa_root/pg/postmaster.pid" ]]; then
        "$qa_pg_bin/pg_ctl" -D "$qa_root/pg" -m fast stop >/dev/null 2>&1 || true
    fi
    if [[ -e "$qa_root/pg/postmaster.pid" || -S "$qa_root/redis.sock" ]]; then
        echo 'QA service shutdown could not be confirmed; retained data requires inspection.' >&2
        if [[ "$qa_status" == 0 ]]; then qa_status=2; fi
    fi
    # Only this invocation's mktemp database is eligible. Retain diagnostics even
    # on success, and retain data if shutdown could not be confirmed.
    if [[ "${V2_QA_KEEP_DATABASE:-0}" == 0 && ! -e "$qa_root/pg/postmaster.pid" && ! -S "$qa_root/redis.sock" \
          && "$qa_root" =~ ^/tmp/v2-data-qa\.[[:alnum:]]{6}$ ]]; then
        rm -rf -- "$qa_root/pg"
    fi
    echo "QA exit=$qa_status; diagnostics retained at $qa_root; database retained=$([[ -d "$qa_root/pg" ]] && echo yes || echo no)"
    exit "$qa_status"
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
# Ambient pytest settings must not disable conftest or load legacy plugins.
# Explicit default scope also keeps option-only calls (e.g. -q or --collect-only)
# in V2, without pytest's whitespace-delimited testpaths configuration pitfalls.
PYTEST_ADDOPTS= PYTEST_PLUGINS= "$qa_python" -m pytest \
    --confcutdir="$qa_confcutdir" -o addopts= \
    "${qa_args[@]}" 2>&1 | tee "$qa_root/pytest.log"
