"""Contract tests for the build pipeline.

All Flutter/Dart/NDK/Android expectations are derived from ``build.toml`` at
test time, so these tests survive a version bump without edits. On a Flutter
release bump (a ``build.toml`` bump that syncs the
repo), the flutter-version-dependent tests below self-adjust; the patch/hunk
contract tests are intentionally tag-agnostic and must keep passing. If a
patch contract intentionally changes, update the assertion deliberately rather
than pinning a version literal.
"""

import json
import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

import build
import package
import utils


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.android_ndk = os.environ.get("ANDROID_NDK")
        os.environ["ANDROID_NDK"] = "/tmp/android-ndk"
        self.instance = build.Build()

    def tearDown(self):
        if self.android_ndk is None:
            os.environ.pop("ANDROID_NDK", None)
        else:
            os.environ["ANDROID_NDK"] = self.android_ndk

    @patch("build.subprocess.run")
    def test_build_requests_packaged_targets(self, run):
        self.instance.build(arch="arm64", mode="debug", jobs=42)

        cmd = run.call_args.args[0]

        self.assertIn("flutter", cmd)
        self.assertIn("flutter/build/archives:artifacts", cmd)
        self.assertIn("flutter/build/archives:dart_sdk_archive", cmd)
        self.assertIn("flutter/build/archives:flutter_patched_sdk", cmd)
        self.assertIn("flutter/shell/platform/linux:flutter_gtk", cmd)
        self.assertIn("flutter/tools/font_subset", cmd)
        self.assertIn("-j42", cmd)

    @patch("build.subprocess.run")
    def test_configure_uses_configured_api_and_termux_stub_includes(self, run):
        self.instance.api = 29

        self.instance.configure(arch="arm64", mode="debug")

        cmd = run.call_args.args[0]
        gn_args = [cmd[index + 1] for index, value in enumerate(cmd[:-1]) if value == "--gn-args"]
        stubs = build.termux_stubs_dir()
        vulkan = f"-I{build.ndk_vulkan_include(self.instance.toolchain)}"

        self.assertIn("termux_api_level=29", gn_args)
        self.assertIn('extra_ldflags=["-lEGL", "-lGLESv2", "-llog"]', gn_args)
        self.assertNotIn("dart_support_perfetto=false", gn_args)
        self.assertIn(
            f'extra_cflags=["{vulkan}", "-I{stubs}", "-D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__"]',
            gn_args,
        )
        self.assertIn(
            f'extra_cflags_cc=["{vulkan}", "-I{stubs}", "-D__ANDROID_UNAVAILABLE_SYMBOLS_ARE_WEAK__", "-Wno-newline-eof"]',
            gn_args,
        )

    def test_default_build_modes_cover_packaged_variants(self):
        with open(self.instance.conf, "rb") as f:
            runtime = tomllib.load(f)["build"].get("runtime")
        self.assertEqual(self.instance.mode, runtime or ["debug"])

    def test_output_uses_package_version_suffix(self):
        expected_version = self.instance.package_version  # snapshot-pinned via utils.deb_version
        output_name = Path(self.instance.output("arm64")).name
        self.assertEqual(output_name, f"flutter_{expected_version}_aarch64.deb")

    @staticmethod
    def _make_checkout(path, branch="main", tag=None):
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", branch, path], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                path,
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "x",
            ],
            check=True,
        )
        if tag:
            subprocess.run(
                [
                    "git",
                    "-C",
                    path,
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "user.name=t",
                    "tag",
                    "-a",
                    tag,
                    "-m",
                    "test",
                ],
                check=True,
            )

    @patch("build.git.Repo.clone_from")
    def test_clone_skips_when_main_branch_matches(self, clone_from):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "flutter")
            self._make_checkout(out, branch="main")
            self.instance.clone(url="https://example.com/flutter.git", tag="main", out=out)
            clone_from.assert_not_called()

    @patch("build.git.Repo.clone_from")
    def test_clone_reclones_when_stable_checkout_and_main_tag(self, clone_from):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "flutter")
            self._make_checkout(out, branch="stable", tag="3.47.5")
            self.instance.clone(url="https://example.com/flutter.git", tag="main", out=out)
            clone_from.assert_called_once()

    @patch("build.git.Repo.clone_from")
    def test_clone_skips_when_stable_tag_matches(self, clone_from):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "flutter")
            self._make_checkout(out, branch="stable", tag="3.47.5")
            self.instance.clone(url="https://example.com/flutter.git", tag="3.47.5", out=out)
            clone_from.assert_not_called()

    @patch("build.git.Repo.clone_from")
    def test_clone_is_shallow_by_default(self, clone_from):
        """The packaged SDK never ships .git, so the clone keeps one commit."""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "flutter")
            self.instance.clone(url="https://example.com/flutter.git", tag=self.instance.tag, out=out)
            _, kwargs = clone_from.call_args
            self.assertEqual(kwargs["depth"], 1)
            self.assertTrue(kwargs["single_branch"])

    @patch("build.git.Repo.clone_from")
    def test_clone_depth_zero_keeps_full_history(self, clone_from):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "flutter")
            self.instance.clone(
                url="https://example.com/flutter.git",
                tag=self.instance.tag,
                out=out,
                depth=0,
            )
            _, kwargs = clone_from.call_args
            self.assertNotIn("depth", kwargs)
            self.assertNotIn("single_branch", kwargs)

    def test_dart_patches_are_kept_in_sync(self):
        patch_dir = os.path.join(os.path.dirname(__file__), "patches")
        dart_path = os.path.join(patch_dir, "dart.patch")
        new_path = os.path.join(patch_dir, "dart.new.patch")
        # dart.new.patch is a symlink to dart.patch (approved); accept both
        # a real symlink and a plain duplicate for Windows-checkout compat.
        if os.path.islink(new_path):
            self.assertEqual(os.readlink(new_path), "dart.patch")
        with open(dart_path, encoding="utf-8") as f:
            dart_patch = f.read()
        with open(new_path, encoding="utf-8") as f:
            dart_new_patch = f.read()

        self.assertEqual(dart_patch, dart_new_patch)
        self.assertIn(
            '+  ProcessResult result = Process.runSync("/data/data/com.termux/files/usr/bin/sh", [',
            dart_patch,
        )
        self.assertIn("+#if defined(DART_HOST_OS_ANDROID) && defined(__TERMUX__)", dart_patch)
        self.assertIn("+      return '/data/data/com.termux/files/usr/bin/sh';", dart_patch)

    def test_engine_patch_contains_termux_build_fixes(self):
        patch_file = os.path.join(os.path.dirname(__file__), "patches", "engine.patch")
        with open(patch_file, encoding="utf-8") as f:
            patch_contents = f.read()

        self.assertIn('"-Wno-unknown-warning-option"', patch_contents)
        self.assertIn('"-llog"', patch_contents)
        self.assertIn('+config("sdk") {', patch_contents)
        self.assertIn(
            '+  if (current_toolchain == "//build/toolchain/termux:${current_cpu}") {',
            patch_contents,
        )
        self.assertIn("+    cflags = []", patch_contents)
        self.assertIn('+      "-Wl,-rpath=/data/data/com.termux/files/usr/lib",', patch_contents)
        self.assertIn('+      "-llog",', patch_contents)
        self.assertIn('+    configs = [ "//build/config/linux:sdk" ]', patch_contents)
        self.assertIn(
            '+} else if (current_toolchain == default_toolchain && is_termux && custom_sysroot != "") {',
            patch_contents,
        )
        self.assertIn("#if defined(__TERMUX__)", patch_contents)
        self.assertIn(
            "diff --git a/engine/src/flutter/shell/platform/linux/fl_view_accessible.cc",
            patch_contents,
        )
        self.assertIn("+    if (defined(invoker.libs)) {", patch_contents)
        self.assertIn("+      libs += invoker.libs", patch_contents)
        self.assertIn('+      libs = [ "vk_swiftshader" ]', patch_contents)
        fl_view_accessible_hunk = re.search(
            r"\+\+\+ b/engine/src/flutter/shell/platform/linux/fl_view_accessible\.cc\n"
            r"@@ -\d+,\d+ \+\d+,\d+ @@\n"
            r"(?P<body>.*?)(?:\ndiff --git |\Z)",
            patch_contents,
            re.DOTALL,
        )
        self.assertIsNotNone(fl_view_accessible_hunk)
        fl_view_accessible_hunk_body = fl_view_accessible_hunk.group("body")
        self.assertIn("+#if defined(__TERMUX__)", fl_view_accessible_hunk_body)
        self.assertIn("+#include <atk/atk.h>", fl_view_accessible_hunk_body)
        self.assertIn("+#else", fl_view_accessible_hunk_body)
        self.assertIn(' extern "C" {', fl_view_accessible_hunk_body)
        self.assertIn("+#endif", fl_view_accessible_hunk_body)

        # Matches the added-file hunk for termux BUILD.gn and captures:
        # 1) declared added-line count in "@@ -0,0 +1,N @@" and 2) hunk body.
        termux_build_gn_hunk_pattern = (
            r"\+\+\+ b/engine/src/build/config/termux/BUILD\.gn\n"
            r"@@ -0,0 \+1,(\d+) @@\n"
            r"(?P<body>.*?)(?:\ndiff --git |\Z)"
        )
        hunk = re.search(
            termux_build_gn_hunk_pattern,
            patch_contents,
            re.DOTALL,
        )
        self.assertIsNotNone(hunk)
        hunk_line_count = int(hunk.group(1))
        added_lines = sum(1 for line in hunk.group("body").splitlines() if line.startswith("+"))
        self.assertEqual(added_lines, hunk_line_count)


