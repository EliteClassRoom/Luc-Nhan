#!/usr/bin/env bash
# Luc Nhan installer for Linux and macOS
# Usage: ./install.sh [IDA_USER_DIR]
#   IDA_USER_DIR  Optional path to IDA user directory (default: auto-detect)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { printf "${CYAN}[*]${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}[+]${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}[!]${NC} %s\n" "$*"; }
err()   { printf "${RED}[-]${NC} %s\n" "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Locate IDA user directory ─────────────────────────────────────────

find_ida_user_dir() {
    # Common locations, in order of preference
    local candidates=()

    if [[ "$(uname)" == "Darwin" ]]; then
        candidates+=(
            "$HOME/.idapro"
            "$HOME/Library/Application Support/Hex-Rays/IDA Pro"
        )
    else
        candidates+=(
            "$HOME/.idapro"
            "$HOME/.ida"
        )
    fi

    for dir in "${candidates[@]}"; do
        if [[ -d "$dir" ]]; then
            echo "$dir"
            return 0
        fi
    done

    return 1
}

if [[ $# -ge 1 ]]; then
    IDA_USER_DIR="$1"
    if [[ ! -d "$IDA_USER_DIR" ]]; then
        err "Provided IDA directory does not exist: $IDA_USER_DIR"
        exit 1
    fi
    info "Using provided IDA directory: $IDA_USER_DIR"
else
    if IDA_USER_DIR="$(find_ida_user_dir)"; then
        info "Auto-detected IDA directory: $IDA_USER_DIR"
    else
        # Fall back to the standard default and create it
        IDA_USER_DIR="$HOME/.idapro"
        warn "No IDA directory found, defaulting to $IDA_USER_DIR"
    fi
fi

PLUGINS_DIR="$IDA_USER_DIR/plugins"
CONFIG_DIR="$IDA_USER_DIR/lucnhan"

# ── Remove old rebrand leftovers (iris, then rikugan) ────────────────
# ``rikugan_plugin.py`` only, never the ``rikugan`` package directory: when
# this installer runs from the plugin directory itself, that directory *is*
# the source tree.
for old_name in "iris_plugin.py" "iris" "rikugan_plugin.py"; do
    OLD_PATH="$PLUGINS_DIR/$old_name"
    if [[ -L "$OLD_PATH" ]]; then
        warn "Removing old '$old_name' symlink: $OLD_PATH"
        rm "$OLD_PATH"
        ok "Old '$old_name' symlink removed"
    elif [[ -e "$OLD_PATH" ]]; then
        warn "Removing old '$old_name': $OLD_PATH"
        rm -rf "$OLD_PATH"
        ok "Old '$old_name' removed"
    fi
done

# ── Sanity checks ─────────────────────────────────────────────────────

if [[ ! -f "$SCRIPT_DIR/lucnhan_plugin.py" ]]; then
    err "lucnhan_plugin.py not found in $SCRIPT_DIR — run this from the repo root"
    exit 1
fi

if [[ ! -d "$SCRIPT_DIR/lucnhan" ]]; then
    err "lucnhan/ package not found in $SCRIPT_DIR — run this from the repo root"
    exit 1
fi

# ── Find IDA installation directory ───────────────────────────────────

find_ida_install_dir() {
    # Check IDADIR env var first
    if [[ -n "${IDADIR:-}" ]] && [[ -d "$IDADIR" ]]; then
        echo "$IDADIR"
        return 0
    fi

    local candidates=()
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS: IDA .app bundles — the actual binaries are in Contents/MacOS
        for app in /Applications/IDA*.app; do
            [[ -d "$app/Contents/MacOS" ]] && candidates+=("$app/Contents/MacOS")
        done
        for app in "$HOME/Applications/IDA"*.app; do
            [[ -d "$app/Contents/MacOS" ]] && candidates+=("$app/Contents/MacOS")
        done
    else
        # Linux common install locations
        candidates+=(
            /opt/ida*
            /opt/idapro*
            "$HOME/ida"*
            "$HOME/idapro"*
        )
    fi

    for dir in "${candidates[@]}"; do
        if [[ -d "$dir" ]]; then
            echo "$dir"
            return 0
        fi
    done

    # Try finding via PATH (ida64 or idat64)
    local ida_bin
    for name in ida64 idat64 ida idat; do
        if ida_bin="$(command -v "$name" 2>/dev/null)"; then
            # Resolve symlinks and get directory
            local real_bin
            real_bin="$(readlink -f "$ida_bin" 2>/dev/null || realpath "$ida_bin" 2>/dev/null || echo "$ida_bin")"
            echo "$(dirname "$real_bin")"
            return 0
        fi
    done

    return 1
}

# ── Find IDA's Python ────────────────────────────────────────────────

_dylib_to_interpreter() {
    # Given a libpython dylib/so path, find the python3 interpreter.
    # e.g. .../Python.framework/Versions/3.14/Python → .../Versions/3.14/bin/python3
    # e.g. /usr/lib/libpython3.12.so → /usr/bin/python3.12 or /usr/bin/python3
    local dylib="$1"
    local dylib_dir
    dylib_dir="$(dirname "$dylib")"

    # Framework layout (macOS): <framework>/Versions/X.Y/bin/python3
    for pybin in "$dylib_dir/bin/python3" "$dylib_dir/bin/python"; do
        if [[ -x "$pybin" ]]; then
            echo "$pybin"
            return 0
        fi
    done

    # Shared lib layout (Linux): libpython is in lib/, interpreter in ../bin/
    for pybin in "$dylib_dir/../bin/python3" "$dylib_dir/python3" "$dylib_dir/../bin/python"; do
        if [[ -x "$pybin" ]]; then
            echo "$pybin"
            return 0
        fi
    done

    return 1
}

_read_ida_reg_python() {
    # Read Python3TargetDLL from IDA's binary registry file (~/.idapro/ida.reg).
    # Format: null-terminated key, 4-byte LE length, 1-byte type, then 'length' bytes of value.
    local reg_file="$1"
    [[ -f "$reg_file" ]] || return 1

    python3 -c "
import sys
with open(sys.argv[1], 'rb') as f:
    data = f.read()
idx = data.find(b'Python3TargetDLL')
if idx < 0:
    sys.exit(1)
key_end = data.index(b'\x00', idx)
length = int.from_bytes(data[key_end+1:key_end+5], 'little')
if length <= 0 or length > 1024:
    sys.exit(1)
value = data[key_end+6:key_end+6+length]
# Decode and strip any trailing nulls
path = value.decode('utf-8', errors='replace').rstrip('\x00')
if path.startswith('/') or path.startswith('\\\\'):
    print(path)
" "$reg_file" 2>/dev/null
}

find_ida_python() {
    local ida_install="$1"

    # 1. Bundled Python: <IDA>/python3*/python3 (IDA 7.5+, some Windows/Linux builds)
    for pydir in "$ida_install"/python3*/; do
        if [[ -x "$pydir/python3" ]]; then
            echo "$pydir/python3"
            return 0
        elif [[ -x "$pydir/python" ]]; then
            echo "$pydir/python"
            return 0
        fi
    done

    # 2. Older bundled layout: <IDA>/python/python3
    if [[ -x "$ida_install/python/python3" ]]; then
        echo "$ida_install/python/python3"
        return 0
    fi

    # 3. Read Python3TargetDLL from ida.reg (works on macOS and Linux)
    local ida_reg="$IDA_USER_DIR/ida.reg"
    local dylib_path
    if dylib_path="$(_read_ida_reg_python "$ida_reg")" && [[ -n "$dylib_path" ]]; then
        local interp
        if interp="$(_dylib_to_interpreter "$dylib_path")"; then
            echo "$interp"
            return 0
        fi
    fi

    # 4. idapyswitch --dry-run --auto-apply --verbose: parse the dylib it would select
    local idapyswitch="$ida_install/idapyswitch"
    if [[ -x "$idapyswitch" ]]; then
        local dll_path
        dll_path="$("$idapyswitch" --dry-run --auto-apply --verbose 2>&1 \
            | grep "Setting registry value Python3TargetDLL" \
            | sed "s/.*to '\\(.*\\)'/\\1/" || true)"
        if [[ -n "$dll_path" ]] && [[ -e "$dll_path" ]]; then
            local interp
            if interp="$(_dylib_to_interpreter "$dll_path")"; then
                echo "$interp"
                return 0
            fi
        fi
    fi

    return 1
}

# ── Install dependencies ──────────────────────────────────────────────

install_requirements() {
    local req="$SCRIPT_DIR/requirements.txt"

    # 1. Explicit override via IDA_PYTHON env var
    if [[ -n "${IDA_PYTHON:-}" ]]; then
        if "$IDA_PYTHON" -m pip install -r "$req"; then
            ok "Dependencies installed with IDA_PYTHON override"
            return 0
        fi
        warn "IDA_PYTHON override failed, trying other methods..."
    fi

    # 2. Try IDA's bundled/configured Python
    local ida_install
    if ida_install="$(find_ida_install_dir)"; then
        info "Found IDA installation at: $ida_install"
        local ida_python
        if ida_python="$(find_ida_python "$ida_install")"; then
            info "Using IDA's Python: $ida_python"
            if "$ida_python" -m pip install --break-system-packages -r "$req" 2>/dev/null \
               || "$ida_python" -m pip install -r "$req"; then
                ok "Dependencies installed into IDA's Python"
                return 0
            fi
            warn "pip install failed with IDA's Python, trying system fallbacks..."
        else
            warn "Could not find IDA's bundled Python, trying system fallbacks..."
        fi
    else
        warn "Could not find IDA installation directory, trying system Python..."
    fi

    # 3. Fallback: system Python
    local fallbacks=("python3 -m pip" "python -m pip" "pip3" "pip")
    for cmd in "${fallbacks[@]}"; do
        if eval "$cmd --version" >/dev/null 2>&1; then
            info "Installing Python dependencies with: $cmd"
            if eval "$cmd install --break-system-packages -r \"$req\"" 2>/dev/null \
               || eval "$cmd install --user -r \"$req\"" 2>/dev/null \
               || eval "$cmd install -r \"$req\""; then
                ok "Dependencies installed successfully"
                return 0
            fi
            warn "Dependency install failed with: $cmd"
        fi
    done
    return 1
}

if ! install_requirements; then
    err "Failed to install Python dependencies from requirements.txt"
    exit 1
fi

# ── Create directories ────────────────────────────────────────────────

mkdir -p "$PLUGINS_DIR"
mkdir -p "$CONFIG_DIR"

# ── Copy built-in skills ──────────────────────────────────────────────

SKILLS_DIR="$CONFIG_DIR/skills"
BUILTINS_SRC="$SCRIPT_DIR/lucnhan/skills/builtins"

# Built-in skills are loaded directly from lucnhan/skills/builtins/ (via symlink).
# The user skills directory is for user-created skills only.
# Remove stale built-in copies that previous installs may have placed here.
if [[ -d "$BUILTINS_SRC" ]] && [[ -d "$SKILLS_DIR" ]]; then
    for skill in "$BUILTINS_SRC"/*/; do
        slug="$(basename "$skill")"
        dst="$SKILLS_DIR/$slug"
        if [[ -d "$dst" ]]; then
            rm -rf "$dst"
            info "Removed stale built-in copy: /$slug"
        fi
    done
fi

# ── Install plugin via symlinks ───────────────────────────────────────

install_link() {
    local src="$1" dst="$2" name="$3"

    if [[ -L "$dst" ]]; then
        local existing
        existing="$(readlink "$dst")"
        if [[ "$existing" == "$src" ]]; then
            ok "$name already linked"
            return
        fi
        warn "Removing stale symlink: $dst -> $existing"
        rm "$dst"
    elif [[ -e "$dst" ]]; then
        warn "Backing up existing $name to ${dst}.bak"
        mv "$dst" "${dst}.bak"
    fi

    ln -s "$src" "$dst"
    ok "$name -> $dst"
}

info "Installing Luc Nhan into $PLUGINS_DIR..."
install_link "$SCRIPT_DIR/lucnhan_plugin.py" "$PLUGINS_DIR/lucnhan_plugin.py" "lucnhan_plugin.py"
install_link "$SCRIPT_DIR/lucnhan"        "$PLUGINS_DIR/lucnhan"        "lucnhan/"

# ── Done ──────────────────────────────────────────────────────────────

echo ""
ok "Luc Nhan installed successfully!"
info "Plugin:  $PLUGINS_DIR/lucnhan_plugin.py"
info "Package: $PLUGINS_DIR/lucnhan"
info "Config:  $CONFIG_DIR/"
info "Skills:  $SKILLS_DIR/"
echo ""
info "Open IDA and press Ctrl+Shift+I to start Luc Nhan."
info "First run: click Settings to configure your LLM provider and API key."
