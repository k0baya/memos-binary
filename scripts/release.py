#!/usr/bin/env python3
"""Release tooling. Python standard library only; never modifies upstream code."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

UPSTREAM = "usememos/memos"
API = "https://api.github.com"
TAG = re.compile(r"v?\d+\.\d+(?:\.\d+)?(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?")
TARGETS = (
    "linux-amd64", "linux-arm64", "linux-armv7",
    "freebsd-amd64", "freebsd-arm64",
    "darwin-amd64", "darwin-arm64", "windows-amd64", "windows-arm64",
    "android-arm64", "android-amd64",
)
FRONTENDS = ("server/frontend/dist", "server/router/frontend/dist")


def api(path, *, data=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "memos-binary"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    request = Request(API + path, headers=headers, data=data)
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def optional_release(repository, tag):
    try:
        return api(f"/repos/{repository}/releases/tags/{quote(tag, safe='')}")
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def valid_tag(value):
    if not TAG.fullmatch(value):
        raise ValueError(f"Not an upstream release tag: {value!r}")
    return value


def outputs(values):
    for key, value in values.items():
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if "\n" in value or "\r" in value:
            raise ValueError("Multiline workflow output")
        print(f"{key}={value}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
                stream.write(f"{key}={value}\n")


def resolve(version):
    latest = api(f"/repos/{UPSTREAM}/releases/latest")
    release = latest if version == "latest" else optional_release(UPSTREAM, valid_tag(version))
    if not release or release["draft"]:
        raise ValueError(f"No published upstream release for {version!r}")
    tag = valid_tag(release["tag_name"])
    # A tag is resolved once, including annotated tags, then every job checks out this SHA.
    ref = api(f"/repos/{UPSTREAM}/git/ref/tags/{quote(tag, safe='')}")["object"]
    for _ in range(5):
        if ref["type"] == "commit":
            break
        if ref["type"] != "tag":
            raise ValueError("Release tag does not point to a commit")
        ref = api(f"/repos/{UPSTREAM}/git/tags/{ref['sha']}")["object"]
    if ref["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", ref["sha"]):
        raise ValueError("Invalid upstream commit")
    return {"tag": tag, "sha": ref["sha"], "prerelease": release["prerelease"],
            "latest": tag == latest["tag_name"] and not release["prerelease"]}


def inspect_source(source):
    package = json.loads((source / "web/package.json").read_text(encoding="utf-8"))
    manager = package.get("packageManager", "pnpm@10.24.0")
    match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(?:\+sha\d+\.[a-f0-9]+)?", manager)
    if not match:
        raise ValueError(f"Unsupported package manager: {manager!r}")
    # Follow the embed declaration, not a hard-coded historical frontend location.
    locations = [path for path in FRONTENDS
                 if any("//go:embed dist/" in file.read_text(encoding="utf-8")
                        for file in (source / path).parent.glob("*.go"))]
    if len(locations) != 1 or "release" not in package.get("scripts", {}):
        raise ValueError("Cannot identify upstream's frontend release/embedding layout")
    if "modernc.org/sqlite" not in (source / "go.mod").read_text(encoding="utf-8"):
        raise ValueError("This release requires a different SQLite build strategy; refusing a CGO stub")
    return {"frontend": locations[0], "pnpm": match[1]}


def frontend_files(directory):
    index = (directory / "index.html").read_text(encoding="utf-8")
    if "<html" not in index.lower() or "/src/" in index or "No embeddable frontend" in index:
        raise ValueError("Missing production frontend")
    assets = list((directory / "assets").rglob("*"))
    if not any(p.suffix == ".js" for p in assets) or not any(p.suffix == ".css" for p in assets):
        raise ValueError("Frontend must contain compiled JavaScript and CSS")
    files = {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(directory.rglob("*")) if p.is_file()}
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("Unexpected frontend symlink")
    return files


def audit_binary(binary, target):
    if not target.startswith(("linux-", "android-")):
        return
    data = binary.read_bytes()
    if data[:4] != b"\x7fELF" or data[5] != 1:
        raise ValueError("Expected a little-endian ELF executable")
    bits = data[4]
    if bits == 2:
        offset = struct.unpack_from("<Q", data, 32)[0]
        size, count = struct.unpack_from("<HH", data, 54)
        headers = [struct.unpack_from("<IIQQQQQQ", data, offset + i * size) for i in range(count)]
        segments = [(t, o, a, f, al) for t, _, o, a, _, f, _, al in headers]
        dynamic_format, dynamic_size = "<qQ", 16
    elif bits == 1:
        offset = struct.unpack_from("<I", data, 28)[0]
        size, count = struct.unpack_from("<HH", data, 42)
        headers = [struct.unpack_from("<IIIIIIII", data, offset + i * size) for i in range(count)]
        segments = [(t, o, a, f, al) for t, o, a, _, f, _, _, al in headers]
        dynamic_format, dynamic_size = "<iI", 8
    else:
        raise ValueError("Unsupported ELF class")
    interpreter = None
    needed = []
    for kind, offset, _, length, _ in segments:
        if kind == 3:
            interpreter = data[offset:offset + length].rstrip(b"\0").decode("ascii")
        if kind == 2:
            entries = [struct.unpack_from(dynamic_format, data, i)
                       for i in range(offset, offset + length, dynamic_size)]
            address = next((value for tag, value in entries if tag == 5), None)
            if address is not None:
                strings = next(o + address - a for t, o, a, n, _ in segments if t == 1 and a <= address < a + n)
                needed.extend(data[strings + value:data.index(b"\0", strings + value)].decode("ascii")
                              for tag, value in entries if tag == 1)
    if target.startswith("linux-"):
        if interpreter or needed:
            raise ValueError(f"Linux binary is not static: interpreter={interpreter}, libraries={needed}")
    else:
        machine = struct.unpack_from("<H", data, 18)[0]
        expected_machine = 183 if target == "android-arm64" else 62
        if bits != 2 or machine != expected_machine or struct.unpack_from("<H", data, 16)[0] != 3:
            raise ValueError("Android binary must be a native 64-bit PIE of the correct architecture")
        if interpreter != "/system/bin/linker64" or "libc.so" not in needed:
            raise ValueError("Android build must use the system Bionic loader and libc for system DNS")
        if set(needed) - {"libc.so", "libdl.so", "liblog.so", "libm.so"}:
            raise ValueError(f"Unexpected non-system Android dependency: {needed}")
        if any(alignment < 16384 for kind, _, _, _, alignment in segments if kind == 1):
            raise ValueError("Android ELF load segments must support 16 KiB pages")
    print(f"ELF audit {target}: interpreter={interpreter}, needed={needed}")


def build(source, target, tag, sha, destination):
    if target not in TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    layout = inspect_source(source)
    frontend_files(source / layout["frontend"])
    goos, arch = target.split("-")
    env = dict(os.environ, GOOS=goos, GOARCH="arm" if arch == "armv7" else arch,
               CGO_ENABLED="0", GOARM="7" if arch == "armv7" else "")
    tags = "netgo,osusergo"
    build_flags = []
    if goos == "android":
        if not env.get("CC"):
            raise ValueError("Android needs an NDK API 26+ compiler in CC for the Bionic system DNS resolver")
        env["CGO_ENABLED"] = "1"
        tags = "netcgo"
        build_flags = ["-buildmode=pie"]
    flags = ["-s", "-w", f"-X=github.com/usememos/memos/internal/version.Version={valid_tag(tag).removeprefix('v')}"]
    if goos == "android":
        flags += ["-linkmode=external", "-extldflags=-Wl,-z,max-page-size=16384"]
    version_source = (source / "internal/version/version.go").read_text(encoding="utf-8")
    for symbol, value in (("Commit", sha), ("DevVersion", tag.removeprefix("v"))):
        if re.search(r"\bvar\s+" + symbol + r"\b", version_source):
            flags.append(f"-X=github.com/usememos/memos/internal/version.{symbol}={value}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["go", "build", *build_flags, "-mod=readonly", "-trimpath", "-buildvcs=false", "-tags=" + tags,
                    "-ldflags=" + " ".join(flags), "-o", str(destination.resolve()), "./cmd/memos"],
                   cwd=source, env=env, check=True)
    audit_binary(destination, target)


def package_names(target):
    names = [f"memos-{target}.tar.gz"]
    if target.startswith("windows-"):
        names.append(f"memos-{target}.zip")
    if target.startswith("linux-"):
        names.append(f"memos-{target}-musl.tar.gz")
    return names


def expected_assets():
    return {name for target in TARGETS for name in package_names(target)} | {"checksums.txt", "build-info.json"}


def package(source, target, binary, destination, tag, sha):
    if target not in TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    destination.mkdir(parents=True, exist_ok=True)
    files = frontend_files(source / inspect_source(source)["frontend"])
    metadata = {"schema": 1, "upstream": UPSTREAM, "tag": valid_tag(tag), "commit": sha,
                "target": target, "cgo_enabled": target.startswith("android-"), "frontend_files": len(files),
                "frontend_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                "frontend_assets": {name: digest for name, digest in files.items() if name.startswith("assets/")},
                "go": subprocess.check_output(["go", "version"], cwd=source, text=True).strip(),
                "build_url": f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'k0baya/memos-binary')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    if target.startswith("android-"):
        metadata.update(android_api=26, android_runtime="system Bionic; not musl/glibc",
                        android_arm64_device_verified=False,
                        android_runtime_test="x86_64 emulator; ARM64 physical device verification pending")
    staging = destination / target
    staging.mkdir(exist_ok=True)
    executable = staging / ("memos.exe" if target.startswith("windows-") else "memos")
    shutil.copyfile(binary, executable)
    executable.chmod(0o755)
    shutil.copyfile(source / "LICENSE", staging / "LICENSE")
    (staging / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    for name in package_names(target):
        if name.endswith(".zip"):
            with zipfile.ZipFile(destination / name, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in sorted(staging.iterdir()):
                    archive.write(file, file.name)
        else:
            with tarfile.open(destination / name, "w:gz") as archive:
                for file in sorted(staging.iterdir()):
                    info = archive.gettarinfo(str(file), arcname=file.name)
                    # Windows chmod cannot express POSIX execute bits. Set archive modes explicitly.
                    info.mode = 0o755 if file == executable else 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with file.open("rb") as stream:
                        archive.addfile(info, stream)
    # Keep the per-target provenance separate for release aggregation, not in artifact subdirectories.
    (destination / f"{target}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(staging)


def checksums(directory):
    archives = sorted(p for p in directory.iterdir() if p.name.endswith((".tar.gz", ".zip")))
    expected = expected_assets() - {"checksums.txt", "build-info.json"}
    if {p.name for p in archives} != expected:
        raise ValueError("Incomplete release archive set")
    metadata = [json.loads((directory / f"{target}.json").read_text(encoding="utf-8")) for target in TARGETS]
    for key in ("tag", "commit", "frontend_sha256"):
        if len({m[key] for m in metadata}) != 1:
            raise ValueError(f"Mixed build provenance: {key}")
    (directory / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    files = archives + [directory / "build-info.json"]
    (directory / "checksums.txt").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files), encoding="utf-8")


def check_update(repository):
    info = resolve("latest")
    release = optional_release(repository, info["tag"])
    names = {a["name"] for a in release["assets"]} if release else set()
    outputs({"tag": info["tag"], "needed": not release or release.get("draft", False)
             or not expected_assets().issubset(names)})


def publish(repository, directory, tag, commit, prerelease, latest):
    # Only the release job has a write token. No token is passed to upstream build scripts.
    release = optional_release(repository, tag)
    if release is None:
        release = api(f"/repos/{repository}/releases", data={
            "tag_name": valid_tag(tag), "target_commitish": commit, "name": tag,
            "draft": True,
            "prerelease": prerelease, "make_latest": "false",
            "body": f"Memos [{tag}](https://github.com/{UPSTREAM}/releases/tag/{tag}) with the complete embedded frontend.\n\n"
                    "Linux packages are static pure-Go binaries; the musl-labelled packages contain the same executable, tested on Alpine. "
                    "Windows ZIPs and legacy tar.gz archives are both available.\n\n"
                    "See README for platform requirements and PM2/serv00 instructions. Check `checksums.txt` and `build-info.json` before installing. "
                    "Back up your database and follow upstream migration notes before upgrading."})
    # Upload the checksum manifest last. New releases remain drafts until every upload succeeds.
    paths = [str(directory / name) for name in sorted(expected_assets() - {"checksums.txt"})]
    paths.append(str(directory / "checksums.txt"))
    subprocess.run(["gh", "release", "upload", tag, "--repo", repository, "--clobber", *paths], check=True)
    if release["draft"]:
        subprocess.run(["gh", "release", "edit", tag, "--repo", repository, "--draft=false",
                        f"--prerelease={str(prerelease).lower()}", f"--latest={str(latest).lower()}"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("resolve", "inspect", "frontend", "build", "package", "checksums", "check-update", "publish"))
    parser.add_argument("--version", default="latest")
    parser.add_argument("--source", type=Path, default=Path("upstream"))
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    parser.add_argument("--binary", type=Path, default=Path("build/memos"))
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--tag")
    parser.add_argument("--sha")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "k0baya/memos-binary"))
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()
    if args.command == "resolve":
        outputs(resolve(args.version))
    elif args.command == "inspect":
        outputs(inspect_source(args.source))
    elif args.command == "frontend":
        print(f"Validated {len(frontend_files(args.directory))} frontend files")
    elif args.command == "build":
        build(args.source, args.target, args.tag, args.sha, args.binary)
    elif args.command == "package":
        package(args.source, args.target, args.binary, args.directory, args.tag, args.sha)
    elif args.command == "checksums":
        checksums(args.directory)
    elif args.command == "check-update":
        check_update(args.repository)
    elif args.command == "publish":
        publish(args.repository, args.directory, args.tag, args.sha, args.prerelease, args.latest)


if __name__ == "__main__":
    main()