class PackageManifestTest(unittest.TestCase):
    def setUp(self):
        with open(Path(__file__).parent / "build.toml", "rb") as f:
            self.cfg = tomllib.load(f)

    def test_manifest_resource_generates_ship_manifest(self):
        flutter = self.cfg["flutter"]
        android = self.cfg["android"]
        ndk = self.cfg["ndk"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "flutter")
            release_out = root / "engine" / "src" / "out" / "linux_release_arm64"
            release_out.mkdir(parents=True)

            with open(Path(__file__).parent / "package.yaml", encoding="utf-8") as f:
                src = yaml.safe_load(f)

            with (
                patch("package.utils.flutter_tag", return_value=flutter["tag"]),
                patch("package.utils.engine_version", return_value="engine_revision_mock"),
            ):
                pkg = package.Package(
                    root=str(root),
                    arch="arm64",
                    dart_version=flutter["dart_version"],
                    framework_revision=flutter["framework_revision"],
                    framework_commit_date=flutter["framework_commit_date"],
                    devtools_version=flutter["devtools_version"],
                    ndk_version=ndk.get("version", ""),
                    compile_sdk=android["compile_sdk"],
                    target_sdk=android["target_sdk"],
                    **src,
                )

            items = list(pkg.gen_resource("manifest"))

            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(
                str(item["out"]), "data/data/com.termux/files/usr/share/flutter/manifest.json"
            )
            self.assertEqual(item["mod"], 0o644)
            manifest = json.loads(item["src"].decode("utf-8"))
            self.assertEqual(
                manifest,
                {
                    "flutter_version": flutter["tag"],
                    "framework_revision": flutter["framework_revision"],
                    "framework_commit_date": flutter["framework_commit_date"],
                    "engine_revision": "engine_revision_mock",
                    "dart_version": flutter["dart_version"],
                    "devtools_version": flutter["devtools_version"],
                    "ndk_version": ndk.get("version", ""),
                    "compile_sdk": android["compile_sdk"],
                    "target_sdk": android["target_sdk"],
                },
            )

    def test_control_version_uses_package_revision(self):
        flutter = self.cfg["flutter"]
        package_cfg = self.cfg.get("package", {})
        package_version = utils.deb_version(
            flutter["tag"],
            package_cfg.get("pkg_rel"),
            utils.snapshot_stamp(
                flutter.get("framework_commit_date", ""), flutter.get("framework_revision", "")
            ),
            flutter.get("framework_version", ""),
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "flutter")
            release_out = root / "engine" / "src" / "out" / "linux_release_arm64"
            release_out.mkdir(parents=True)

            with open(Path(__file__).parent / "package.yaml", encoding="utf-8") as f:
                src = yaml.safe_load(f)

            with (
                patch("package.utils.flutter_tag", return_value=flutter["tag"]),
                patch("package.utils.engine_version", return_value="engine_revision_mock"),
            ):
                pkg = package.Package(
                    root=str(root),
                    arch="arm64",
                    package_version=package_version,
                    **src,
                )

            control = pkg.gen_control()["src"].decode("utf-8")
            self.assertIn(f"Version: {package_version}", control)

    def _make_package(self, root: Path):
        """Build a Package against a minimal temp flutter root.

        ``flutter_tag``/``engine_version`` are stubbed so no real checkout is
        needed, and the release output dir is created for you."""
        flutter = self.cfg["flutter"]
        android = self.cfg["android"]
        ndk = self.cfg["ndk"]
        (root / "engine" / "src" / "out" / "linux_release_arm64").mkdir(
            parents=True, exist_ok=True
        )
        with open(Path(__file__).parent / "package.yaml", encoding="utf-8") as f:
            src = yaml.safe_load(f)
        with (
            patch("package.utils.flutter_tag", return_value=flutter["tag"]),
            patch("package.utils.engine_version", return_value="engine_revision_mock"),
        ):
            return package.Package(
                root=str(root),
                arch="arm64",
                dart_version=flutter["dart_version"],
                framework_revision=flutter["framework_revision"],
                framework_commit_date=flutter["framework_commit_date"],
                devtools_version=flutter["devtools_version"],
                ndk_version=ndk.get("version", ""),
                compile_sdk=android["compile_sdk"],
                target_sdk=android["target_sdk"],
                **src,
            )

    def test_post_install_resource_ships_config_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._make_package(Path(tmp, "flutter"))
            items = list(pkg.gen_resource("post_install"))

        # The deb must ship exactly the documented on-device script, executable.
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(
            str(item["out"]),
            "data/data/com.termux/files/usr/share/flutter/post_install.sh",
        )
        self.assertEqual(item["mod"], 0o755)
        expected = (
            Path(__file__).parent / "scripts" / "install" / "post_install.sh"
        ).read_bytes()
        self.assertEqual(Path(item["src"]).read_bytes(), expected)

    def test_tar_never_emits_hardlinks(self):
        """Duplicate multi-MiB payloads stay full copies.

        Termux installs onto ``/data``, which is f2fs on most devices, and the
        kernel rejects every ``link(2)`` there with EACCES. dpkg unpacking a
        ``LNKTYPE`` member then aborts the whole data tar, so the deb cannot be
        installed at all. xz preset 9 already dedupes the repeated bytes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            payload = b"x" * (1 << 21)
            first = Path(tmp, "first.bin")
            second = Path(tmp, "second.bin")
            first.write_bytes(payload)
            second.write_bytes(payload)
            data_tar = Path(tmp, "data.tar.xz")
            package.tar(
                data_tar,
                [
                    {"out": "a/first.bin", "src": first},
                    {"out": "a/second.bin", "src": second},
                ],
            )
            with tarfile.open(data_tar) as tar:
                members = {m.name: m for m in tar.getmembers()}
                extracted = tar.extractfile("a/second.bin").read()

        self.assertEqual([m.name for m in members.values() if m.islnk()], [])
        self.assertFalse(members["a/second.bin"].issym())
        self.assertEqual(members["a/second.bin"].size, len(payload))
        self.assertEqual(extracted, payload)

    def test_tar_compresses_with_xz_preset_9(self):
        """Preset 9 (64 MiB dictionary) dedupes duplicate engine snapshots that
        the tarfile default (6, 8 MiB) leaves uncompressed twice."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "a.bin")
            src.write_bytes(b"x")
            out = Path(tmp, "data.tar.xz")

            with patch("package.tarfile.open", wraps=tarfile.open) as opener:
                package.tar(out, [{"out": "a.bin", "src": src}])
            self.assertEqual(opener.call_args.kwargs["preset"], 9)

            with (
                patch.dict(os.environ, {"PACKAGE_XZ_PRESET": "3"}),
                patch("package.tarfile.open", wraps=tarfile.open) as opener,
            ):
                package.tar(out, [{"out": "a.bin", "src": src}])
            self.assertEqual(opener.call_args.kwargs["preset"], 3)

    def test_exclude_prunes_build_only_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "tree")
            for rel in (
                "bin/flutter",
                "engine/src/engine.cc",
                "dev/bots/x.dart",
                "docs/README.md",
                "packages/flutter/lib/a.dart",
                "packages/flutter/test/a_test.dart",
            ):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x", encoding="utf-8")

            patterns = ["engine/**", "dev/**", "docs/**", "packages/*/test/**"]
            got = {it.as_posix() for _, it in package.explore(root, False, patterns)}

        self.assertIn("bin/flutter", got)
        self.assertIn("packages/flutter/lib/a.dart", got)
        for pruned in ("engine", "dev", "docs", "packages/flutter/test"):
            self.assertFalse(
                [it for it in got if it == pruned or it.startswith(pruned + "/")],
                f"{pruned} was not pruned",
            )

    def test_git_resources_never_ship_dot_git(self):
        """A git-backed resource must carry the tree, never .git: 490 MiB of
        history, and post_install.sh synthesises a repo on device instead."""
        import git

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "repo")
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "flutter").write_text("x", encoding="utf-8")
            repo = git.Repo.init(root)
            repo.index.add(["bin/flutter"])
            actor = git.Actor("test", "test@example.com")
            repo.index.commit("init", author=actor, committer=actor)

            got = {it.as_posix() for _, it in package.explore(root, True)}

        self.assertIn("bin/flutter", got)
        self.assertFalse([it for it in got if it.startswith(".git")])

    def test_package_ships_maintainer_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = self._make_package(Path(tmp, "flutter"))
            scripts = {it["out"]: it for it in pkg.gen_scripts()}

        self.assertEqual(set(scripts), {"preinst", "postinst"})
        for name, item in scripts.items():
            self.assertEqual(item["mod"], 0o755, name)
            text = Path(item["src"]).read_text(encoding="utf-8")
            self.assertTrue(
                text.startswith("#!/data/data/com.termux/files/usr/bin/sh"), name
            )

    def test_postinst_runs_post_install_and_never_fails_the_install(self):
        script = (
            Path(__file__).parent / "scripts" / "install" / "deb_postinst.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("post_install.sh", script)
        self.assertIn("TERMUX_POSTINST=true", script)
        self.assertIn("configure) ;;", script)
        self.assertIn("timeout 1800", script)
        self.assertTrue(script.rstrip().endswith("exit 0"))

    def test_preinst_reclaims_legacy_payload_only(self):
        """Legacy .git (upstream clone) and build-only trees are removed on
        upgrade; a user-owned repo and a synthetic repo are preserved."""
        import git

        script = Path(__file__).parent / "scripts" / "install" / "deb_preinst.sh"

        def run(root: Path):
            env = {**os.environ, "FLUTTER_ROOT": str(root), "PREFIX": str(root.parent)}
            return subprocess.run(
                ["sh", str(script), "upgrade"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            legacy = tmp_path / "legacy"
            (legacy / "engine" / "src").mkdir(parents=True)
            (legacy / "dev").mkdir()
            (legacy / "packages" / "flutter" / "test").mkdir(parents=True)
            (legacy / "packages" / "flutter" / "lib").mkdir(parents=True)
            (legacy / "packages" / "flutter" / "lib" / "a.dart").write_text("a")
            repo = git.Repo.init(legacy)
            repo.create_remote("origin", "https://github.com/flutter/flutter")

            run(legacy)

            self.assertFalse((legacy / ".git").exists())
            self.assertFalse((legacy / "engine").exists())
            self.assertFalse((legacy / "dev").exists())
            self.assertFalse((legacy / "packages" / "flutter" / "test").exists())
            self.assertTrue((legacy / "packages" / "flutter" / "lib" / "a.dart").is_file())

            own = tmp_path / "own"
            own.mkdir()
            own_repo = git.Repo.init(own)
            own_repo.create_remote("origin", "https://example.com/mine.git")
            run(own)
            self.assertTrue((own / ".git").exists())

            synthetic = tmp_path / "synthetic"
            (synthetic / ".git").mkdir(parents=True)
            (synthetic / ".git" / "termux_synthetic").write_text("{}")
            run(synthetic)
            self.assertTrue((synthetic / ".git" / "termux_synthetic").is_file())

    def test_engine_patch_blocks_sdk_self_upgrade(self):
        """flutter_tools must reject upgrade/downgrade/channel-switch on Termux;
        the package manager owns SDK versions and the tree is cross-built."""
        patch = (
            Path(__file__).parent / "patches" / "engine.patch"
        ).read_text(encoding="utf-8")

        for command in ("upgrade", "downgrade", "channel"):
            self.assertIn(
                f"diff --git a/packages/flutter_tools/lib/src/commands/{command}.dart",
                patch,
            )
            self.assertIn(
                f"'flutter {command} is disabled on the Termux build",
                patch,
            )
        self.assertIn("pkg upgrade flutter", patch)


class FrameworkVersionTest(unittest.TestCase):
    def test_flutter_to_deb_upstream_translates_prerelease(self):
        self.assertEqual(utils.flutter_to_deb_upstream("3.47.6-0.0.pre-123"), "3.47.6~0.0.pre.123")
        self.assertEqual(utils.flutter_to_deb_upstream("3.47.6-0.0.pre"), "3.47.6~0.0.pre")
        self.assertEqual(utils.flutter_to_deb_upstream("3.47.5"), "3.47.5")
        self.assertEqual(utils.flutter_to_deb_upstream(""), "")
        self.assertEqual(utils.flutter_to_deb_upstream("0.0.0-unknown"), "")

    def test_deb_version_main_uses_framework_version(self):
        self.assertEqual(
            utils.deb_version("main", "1", "20260926.8db5526", "3.47.6-0.0.pre"),
            "3.47.6~0.0.pre+main.20260926.8db5526-1",
        )

    def test_deb_version_main_falls_back_without_framework_version(self):
        self.assertEqual(
            utils.deb_version("main", "1", "20260926.8db5526", ""),
            "0~main.20260926.8db5526-1",
        )

    def test_deb_version_stable_ignores_framework_version(self):
        self.assertEqual(
            utils.deb_version("3.47.5", "1", "20260926.8db5526", "3.47.6-0.0.pre"),
            "3.47.5-1",
        )

    def test_main_snapshot_sorts_above_last_stable(self):
        version = utils.deb_version("main", "1", "20260926.8db5526", "3.47.6-0.0.pre")
        self.assertTrue(version.startswith("3.47.6"))
        self.assertIn("~", version)
        self.assertIn("+main.", version)
        self.assertNotEqual(version, utils.deb_version("main", "1", "20260926.8db5526", ""))


class EngineVersionTest(unittest.TestCase):
    def test_reads_pinned_file_when_present(self):
        import utils

        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp, "bin", "internal")
            version_file.mkdir(parents=True)
            (version_file / "engine.version").write_text("abc123\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", tmp], check=True)
            self.assertEqual(utils.engine_version(tmp), "abc123\n")

    def test_falls_back_to_checkout_head_on_main(self):
        import utils

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", "-b", "main", tmp], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    tmp,
                    "-c",
                    "user.email=t@t",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "x",
                ],
                check=True,
            )
            head = subprocess.run(
                ["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(utils.engine_version(tmp), head)


class SafeEvalTest(unittest.TestCase):
    def test_legit_expressions_resolve(self):
        from package import safe_eval

        class Out:
            any = "/tmp/any"
            debug = "/tmp/debug"

        g = {"version": "abc123", "output": Out(), "distro": "/opt/flutter"}
        self.assertEqual(safe_eval('"plain"', g), "plain")
        self.assertEqual(safe_eval("f'https://x/{version}'", g), "https://x/abc123")
        self.assertEqual(safe_eval("output.any", g), "/tmp/any")
        self.assertEqual(safe_eval("f'{distro}/bin/cache'", g), "/opt/flutter/bin/cache")

    def test_malicious_expressions_rejected(self):
        from package import safe_eval

        g = {"version": "abc123"}
        for expr in (
            "__import__('os').system('id')",
            "open('/etc/passwd').read()",
            "(lambda: 1)()",
            "[x for x in range(3)]",
            "version.__class__",
        ):
            with self.assertRaises(ValueError, msg=expr):
                safe_eval(expr, g)


class RecordDecoratorTest(unittest.TestCase):
    def test_record_reraises_instead_of_exit(self):
        import utils

        @utils.recordm
        def boom():
            raise RuntimeError("kaput")

        with self.assertRaises(RuntimeError):
            boom()


class SysrootLockTest(unittest.TestCase):
    def test_write_lock_schema(self):
        import sysroot

        metas = [
            {
                "name": "glib",
                "version": "2.0",
                "url": "https://example.com/glib.deb",
                "sha256": "0" * 64,
                "size": 123,
                "archive_path": "pool/glib.deb",
                "repo": "https://example.com/",
                "dist": "stable",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp, "sysroot.lock.json")
            sysroot._write_lock(lock, "aarch64", metas, "deadbeef")
            data = json.loads(lock.read_text(encoding="utf-8"))
            for key in ("aarch64", "arm64"):
                self.assertIn(key, data)
                entry = data[key]
                for req in ("arch", "created_at", "tree_hash", "packages"):
                    self.assertIn(req, entry)
                pkg = entry["packages"]["glib"]
                for field in (
                    "name",
                    "version",
                    "url",
                    "sha256",
                    "size",
                    "archive_path",
                    "repo",
                    "dist",
                ):
                    self.assertIn(field, pkg)


def test_version_single_source_matches_build():
    import os
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path("scripts/ci").resolve()))
    import version_lib

    import build

    os.environ.setdefault("ANDROID_NDK", "/tmp/android-ndk")
    b = build.Build()
    assert b.release_tag == version_lib.release_tag(
        b.tag, b.pkg_rel, b.snapshot, b.framework_version
    )
    assert b.package_version == version_lib.deb_version(
        b.tag, b.pkg_rel, b.snapshot, b.framework_version
    )


def test_drift_config_matches_build_output():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path("scripts/ci").resolve()))
    from check_version_drift import load_build_config

    import build

    os.environ.setdefault("ANDROID_NDK", "/tmp/android-ndk")
    b = build.Build()
    cfg = load_build_config()
    assert cfg["release_tag"] == b.release_tag
    assert cfg["asset_name"] == b.output("arm64").name


