import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import tarfile
import zipfile

from scripts import release
from scripts.smoke import Assets


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.dist = self.root / "dist"
        self.sha = "a" * 40

    def source_fixture(self, frontend="server/frontend/dist", manager="pnpm@11.0.1"):
        (self.source / "web").mkdir(exist_ok=True)
        package = {"scripts": {"release": "vite build --mode release"}}
        if manager:
            package["packageManager"] = manager
        (self.source / "web/package.json").write_text(json.dumps(package))
        (self.source / "go.mod").write_text("go 1.27.0\nrequire modernc.org/sqlite v1.56.0\n")
        (self.source / "LICENSE").write_text("upstream license")
        directory = self.source / frontend
        directory.mkdir(parents=True, exist_ok=True)
        (directory.parent / "frontend.go").write_text("//go:embed dist/*\n")
        (directory / "assets").mkdir(exist_ok=True)
        (directory / "index.html").write_text('<html><script src="/assets/app.js"></script><link href="/assets/app.css"></html>')
        (directory / "assets/app.js").write_text("console.log('complete frontend')")
        (directory / "assets/app.css").write_text("body{color:black}")
        return directory

    def test_valid_tags_include_calendar_prereleases(self):
        for tag in ("v0.31.0", "v0.25.3", "26.10.0-rc.2", "26.10-rc.1"):
            with self.subTest(tag=tag):
                self.assertEqual(release.valid_tag(tag), tag)

    def test_rejects_shell_injection_and_branch_inputs(self):
        for tag in ("main", "latest;id", "v1.2.3\nx=y", "../v1.2.3", "$(id)", "v1.2.3 "):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.valid_tag(tag)

    @patch("scripts.release.api")
    def test_resolve_latest_once_and_dereference_annotated_tag(self, api):
        api.side_effect = [
            {"tag_name": "v0.31.0", "draft": False, "prerelease": False},
            {"object": {"type": "tag", "sha": "b" * 40}},
            {"object": {"type": "commit", "sha": self.sha}},
        ]
        result = release.resolve("latest")
        self.assertEqual(result, {"tag": "v0.31.0", "sha": self.sha, "prerelease": False, "latest": True})
        self.assertEqual(api.call_count, 3)

    @patch("scripts.release.api")
    def test_older_version_does_not_become_latest(self, api):
        api.side_effect = [
            {"tag_name": "v0.31.0"},
            {"tag_name": "v0.25.3", "draft": False, "prerelease": False},
            {"object": {"type": "commit", "sha": self.sha}},
        ]
        self.assertFalse(release.resolve("v0.25.3")["latest"])

    @patch("scripts.release.api")
    def test_explicit_prerelease_is_not_latest(self, api):
        api.side_effect = [
            {"tag_name": "v0.31.0"},
            {"tag_name": "26.10.0-rc.2", "draft": False, "prerelease": True},
            {"object": {"type": "commit", "sha": self.sha}},
        ]
        self.assertTrue(release.resolve("26.10.0-rc.2")["prerelease"])

    @patch("scripts.release.api")
    def test_missing_release_is_not_a_buildable_version(self, api):
        api.side_effect = [{"tag_name": "v0.31.0"}, HTTPError("url", 404, "missing", {}, None)]
        with self.assertRaises(ValueError):
            release.resolve("v0.99.0")

    @patch("scripts.release.api")
    def test_authorization_failures_are_not_missing_releases(self, api):
        api.side_effect = HTTPError("url", 403, "forbidden", {}, None)
        with self.assertRaises(HTTPError):
            release.optional_release("owner/repo", "v0.31.0")

    def test_current_embed_path_and_pinned_package_manager(self):
        self.source_fixture()
        self.assertEqual(release.inspect_source(self.source), {"frontend": "server/frontend/dist", "pnpm": "11.0.1"})

    def test_legacy_embed_path_and_explicit_fallback_package_manager(self):
        self.source_fixture("server/router/frontend/dist", manager=None)
        self.assertEqual(release.inspect_source(self.source), {"frontend": "server/router/frontend/dist", "pnpm": "10.24.0"})

    def test_ambiguous_embedding_is_rejected(self):
        self.source_fixture()
        self.source_fixture("server/router/frontend/dist")
        with self.assertRaises(ValueError):
            release.inspect_source(self.source)

    def test_unknown_sqlite_driver_is_rejected(self):
        self.source_fixture()
        (self.source / "go.mod").write_text("require github.com/mattn/go-sqlite3 v1.0.0")
        with self.assertRaises(ValueError):
            release.inspect_source(self.source)

    def test_unpinned_package_manager_is_rejected(self):
        self.source_fixture(manager="pnpm@latest")
        with self.assertRaises(ValueError):
            release.inspect_source(self.source)

    def test_complete_frontend_hashes_every_file(self):
        directory = self.source_fixture()
        self.assertEqual(len(release.frontend_files(directory)), 3)

    def test_development_frontend_is_rejected(self):
        directory = self.source_fixture()
        (directory / "index.html").write_text('<html><script src="/src/main.tsx"></script></html>')
        with self.assertRaises(ValueError):
            release.frontend_files(directory)

    def test_missing_css_is_rejected(self):
        directory = self.source_fixture()
        (directory / "assets/app.css").unlink()
        with self.assertRaises(ValueError):
            release.frontend_files(directory)

    @patch("scripts.release.subprocess.run")
    def test_armv7_build_uses_real_sqlite_no_cgo_and_package_entry_point(self, run):
        self.source_fixture()
        version = self.source / "internal/version"
        version.mkdir(parents=True)
        (version / "version.go").write_text('var Version = "dev"\nvar Commit = "unknown"\n')
        release.build(self.source, "linux-armv7", "v0.31.0", self.sha, self.root / "binary")
        args, kwargs = run.call_args
        self.assertEqual(kwargs["env"]["CGO_ENABLED"], "0")
        self.assertEqual(kwargs["env"]["GOARM"], "7")
        self.assertEqual(kwargs["env"]["GOARCH"], "arm")
        self.assertEqual(args[0][-1], "./cmd/memos")
        self.assertIn(self.sha, " ".join(args[0]))

    @patch("scripts.release.subprocess.check_output", return_value="go version go1.27.0 linux/amd64")
    def test_all_packages_include_license_binary_and_provenance(self, _go):
        self.source_fixture()
        binary = self.root / "binary"
        binary.write_bytes(b"fake-test-executable")
        for target in release.TARGETS:
            release.package(self.source, target, binary, self.dist, "v0.31.0", self.sha)
            name = "memos.exe" if target.startswith("windows-") else "memos"
            for archive_name in release.package_names(target):
                with self.subTest(archive=archive_name):
                    path = self.dist / archive_name
                    if archive_name.endswith(".zip"):
                        with zipfile.ZipFile(path) as archive:
                            self.assertEqual(set(archive.namelist()), {name, "LICENSE", "BUILD-INFO.json"})
                            self.assertEqual(archive.read(name), binary.read_bytes())
                    else:
                        with tarfile.open(path) as archive:
                            self.assertEqual(set(archive.getnames()), {name, "LICENSE", "BUILD-INFO.json"})
                            self.assertEqual(archive.extractfile(name).read(), binary.read_bytes())
                            self.assertEqual(archive.getmember(name).mode & 0o777, 0o755)
        release.checksums(self.dist)
        self.assertTrue(release.expected_assets().issubset({p.name for p in self.dist.iterdir()}))
        metadata = json.loads((self.dist / "build-info.json").read_text())
        self.assertEqual(len(metadata), 9)
        for row in metadata:
            self.assertEqual(row["frontend_files"], 3)
            self.assertFalse(row["cgo_enabled"])
        for line in (self.dist / "checksums.txt").read_text().splitlines():
            checksum, name = line.split("  ")
            self.assertEqual(checksum, hashlib.sha256((self.dist / name).read_bytes()).hexdigest())
        # Mixing upstream versions or frontend builds must never produce a publishable set.
        path = self.dist / "linux-amd64.json"
        row = json.loads(path.read_text())
        row["commit"] = "c" * 40
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, "Mixed build provenance"):
            release.checksums(self.dist)

    def test_incomplete_archive_set_cannot_be_published(self):
        self.dist.mkdir()
        (self.dist / "memos-linux-amd64.tar.gz").write_bytes(b"incomplete")
        with self.assertRaises(ValueError):
            release.checksums(self.dist)

    @patch("scripts.release.outputs")
    @patch("scripts.release.optional_release")
    @patch("scripts.release.resolve", return_value={"tag": "v0.31.0"})
    def test_update_checker_requires_complete_local_release(self, _resolve, get_release, output):
        for names, expected in (([], True), (["memos-linux-amd64.tar.gz"], True), (release.expected_assets(), False)):
            with self.subTest(expected=expected):
                get_release.return_value = {"assets": [{"name": name} for name in names]}
                release.check_update("custom/fork")
                get_release.assert_called_with("custom/fork", "v0.31.0")
                self.assertEqual(output.call_args.args[0]["needed"], expected)

    @patch("scripts.release.subprocess.run")
    @patch("scripts.release.api")
    @patch("scripts.release.optional_release", return_value=None)
    def test_backfill_never_changes_latest_release(self, _existing, api, run):
        release.publish("custom/fork", self.dist, "v0.25.3", self.sha, False, False)
        self.assertEqual(api.call_args.kwargs["data"]["make_latest"], "false")
        self.assertIn("--clobber", run.call_args.args[0])

    def test_smoke_parser_rejects_remote_assets(self):
        with self.assertRaises(AssertionError):
            Assets().feed('<script src="https://cdn.example/app.js"></script>')

    def test_smoke_parser_finds_production_script_and_css(self):
        parser = Assets()
        parser.feed('<script type="module" src="/assets/a.js"></script><link rel="stylesheet" href="/assets/a.css">')
        self.assertEqual(parser.paths, {"/assets/a.js", "/assets/a.css"})


if __name__ == "__main__":
    unittest.main()
