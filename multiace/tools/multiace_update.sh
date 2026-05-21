#!/bin/sh
set -e
REPO="${MULTIACE_UPDATE_REPO:-decay71/multiACE}"
STATIC_BASE="${MULTIACE_UPDATE_URL_BASE:-}"
USE_STATIC=0
if [ -n "$STATIC_BASE" ]; then
    USE_STATIC=1
    STATIC_BASE="${STATIC_BASE%/}"
    if [ "${MULTIACE_UPDATE_PRERELEASE:-0}" = "1" ]; then
        STATIC_INDEX_URL="$STATIC_BASE/beta.txt"
    else
        STATIC_INDEX_URL="$STATIC_BASE/latest.txt"
    fi
elif [ "${MULTIACE_UPDATE_PRERELEASE:-0}" = "1" ]; then
    API="https://api.github.com/repos/$REPO/releases"
else
    API="https://api.github.com/repos/$REPO/releases/latest"
fi
ACE_PY="/home/lava/klipper/klippy/extras/ace.py"
INSTALL_BASE="/home/lava/multiace"
restart_klipper() {
    # 1. Moonraker API - preferred on Snapmaker / PAXX. Runs as lava,
    #    exposes /printer/firmware_restart on 127.0.0.1:7125, works
    #    whether the updater itself is root or lava.
    for url in \
        http://127.0.0.1:7125/printer/firmware_restart \
        http://127.0.0.1:7125/printer/restart; do
        if command -v curl >/dev/null 2>&1; then
            if curl -sf -X POST "$url" >/dev/null 2>&1; then
                return 0
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -q --method=POST -O /dev/null "$url" 2>/dev/null; then
                return 0
            fi
        fi
    done
    # 2. Init scripts. Snapmaker U1 / PAXX uses S60klipper; the others
    #    are listed for forks. Needs root.
    for init in /etc/init.d/S60klipper /etc/init.d/S55klipper \
                /etc/init.d/S58klipper /etc/init.d/klipper \
                /etc/init.d/S99klipper; do
        if [ -x "$init" ]; then
            "$init" restart >/dev/null 2>&1 && return 0
        fi
    done
    # 3. systemd, not on this hardware but harmless.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl restart klipper >/dev/null 2>&1 && return 0
    fi
    echo "WARN: could not restart Klipper automatically - do it manually" >&2
    return 1
}
current_version() {
    if [ -f "$ACE_PY" ]; then
        sed -n -e "s/^MULTIACE_VERSION *= *'\([^']*\)'.*/\1/p" \
               -e 's/^MULTIACE_VERSION *= *"\([^"]*\)".*/\1/p' \
               "$ACE_PY" | head -1
    fi
}
fetch_url() {
    url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -sSfL "$url"
        return $?
    fi
    if command -v wget >/dev/null 2>&1; then
        if [ "${url#https://}" = "$url" ]; then
            wget -qO- "$url"
            return $?
        fi
        out="$(wget -qO- "$url" 2>&1)"
        rc=$?
        if [ "$rc" = "0" ]; then
            printf '%s' "$out"
            return 0
        fi
        case "$out" in
            *"not an http or ftp url"*|*"SSL_init"*|*"not compiled"*) ;;
            *) printf '%s' "$out" >&2; return "$rc" ;;
        esac
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 - "$url" <<'PYEOF'
import sys, ssl, urllib.request
url = sys.argv[1]
try:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "multiace-update/1.0"})
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        sys.stdout.buffer.write(r.read())
except Exception as e:
    sys.stderr.write("python urlopen failed: %s\n" % e)
    sys.exit(1)
PYEOF
        return $?
    fi
    echo "ERROR: need curl, wget-with-SSL, or python3" >&2
    return 2
}
fetch_json() {
    fetch_url "$API"
}
fetch_static_tag() {
    fetch_url "$STATIC_INDEX_URL" | head -1 | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}
