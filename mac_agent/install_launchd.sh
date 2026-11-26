#!/bin/bash
set -euo pipefail

agent_dir="$(cd "$(dirname "$0")" && pwd)"
config="$agent_dir/config.json"
python="$(printenv VITALS_PYTHON || printf python3)"
venv_python="$agent_dir/.venv/bin/python"
label="com.vitals.mac-agent"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/vitals-mac-agent.log"

if [[ "$(uname -s)" != Darwin || "$(id -u)" == 0 ]]; then
    printf '%s\n' '请在 macOS 登录用户的终端执行，无需 sudo。' >&2
    exit 1
fi

umask 077
if [[ ! -e "$config" ]]; then
    cp "$agent_dir/config.example.json" "$config"
    printf '请填写 %s 中的 server_url 和 token，然后重新运行。\n' "$config" >&2
    exit 1
fi
chmod 600 "$config"

"$python" -c 'import sys; sys.exit("请安装 Python 3.10 或更新版本") if sys.version_info < (3, 10) else None'
"$python" -m venv "$agent_dir/.venv"
"$venv_python" -m pip install -r "$agent_dir/requirements.txt"
"$venv_python" "$agent_dir/agent.py" --config "$config" --check-config

mkdir -p "$(dirname "$plist")" "$(dirname "$log")"
touch "$log"
"$venv_python" - "$label" "$agent_dir" "$venv_python" "$config" "$log" "$plist" <<'PY'
from pathlib import Path
import plistlib
import sys
import tempfile

label, directory, python, config, log, target = sys.argv[1:]
target = Path(target)
with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
    try:
        plistlib.dump({
            "Label": label,
            "ProgramArguments": [python, "-u", str(Path(directory) / "agent.py"), "--config", config],
            "WorkingDirectory": directory,
            "LimitLoadToSessionType": "Aqua",
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }, output)
        output.flush()
        Path(output.name).replace(target)
    finally:
        Path(output.name).unlink(missing_ok=True)
PY
/usr/bin/plutil -lint "$plist"
if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then
    /bin/launchctl bootout "$domain/$label"
fi
/bin/launchctl enable "$domain/$label"
/bin/launchctl bootstrap "$domain" "$plist"
printf 'Vitals Mac agent 已加载。日志 %s\n' "$log"
