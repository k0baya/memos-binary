#!/system/bin/sh
# Integration example for an existing module; put the Android ARM64 binary in bin/memos.
# Never use a Linux/musl binary here. No SELinux policy or system mounts are changed.
MODDIR=${0%/*}
MODDIR=$(cd "$MODDIR" && pwd -P) || exit 1
DATA=/data/adb/memos
umask 077
mkdir -p "$DATA/tmp" || exit 1
exec 9>"$DATA/start.lock" || exit 1
flock -n 9 || exit 0

# Do not block post-fs-data. This file belongs in Magisk late_start service mode.
attempt=0
while [ "$(getprop sys.boot_completed)" != 1 ] && [ "$attempt" -lt 90 ]; do
  sleep 2
  attempt=$((attempt + 1))
done
[ -x "$MODDIR/bin/memos" ] || exit 1
[ ! -f "$MODDIR/disable" ] && [ ! -f "$MODDIR/remove" ] || exit 0

if [ -f "$DATA/memos.pid" ]; then
  pid=$(cat "$DATA/memos.pid")
  case "$pid" in
    ''|*[!0-9]*) ;;
    *) [ "$(readlink "/proc/$pid/exe")" = "$MODDIR/bin/memos" ] && exit 0 ;;
  esac
fi

export HOME="$DATA" TMPDIR="$DATA/tmp"
cd "$DATA" || exit 1
nohup "$MODDIR/bin/memos" --addr=127.0.0.1 --port=5230 --driver=sqlite --data="$DATA" \
  >>"$DATA/memos.log" 2>&1 </dev/null 9>&- &
echo "$!" >"$DATA/memos.pid"
# Data stays outside the module, so a module update/uninstall does not erase notes.
