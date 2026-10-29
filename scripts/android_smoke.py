"""Test the native Android package on a disposable ADB emulator, never a USB phone."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, build_opener
import uuid

from scripts.smoke import verify_frontend


def smoke(directory, probe, serial):
    if not re.fullmatch(r"emulator-\d+", serial):
        raise ValueError("This destructive scratch-data test is restricted to disposable emulators")
    binary = directory / "memos"
    metadata = json.loads((directory / "BUILD-INFO.json").read_text())
    if hashlib.sha256(binary.read_bytes()).hexdigest() != metadata["binary_sha256"]:
        raise AssertionError("Android executable hash mismatch")
    remote = "/data/local/tmp/memos-smoke-" + uuid.uuid4().hex
    prefix = ["adb", "-s", serial]

    def adb(*arguments):
        result = subprocess.run([*prefix, *arguments], check=True, capture_output=True, text=True, timeout=90)
        return result.stdout.strip()

    def shell(command):
        return adb("shell", command)

    def stop():
        # Never signal a reused PID or an unrelated process.
        shell(f'if [ -f {remote}/pid ]; then p=$(cat {remote}/pid); '
              f'if [ "$(readlink /proc/$p/exe)" = "{remote}/memos" ]; then kill -TERM "$p"; fi; fi')
        for _ in range(30):
            running = shell(f'p=$(cat {remote}/pid 2>/dev/null); readlink /proc/$p/exe 2>/dev/null || true')
            if running != remote + "/memos":
                return
            time.sleep(0.5)
        raise AssertionError("Android process did not shut down cleanly")

    adb("wait-for-device")
    print("Android emulator:", shell("getprop ro.build.version.release; getprop ro.product.cpu.abi; getconf PAGE_SIZE || true"), flush=True)
    shell(f"mkdir -p {remote}/data; chmod 700 {remote}")
    adb("push", str(binary), remote + "/memos")
    adb("push", str(probe), remote + "/dns-probe")
    shell(f"chmod 700 {remote}/memos {remote}/dns-probe")
    legacy_mode = " --mode=prod" if "--mode" in shell(f"{remote}/memos --help") else ""
    adb("forward", "tcp:15230", "tcp:15230")
    http = build_opener(ProxyHandler({}))
    base = "http://127.0.0.1:15230"

    def start_and_verify():
        shell(f"cd {remote}; nohup ./memos --addr=127.0.0.1 --port=15230 --driver=sqlite "
              f"--data={remote}/data{legacy_mode} >{remote}/memos.log 2>&1 </dev/null & echo $! >{remote}/pid")
        deadline = time.monotonic() + 180
        while True:
            try:
                with http.open(base + "/", timeout=5) as response:
                    index = response.read().decode()
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Android Memos startup timed out")
                time.sleep(1)
        verify_frontend(http, base, index, metadata)
        stop()
        if metadata["tag"].removeprefix("v") not in shell(f"cat {remote}/memos.log"):
            raise AssertionError("Android startup log does not identify the packaged version")

    try:
        # Compiled with the same NDK, CGO and DNS tags as Memos, without public DNS overrides.
        for attempt in range(3):
            try:
                print(shell(shlex.quote(remote + "/dns-probe")), flush=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(3)
        start_and_verify()
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "before"
            adb("pull", remote + "/data", str(local))
            databases = list(local.glob("*.db"))
            if len(databases) != 1:
                raise AssertionError("Android SQLite database was not created")
            database = databases[0]
            with closing(sqlite3.connect(database)) as connection, connection:
                if not connection.execute("SELECT name FROM sqlite_master WHERE name='memo'").fetchone():
                    raise AssertionError("Android SQLite schema missing")
                connection.execute("CREATE TABLE binary_release_smoke (value TEXT)")
                connection.execute("INSERT INTO binary_release_smoke VALUES ('persistent')")
            # This is exclusively our generated scratch directory, with Memos stopped.
            shell(f"rm -f {remote}/data/{database.name}-wal {remote}/data/{database.name}-shm")
            adb("push", str(database), remote + "/data/" + database.name)
            start_and_verify()
            after = Path(temporary) / "after"
            adb("pull", remote + "/data", str(after))
            with closing(sqlite3.connect(after / database.name)) as connection:
                if connection.execute("SELECT value FROM binary_release_smoke").fetchone() != ("persistent",):
                    raise AssertionError("Android SQLite restart persistence failed")
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise AssertionError("Android SQLite integrity check failed")
        print("Android Bionic DNS/HTTPS, SQLite and embedded frontend OK", flush=True)
    except Exception:
        print(shell(f"cat {remote}/memos.log 2>/dev/null || true"), flush=True)
        raise
    finally:
        try:
            stop()
        finally:
            adb("forward", "--remove", "tcp:15230")
            # The path has a fixed prefix and a locally generated hex UUID; no user data is removed.
            shell(f"rm -rf {remote}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("probe", type=Path)
    parser.add_argument("--serial", default="emulator-5554")
    args = parser.parse_args()
    smoke(args.directory, args.probe, args.serial)
