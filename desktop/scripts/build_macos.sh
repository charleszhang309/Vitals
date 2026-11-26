#!/bin/bash
set -euo pipefail

scripts="$(cd "$(dirname "$0")" && pwd)"
desktop="$(dirname "$scripts")"
project="$(dirname "$desktop")"
python="$project/.venv/bin/python"
export PATH="$HOME/.cargo/bin:$PATH"

if [[ "$(uname -s)" != Darwin ]]; then
    printf '%s\n' '请在 macOS 上构建 Vitals.app。' >&2
    exit 1
fi
export APPLE_SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:--}"

target="$(rustc -vV | awk '/^host: / { print $2 }')"
case "$target" in
    aarch64-apple-darwin) arch=arm64 ;;
    x86_64-apple-darwin) arch=x86_64 ;;
    *) printf '%s\n' '请使用 macOS 原生 Rust 工具链。' >&2; exit 1 ;;
esac

if [[ ! -x "$python" ]]; then
    "${VITALS_PYTHON:-python3}" -m venv "$project/.venv"
fi
if [[ "$("$python" -c 'import platform; print(platform.machine())')" != "$arch" ]]; then
    printf '%s\n' 'Python 的架构必须与 Rust 工具链一致。请使用同架构的 Python 虚拟环境。' >&2
    exit 1
fi

build="$desktop/.build/$target"
static="$build/static"
binaries="$desktop/src-tauri/binaries"
mkdir -p "$build" "$binaries"

npm --prefix "$project/frontend" ci
npm --prefix "$project/frontend" run build -- --outDir "$static"
"$python" -m pip install pyinstaller==6.22.3 -r "$project/backend/requirements.txt"

pyinstaller=(
    --noconfirm --onefile --console --name vitals-hub --target-architecture "$arch"
    --paths "$project" --distpath "$build/frozen" --workpath "$build/work" --specpath "$build"
    --add-data "$static:backend/static" --add-data "$project/backend/agent.md:backend"
    --collect-submodules uvicorn
)
if [[ "$APPLE_SIGNING_IDENTITY" != "-" ]]; then
    pyinstaller+=(--codesign-identity "$APPLE_SIGNING_IDENTITY")
fi
"$python" -m PyInstaller "${pyinstaller[@]}" "$scripts/sidecar_entry.py"
/usr/bin/install -m 755 "$build/frozen/vitals-hub" "$binaries/vitals-hub-$target"

npm --prefix "$desktop" ci
tauri=(build --target "$target")
if [[ "$APPLE_SIGNING_IDENTITY" == "-" ]]; then
    # Ad-hoc signatures have no Team ID. Match PyInstaller's local signing
    # mode so the frozen Sidecar can load its extracted Python libraries.
    tauri+=(--config '{"bundle":{"macOS":{"hardenedRuntime":false}}}')
fi
# Tauri can create the drag-to-Applications image without controlling Finder.
CI=true npm --prefix "$desktop" run tauri -- "${tauri[@]}" -- --locked
