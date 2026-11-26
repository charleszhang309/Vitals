#!/bin/bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
label="com.vitals.hub"
domain="gui/$(id -u)"
service="$domain/$label"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/vitals-hub.log"

if [[ "$(uname -s)" != Darwin || "$(id -u)" == 0 ]]; then
    printf '%s\n' '请用 macOS 登录用户运行此脚本，无需 sudo。' >&2
    exit 1
fi

"$root/start_service.sh" --setup-only
umask 077
mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
touch "$log"
"$root/.venv/bin/python" - "$root" "$label" "$log" "$plist" "$@" <<'PY'
from pathlib import Path
import plistlib
import sys
import tempfile

root, label, log, plist, *args = sys.argv[1:]
target = Path(plist)
with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
    temporary = Path(output.name)
    try:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": [str(Path(root) / ".venv/bin/python"), "-B", "-u", "-m", "backend.main", *args],
            "WorkingDirectory": root,
            "LimitLoadToSessionType": "Aqua",
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }, output)
        output.flush()
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
PY
/usr/bin/plutil -lint "$plist"

if /bin/launchctl print "$service" >/dev/null 2>&1; then
    /bin/launchctl bootout "$service"
    for ((attempt = 0; attempt < 30; attempt++)); do
        if ! /bin/launchctl print "$service" >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
fi
/bin/launchctl enable "$service"
if ! /bin/launchctl bootstrap "$domain" "$plist"; then
    sleep 1
    /bin/launchctl bootstrap "$domain" "$plist"
fi
printf 'Vitals 主机已加载。日志 %s\n' "$log"
