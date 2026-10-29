"""Exercise the Magisk service example in an isolated BusyBox container, not on a phone."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, build_opener
import uuid

from scripts.smoke import verify_frontend


def smoke(package):
    name = "memos-magisk-smoke-" + uuid.uuid4().hex

    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True, timeout=180).strip()

    def shell(command):
        return docker("exec", name, "sh", "-c", command)

    def start():
        shell("PATH=/test-bin:$PATH sh /module/service.sh")
        deadline = time.monotonic() + 60
        while True:
            try:
                with http.open(base + "/", timeout=3) as response:
                    index = response.read().decode()
                verify_frontend(http, base, index, metadata)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("Magisk service example did not start")
                time.sleep(1)

    def stop():
        shell('p=$(cat /data/adb/memos/memos.pid); '
              '[ "$(readlink /proc/$p/exe)" = /module/bin/memos ] && kill -TERM "$p"')
        for _ in range(40):
            if not shell('p=$(cat /data/adb/memos/memos.pid); readlink /proc/$p/exe 2>/dev/null || true'):
                return
            time.sleep(0.25)
        raise AssertionError("Magisk service did not terminate cleanly")

    metadata = json.loads((package / "BUILD-INFO.json").read_text())
    http = build_opener(ProxyHandler({}))
    base = "http://127.0.0.1:5230"
    with tempfile.TemporaryDirectory(prefix="memos-magisk-fixture-") as temporary:
        module = Path(temporary)
        (module / "bin").mkdir()
        shutil.copyfile(package / "memos", module / "bin/memos")
        (module / "bin/memos").chmod(0o755)
        shutil.copyfile("examples/magisk/service.sh", module / "service.sh")
        try:
            docker("run", "-d", "--rm", "--name", name, "--network", "host",
                   "-v", f"{module}:/module:ro", "python:3.13-alpine3.22", "sleep", "600")
            shell("mkdir /test-bin; printf '#!/bin/sh\nprintf 1\n' >/test-bin/getprop; chmod 755 /test-bin/getprop")
            start()
            first_pid = shell("cat /data/adb/memos/memos.pid")
            # The lock must be released by the script, not inherited by the daemon.
            shell("flock -n /data/adb/memos/start.lock true")
            shell("PATH=/test-bin:$PATH sh /module/service.sh")
            if shell("cat /data/adb/memos/memos.pid") != first_pid:
                raise AssertionError("Magisk example launched a duplicate process")
            if shell("stat -c %a /data/adb/memos") != "700":
                raise AssertionError("Magisk data directory is not private")
            stop()
            shell("python -c \"import sqlite3; c=sqlite3.connect('/data/adb/memos/memos_prod.db'); "
                  "c.execute('CREATE TABLE module_smoke (value TEXT)'); "
                  "c.execute('INSERT INTO module_smoke VALUES (123)'); c.commit(); c.close()\"")
            start()
            result = shell("python -c \"import sqlite3; c=sqlite3.connect('/data/adb/memos/memos_prod.db'); "
                           "print(c.execute('SELECT value FROM module_smoke').fetchone()[0]); c.close()\"")
            if result != "123":
                raise AssertionError("Magisk service lost data across restart")
            stop()
            print("BusyBox service lock, duplicate prevention, private data and restart persistence OK (not physical Magisk validation)")
        except Exception:
            print(shell("cat /data/adb/memos/memos.log 2>/dev/null || true"), flush=True)
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", name], check=False, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    smoke(parser.parse_args().package)