resolve_latest() {
    if [ "$USE_STATIC" -eq 1 ]; then
        LATEST="$(fetch_static_tag || true)"
        if [ -z "$LATEST" ]; then
            echo "ERROR: could not fetch $STATIC_INDEX_URL" >&2
            return 1
        fi
        PUBLISHED=""
        TARBALL_URL="$STATIC_BASE/multiace-${LATEST}.tar.gz"
        SHA_URL="$STATIC_BASE/multiace-${LATEST}.tar.gz.sha256"
        return 0
    fi
    JSON="$(fetch_json)"
    LATEST="$(echo "$JSON" | json_field tag_name)"
    PUBLISHED="$(echo "$JSON" | json_field published_at)"
    if [ -z "$LATEST" ]; then
        echo "ERROR: could not parse latest tag from $API" >&2
        return 1
    fi
    TARBALL_URL="$(echo "$JSON" | json_asset_urls | grep -E 'multiace-.*\.tar\.gz$' | head -1)"
    SHA_URL="$(echo "$JSON" | json_asset_urls | grep -E 'multiace-.*\.tar\.gz\.sha256$' | head -1)"
    return 0
}
json_field() {
    sed -n "s/.*\"$1\":[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -1
}
json_asset_urls() {
    sed -n 's/.*"browser_download_url":[[:space:]]*"\([^"]*\)".*/\1/p'
}
normalize_version() {
    echo "${1:-}" | sed -n 's/^v\?\([0-9][0-9.]*[a-z]\?\).*/\1/p'
}
is_newer() {
    cur="$1"
    lat="$2"
    [ -z "$lat" ] && return 1
    [ -z "$cur" ] && return 0
    [ "$cur" = "$lat" ] && return 1
    newest="$(printf '%s\n%s\n' "$cur" "$lat" | sort -V | tail -1)"
    [ "$newest" = "$lat" ] && return 0
    return 1
}
cmd_check() {
    CUR="$(current_version || true)"
    resolve_latest || return 1
    echo "STATUS: current=$CUR latest=$LATEST published=$PUBLISHED"
    cur_norm="$(normalize_version "$CUR")"
    lat_norm="$(normalize_version "$LATEST")"
    if [ "$cur_norm" = "$lat_norm" ]; then
        echo "STATUS: up_to_date"
        return 0
    fi
    if is_newer "$cur_norm" "$lat_norm"; then
        echo "STATUS: update_available from=$CUR to=$LATEST"
    else
        echo "STATUS: up_to_date current=$CUR newer_than latest=$LATEST"
    fi
    return 0
}
cmd_apply() {
    FORCE=0
    KEEP_WEB=0
    INSTALL_WEB_FLAG=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --force)    FORCE=1 ;;
            --keep-web) KEEP_WEB=1 ;;
            --install-web) INSTALL_WEB_FLAG="--install-web" ;;
            *) echo "WARN: ignoring unknown flag: $1" >&2 ;;
        esac
        shift
    done
    if [ "$(id -u)" -ne 0 ]; then
        SUDO_BIN=""
        for c in /usr/bin/sudo /bin/sudo /usr/local/bin/sudo; do
            if [ -x "$c" ]; then SUDO_BIN="$c"; break; fi
        done
        if [ -z "$SUDO_BIN" ] && command -v sudo >/dev/null 2>&1; then
            SUDO_BIN="sudo"
        fi
        if [ -n "$SUDO_BIN" ]; then
            echo "STATUS: re-execing as root via $SUDO_BIN (klipper extras dir is root-owned)"
            exec "$SUDO_BIN" -n "$0" apply ${FORCE:+--force} ${KEEP_WEB:+--keep-web} $INSTALL_WEB_FLAG
        else
            echo "ERROR: must run as root - klipper extras dir is not writable as $(id -un); sudo not found" >&2
            return 1
        fi
    fi
    CUR="$(current_version || true)"
    resolve_latest || return 1
    cur_norm="$(normalize_version "$CUR")"
    lat_norm="$(normalize_version "$LATEST")"
    if [ "$FORCE" -eq 0 ]; then
        if [ "$cur_norm" = "$lat_norm" ]; then
            echo "STATUS: already_on_latest version=$CUR - pass --force to reinstall"
            return 0
        fi
        if ! is_newer "$cur_norm" "$lat_norm"; then
            echo "STATUS: refusing_downgrade current=$CUR latest=$LATEST - pass --force to override"
            return 0
        fi
    fi
    if [ -z "$TARBALL_URL" ]; then
        echo "ERROR: release $LATEST has no multiace-*.tar.gz asset" >&2
        return 1
    fi
    echo "STATUS: downloading tarball=$TARBALL_URL"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    TARBALL="$TMP/multiace.tar.gz"
    fetch_url "$TARBALL_URL" > "$TARBALL" || {
        echo "ERROR: tarball download failed from $TARBALL_URL" >&2
        return 1
    }
    if [ -n "$SHA_URL" ]; then
        echo "STATUS: verifying sha256"
        fetch_url "$SHA_URL" > "$TARBALL.sha256" || {
            echo "WARN: sha256 download failed - skipping verification" >&2
        }
        EXPECTED="$(awk '{print $1}' "$TARBALL.sha256" | head -1)"
        ACTUAL="$(sha256sum "$TARBALL" | awk '{print $1}')"
        if [ "$EXPECTED" != "$ACTUAL" ]; then
            echo "ERROR: sha256 mismatch - expected $EXPECTED got $ACTUAL" >&2
            return 1
        fi
        echo "STATUS: sha256_ok"
    else
        echo "STATUS: sha256_skipped (no .sha256 asset on release - trust GitHub TLS)"
    fi
    echo "STATUS: extracting"
    mkdir "$TMP/extracted"
    tar xzf "$TARBALL" -C "$TMP/extracted"
    SRC="$(find "$TMP/extracted" -maxdepth 3 -name install_multiace.sh | head -1)"
    SRC="$(dirname "$SRC" 2>/dev/null)"
    [ "$SRC" = "." ] && SRC=""
    if [ -z "$SRC" ]; then
        echo "ERROR: tarball missing install_multiace.sh - wrong asset layout" >&2
        return 1
    fi
    if [ "$KEEP_WEB" -eq 1 ]; then
        INSTALL_WEB_FLAG=""
    elif [ -z "$INSTALL_WEB_FLAG" ] && [ -x /etc/init.d/S98multiace-web ]; then
        INSTALL_WEB_FLAG="--install-web"
    fi
    echo "STATUS: applying install_multiace.sh $INSTALL_WEB_FLAG"
    bash "$SRC/install_multiace.sh" $INSTALL_WEB_FLAG
    echo "STATUS: please_reboot_printer"
    echo "INFO: Update installed. Please restart the printer for the new code to take effect."
    echo "STATUS: done from=$CUR to=$LATEST"
}
case "${1:-check}" in
    check)
        shift; cmd_check "$@" ;;
    apply)
        shift; cmd_apply "$@" ;;
    -h|--help|help)
        sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# *//'
        ;;
    *)
        echo "ERROR: unknown command: $1" >&2
        echo "Run with --help for usage." >&2
        exit 2
        ;;
esac