def test_main_release_tag_never_collides_with_stable():
    import re

    import build

    os.environ.setdefault("ANDROID_NDK", "/tmp/android-ndk")
    b = build.Build()
    assert not re.fullmatch(r"v?\d+\.\d+\.\d+", b.release_tag)
    assert re.fullmatch(r"v\d+\.\d+\.\d+-.+-\d+", b.release_tag)


def test_dart_canonical_vs_prose_forms():
    import re
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path("scripts/ci").resolve()))
    from check_version_drift import load_build_config

    cfg = load_build_config()
    assert re.fullmatch(r"\d+\.\d+\.\d+ \(build .+\)", cfg["dart_version"])
    semver = cfg["dart_version"].split(" ")[0]
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", semver)


def test_verify_patches_helper_exists_and_parses():
    import subprocess

    r = subprocess.run(
        ["python3", "scripts/ci/verify_patches.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "--rev" in r.stdout


def test_patch_rebase_evidence_matches_pin():
    import tomllib

    evidence = Path("patches/last-rebase.txt")
    assert evidence.is_file(), (
        "missing patches/last-rebase.txt; run scripts/ci/verify_patches.py --rev <framework_revision>"
    )
    text = evidence.read_text(encoding="utf-8")
    with open("build.toml", "rb") as f:
        rev = tomllib.load(f)["flutter"]["framework_revision"]
    assert f"rev={rev}" in text
    assert "engine=OK" in text
    assert "dart=parse-OK" in text
    assert "skia=parse-OK" in text


def test_expected_sha256_override_precedence():
    """FLUTTER_DEB_SHA256 beats EXPECTED_SHA256, and the pin stays fail-closed."""
    script = 'source scripts/install/versions_common.sh; printf "%s" "$EXPECTED_SHA256"'

    def resolved(**overrides):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("FLUTTER_DEB_SHA256", "EXPECTED_SHA256")
        }
        env.update(overrides)
        try:
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, env=env
            )
        except FileNotFoundError:
            raise unittest.SkipTest("bash not available")
        assert r.returncode == 0, r.stderr
        return r.stdout

    default = resolved()
    assert default, "EXPECTED_SHA256 must never resolve empty (fail closed)"
    with open("build.toml", "rb") as f:
        pinned = tomllib.load(f)["flutter"].get("sha256")
    if pinned:
        assert re.fullmatch(r"[0-9a-f]{64}", default), (
            "pinned default must be a published sha256; refresh it after a release"
        )
        assert default == pinned, (
            "versions_common.sh default drifted from build.toml [flutter] sha256"
        )
    else:
        assert not re.fullmatch(r"[0-9a-f]{64}", default), (
            "no published sha256 yet: keep the fail-closed placeholder until the "
            "first build of this package revision publishes"
        )
    assert resolved(EXPECTED_SHA256="a" * 64) == "a" * 64
    assert resolved(FLUTTER_DEB_SHA256="b" * 64) == "b" * 64
    assert resolved(EXPECTED_SHA256="a" * 64, FLUTTER_DEB_SHA256="b" * 64) == "b" * 64


if __name__ == "__main__":
    unittest.main()
