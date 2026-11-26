#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
cd "$root"
python="$(printenv VITALS_PYTHON || printf python3)"
venv="$root/.venv/bin/python"
prepare=false
build=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --setup-only) prepare=true; shift ;;
        --build) build=true; shift ;;
        *) break ;;
    esac
done

if [[ ! -x "$venv" ]]; then
    "$python" -c 'import sys; sys.exit("Vitals 需要 Python 3.10 或更新版本") if sys.version_info < (3, 10) else None'
    "$python" -m venv "$root/.venv"
fi
"$venv" -m pip install -r backend/requirements.txt

if [[ ! -e config.json ]]; then
    (umask 077; cp -n config.example.json config.json)
fi

index="backend/static/index.html"
if $build || [[ ! -f "$index" ]] || [[ frontend/package-lock.json -nt "$index" ]] ||
    [[ -n "$(find frontend/src -type f -newer "$index" -print -quit)" ]]; then
    if ! command -v npm >/dev/null; then
        printf '%s\n' "构建 Vitals 网页需要 Node.js 和 npm。" >&2
        exit 1
    fi
    npm --prefix frontend ci
    npm --prefix frontend run build
fi

if ! $prepare; then
    exec "$venv" -B -m backend.main "$@"
fi
