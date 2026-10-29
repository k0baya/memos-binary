# memos-binary

Build [Memos](https://github.com/usememos/memos) with the **complete frontend embedded in the executable**. No Node.js, separate `dist/` folder, or SQLite installation is needed to run a release.

Download from [Releases](https://github.com/k0baya/memos-binary/releases). This repository packages upstream releases; it does not modify the Memos application or its database migrations.

## Platforms and packages

| OS | Architectures | Packages |
| --- | --- | --- |
| Linux | amd64, arm64, armv7 | `memos-linux-<arch>.tar.gz` |
| Alpine / musl Linux | amd64, arm64, armv7 | `memos-linux-<arch>-musl.tar.gz` |
| FreeBSD | amd64, arm64 | `memos-freebsd-<arch>.tar.gz` |
| macOS | amd64 (Intel), arm64 (Apple Silicon) | `memos-darwin-<arch>.tar.gz` |
| Windows | amd64, arm64 | `memos-windows-<arch>.zip`, plus legacy `.tar.gz` |

Each release has 14 archives, `checksums.txt`, and `build-info.json`. Each archive contains the executable, upstream `LICENSE`, and `BUILD-INFO.json` identifying the upstream commit, Go toolchain, target, executable hash, and frontend digest. Existing amd64 `.tar.gz` names are retained for compatibility.

**musl details:** supported upstream versions use the pure-Go `modernc.org/sqlite` driver. All packages use `CGO_ENABLED=0`; Linux executables have no dynamic ELF loader or shared-library dependency. The musl-labelled archives intentionally contain the **same static executable** as the corresponding Linux archive, not a different CGO/musl-linked build. They are tested in Alpine without glibc compatibility packages. Use these on Alpine/musl, or the regular Linux archives on glibc systems.

armv7 requires ARMv7 hardware and the OS support required by the upstream Go/SQLite versions. There are no unsupported 32-bit x86 builds or CGO-disabled SQLite stubs. Runtime minimum OS/kernel versions follow the Go version recorded in `BUILD-INFO.json`; a FreeBSD VM pass is not proof that a particular shared-hosting service permits execution.

## Run

Linux / FreeBSD / macOS:

```sh
tar -xzf memos-linux-amd64.tar.gz # choose the archive for your actual OS/CPU
chmod +x memos
mkdir -p data
./memos --addr 127.0.0.1 --port 5230 --data "$PWD/data"
```

Windows PowerShell:

```powershell
Expand-Archive .\memos-windows-amd64.zip -DestinationPath .\memos
Set-Location .\memos
New-Item -ItemType Directory -Force .\data
.\memos.exe --addr 127.0.0.1 --port 5230 --data "$PWD\data"
```

Open `http://127.0.0.1:5230`. We explicitly select a writable data directory and port rather than relying on version-dependent defaults. Use a reverse proxy for public HTTPS; binding to loopback by default avoids unintentionally exposing a new instance.

### serv00 / PM2 (FreeBSD)

Use the **FreeBSD**, not Linux, archive. Check `uname -srm`, executable permissions, the hosting service's execution policy, available memory, and your allocated TCP port first. Try running the executable directly to obtain the real error before adding PM2.

Place `examples/ecosystem.config.cjs` **beside the extracted `memos` executable**, create `data/` there, and change `5230` to the port allocated by your host. The example uses `interpreter: "none"`, `exec_mode: "fork"`, an absolute executable/data path, and one process: PM2 must not interpret a Go executable as JavaScript or run multiple instances against the same SQLite database.

```sh
chmod +x memos
mkdir -p data
./memos --addr 127.0.0.1 --port YOUR_ALLOCATED_PORT --data "$PWD/data"
# Stop the foreground process before starting PM2:
pm2 start ecosystem.config.cjs
pm2 logs memos --lines 100
pm2 save
```

If direct startup fails, keep the complete error and `uname -srm` output. VM tests cannot reproduce serv00 account restrictions. Historical issue #4 was confirmed resolved by its reporter after an upgrade; no new unverified host-specific root cause is claimed here.

## Build or backfill another upstream version

In **Actions → Build → Run workflow**:

* `version`: `latest` (the latest **stable release**, not the first tag), or an exact published upstream release such as `v0.31.0`, `v0.25.3`, or `26.10.0-rc.2`.
* `publish`: off by default for artifact-only tests. Enable on `main` to create/update that release after **all** build and runtime checks pass. Backfills and prereleases do not replace the latest stable release.

Compatibility is determined from the selected source: its `go.mod`, pinned `packageManager` (or pnpm 10.24.0 for older tags without one), and actual frontend embed path. Both `server/router/frontend/dist` and `server/frontend/dist` are supported. Unknown layouts or non-pure-Go SQLite releases fail explicitly rather than silently publishing broken packages. This is not a promise that every historical tag supports every architecture.

**Check Updates** runs daily at 16:00 UTC (00:00 Asia/Shanghai). It checks the release assets in the **current repository**, and invokes the reusable build workflow if the latest stable release is absent or incomplete. No personal access token or hard-coded old `memos-freebsd` repository is used. GitHub may disable scheduled workflows in inactive repositories; re-enable it in Actions if necessary.

Pushes to `main` and `codex/**` that change the build/tooling run artifact-only builds. CI also validates tooling and workflows on pull requests. Failed runs are retained for diagnosis, not automatically deleted.

## Verification and upgrade safety

Publication is gated on:

* Native Linux amd64/arm64, Windows amd64/arm64, and macOS Intel/Apple Silicon startup tests.
* Alpine/musl startup tests on amd64/arm64 and emulated armv7, plus static ELF dependency checks.
* FreeBSD 14.4 amd64/arm64 VM startup tests.
* Each test starts the **extracted package from an empty working directory**, fetches the HTML and a SPA route, verifies every packaged `assets/` file against its SHA-256 hash (including lazy chunks, fonts and images), initializes SQLite, restarts, and verifies persistence and database integrity. No external frontend folder is available to mask missing embedded assets.
* A complete archive set with consistent source/frontend provenance and SHA-256 checksums.

The SQLite restart check uses a disposable database; it is **not** an existing-user database migration test. Back up your complete data directory and follow upstream upgrade/migration instructions before changing versions. Do not downgrade a migrated database. macOS/Windows packages are not code-signed; apply your normal download verification and OS security procedures.

For Linux/FreeBSD, verify downloads with `sha256sum -c checksums.txt` (or `sha256` on FreeBSD); on macOS use `shasum -a 256 -c checksums.txt`. Keep all listed files together for a full check, or compare just the checksum for your selected archive. On Windows, use `Get-FileHash -Algorithm SHA256` and compare with the corresponding line.

Local tooling tests (Python 3.10+, standard library only):

```sh
python -m unittest discover -s tests -v
python scripts/release.py resolve --version latest
python scripts/smoke.py /absolute/path/to/memos --version v0.31.0
```
