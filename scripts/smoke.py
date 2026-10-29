#!/usr/bin/env python3
"""Exercise the packaged binary, SQLite and embedded assets from an empty cwd."""

import argparse
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, build_opener


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = attrs.get("src" if tag == "script" else "href", "")
        if tag in ("script", "link") and urlparse(value).path.endswith((".js", ".css")):
            if urlparse(value).scheme or urlparse(value).netloc:
                raise AssertionError(f"Unexpected externally hosted application asset: {value}")
            self.paths.add(value)


def verify_frontend(http, base, index, metadata):
    if "<html" not in index.lower() or "No embeddable frontend" in index or "/src/" in index:
        raise AssertionError("Missing production frontend")
    parser = Assets()
    parser.feed(index)
    for suffix in (".js", ".css"):
        if not any(urlparse(path).path.endswith(suffix) for path in parser.paths):
            raise AssertionError(f"No embedded {suffix} asset in index.html")
    asset_hashes = {"/" + name: digest for name, digest in metadata.get("frontend_assets", {}).items()}
    for path in sorted(parser.paths | asset_hashes.keys()):
        with http.open(urljoin(base + "/", path), timeout=30) as response:
            body = response.read()
            mime = response.headers.get("Content-Type", "")
        if not body or "text/html" in mime or body.lstrip().lower().startswith(b"<!doctype html"):
            raise AssertionError(f"Missing embedded asset (SPA fallback): {path}")
        if path in asset_hashes and hashlib.sha256(body).hexdigest() != asset_hashes[path]:
            raise AssertionError(f"Embedded asset hash mismatch: {path}")
    with http.open(base + "/auth", timeout=10) as response:
        if "<html" not in response.read().decode("utf-8").lower():
            raise AssertionError("SPA fallback failed")
    print(f"Embedded frontend and {len(parser.paths | asset_hashes.keys())} assets OK", flush=True)


def smoke(binary, expected_version=None):
    binary = binary.resolve()
    metadata_path = binary.parent / "BUILD-INFO.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    if metadata and hashlib.sha256(binary.read_bytes()).hexdigest() != metadata["binary_sha256"]:
        raise AssertionError("Executable does not match package provenance")
    help_text = subprocess.check_output([str(binary), "--help"], text=True, timeout=60)
    http = build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="memos-smoke-") as temporary:
        root = Path(temporary)
        data = root / "data"
        data.mkdir()
        log = root / "memos.log"

        def run_once():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            command = [str(binary), "--addr=127.0.0.1", f"--port={port}", f"--data={data}", "--driver=sqlite"]
            if "--mode" in help_text:
                command.append("--mode=prod")
            with log.open("ab") as output:
                process = subprocess.Popen(command, cwd=root, stdout=output, stderr=output)
                try:
                    deadline = time.monotonic() + 180
                    while True:
                        if process.poll() is not None:
                            raise AssertionError(f"Binary exited with {process.returncode}")
                        try:
                            with http.open(base + "/", timeout=5) as response:
                                index = response.read().decode("utf-8")
                                content_type = response.headers.get("Content-Type", "")
                            if "text/html" not in content_type or "<html" not in index.lower():
                                raise AssertionError("Root route did not return the frontend")
                            if "No embeddable frontend" in index or "/src/" in index:
                                raise AssertionError("Served a placeholder/development frontend")
                            break
                        except (OSError, TimeoutError):
                            if time.monotonic() >= deadline:
                                raise AssertionError("Timed out waiting for the frontend")
                            time.sleep(1)
                    verify_frontend(http, base, index, metadata)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)

        try:
            run_once()
            databases = list(data.glob("*.db"))
            if len(databases) != 1:
                raise AssertionError(f"Expected one SQLite database, found {databases}")
            with closing(sqlite3.connect(databases[0])) as connection, connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "memo" not in tables:
                    raise AssertionError("SQLite schema was not initialized")
                connection.execute("CREATE TABLE binary_release_smoke (value TEXT)")
                connection.execute("INSERT INTO binary_release_smoke VALUES ('persistent')")
            run_once()
            with closing(sqlite3.connect(databases[0])) as connection:
                if connection.execute("SELECT value FROM binary_release_smoke").fetchone() != ("persistent",):
                    raise AssertionError("SQLite data did not persist across restart")
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise AssertionError("SQLite integrity check failed")
            if expected_version and expected_version.removeprefix("v") not in log.read_text(encoding="utf-8", errors="replace"):
                raise AssertionError("Startup log does not identify the expected version")
            print("SQLite initialization, restart persistence and integrity OK", flush=True)
        except Exception:
            print(log.read_text(encoding="utf-8", errors="replace"), flush=True)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args()
    smoke(args.binary, args.version)
