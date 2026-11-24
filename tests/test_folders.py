"""Folders the user may move or rename: markers, search, and never recreating an old path."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf2md_local as pdf2md  # noqa: E402


class FolderTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.inbox = self.root / "pdf2md" / "input_pdf"
        self.output = self.root / "pdf2md" / "output_md"
        self.inbox.mkdir(parents=True)
        self.output.mkdir(parents=True)
        self.config_file = self.root / "config.json"
        patches = [
            mock.patch.dict("os.environ", {"PDF2MD_LOCAL_CONFIG": str(self.config_file)}),
            mock.patch.object(pdf2md, "search_roots", return_value=[self.root]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self._temp.cleanup()

    def marked_config(self):
        config = pdf2md.default_config()
        config.update({"inbox": str(self.inbox), "output": str(self.output)})
        pdf2md.save_config(config)
        config = pdf2md.load_config()
        pdf2md.mark_folders(config)
        return pdf2md.load_config()

    def test_marker_is_written_once(self):
        first = pdf2md.ensure_marker(self.inbox, "input")
        self.assertEqual(pdf2md.ensure_marker(self.inbox, "input"), first)
        self.assertEqual(pdf2md.read_marker(self.inbox)["kind"], "input")

    def test_moved_folders_are_followed_and_remembered(self):
        config = self.marked_config()
        moved_inbox = self.root / "Desktop" / "my papers in"
        moved_output = self.root / "Documents" / "deep" / "md here"
        moved_inbox.parent.mkdir()
        moved_output.parent.mkdir(parents=True)
        shutil.move(str(self.inbox), str(moved_inbox))
        shutil.move(str(self.output), str(moved_output))
        inbox, output = pdf2md.resolve_folders(SimpleNamespace(), config)
        self.assertEqual((inbox, output), (moved_inbox, moved_output))
        saved = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["inbox"], str(moved_inbox))
        self.assertFalse(self.inbox.exists(), "the old path must not be recreated")

    def test_missing_folder_that_cannot_be_found_is_not_recreated(self):
        config = self.marked_config()
        shutil.rmtree(self.inbox)
        inbox, _ = pdf2md.resolve_folders(SimpleNamespace(), config)
        self.assertEqual(inbox, self.inbox)
        self.assertFalse(self.inbox.exists())

    def test_a_different_folder_at_the_old_path_is_not_taken(self):
        config = self.marked_config()
        real = self.root / "elsewhere"
        shutil.move(str(self.inbox), str(real))
        self.inbox.mkdir()   # e.g. recreated empty by some other program
        inbox, _ = pdf2md.resolve_folders(SimpleNamespace(), config)
        self.assertEqual(inbox, real)

    def test_existing_marker_is_adopted_and_lookups_never_write_one(self):
        self.marked_config()
        folder_id = pdf2md.read_marker(self.inbox)["id"]
        fresh = pdf2md.default_config()
        fresh.update({"inbox": str(self.inbox), "output": str(self.output)})
        pdf2md.save_config(fresh)
        fresh = pdf2md.load_config()
        pdf2md.resolve_folders(SimpleNamespace(), fresh)
        self.assertEqual(fresh["inbox_id"], folder_id)
        unmarked = self.root / "plain"
        unmarked.mkdir()
        fresh["inbox"] = str(unmarked)
        fresh.pop("inbox_id")
        pdf2md.resolve_folders(SimpleNamespace(), fresh)
        self.assertIsNone(pdf2md.read_marker(unmarked), "looking a folder up must not write a marker")

    def test_open_prints_both_folders(self):
        config = self.marked_config()
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            pdf2md.cmd_open(SimpleNamespace(inbox=None, output=None, print_only=True), config)
        self.assertIn(str(self.inbox), out.getvalue())
        self.assertIn(str(self.output), out.getvalue())

    def test_flags_and_env_folders_are_used_as_given(self):
        config = self.marked_config()
        shutil.move(str(self.inbox), str(self.root / "moved"))
        args = SimpleNamespace(inbox=str(self.inbox), output=str(self.output))
        self.assertEqual(pdf2md.resolve_folders(args, config)[0], self.inbox)
        with mock.patch.dict("os.environ", {"PDF2MD_LOCAL_INPUT": str(self.inbox)}):
            env_config = pdf2md.load_config()
            self.assertEqual(pdf2md.resolve_folders(SimpleNamespace(), env_config)[0], self.inbox)

    def test_search_skips_hidden_and_too_deep_folders(self):
        folder_id = pdf2md.ensure_marker(self.inbox, "input")
        hidden = self.root / ".hidden" / "x"
        hidden.mkdir(parents=True)
        shutil.move(str(self.inbox), str(hidden / "in"))
        self.assertIsNone(pdf2md.find_marked_folder(folder_id, roots=[self.root]))

    def test_batch_never_recreates_a_missing_output(self):
        shutil.rmtree(self.output)
        opts = SimpleNamespace(inbox=self.inbox, output=self.output, tool="mineru", allow_cpu=False,
                               unattended=True, mode="watch")
        with mock.patch.object(pdf2md, "tool_version", return_value="x"):
            with self.assertRaises(pdf2md.UserError):
                pdf2md.run_batch({}, opts, [], move_source=True)
        self.assertFalse(self.output.exists())

    def test_watcher_follows_a_moved_inbox(self):
        config = self.marked_config()
        moved = self.root / "renamed inbox"
        seen = []

        def fake_watch(cfg, opts):
            seen.append(opts.inbox)
            if len(seen) == 1:
                shutil.move(str(self.inbox), str(moved))   # the user moves it while it is watched

        with mock.patch.object(pdf2md, "hold_watch_lock", return_value=object()), \
                mock.patch.object(pdf2md, "watch", side_effect=fake_watch):
            pdf2md.cmd_watch(SimpleNamespace(inbox=None, output=None, tool=None, allow_cpu=False), config,
                             sleep=lambda seconds: None, max_rounds=2)
        self.assertEqual(seen, [self.inbox, moved])
        self.assertFalse(self.inbox.exists())


class SetupTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.home = Path(self._temp.name)
        good = {"ok": True, "best": None, "gpus": [], "ram_gb": 16, "disk_free_gb": 100, "problems": [], "notes": []}
        patches = [
            mock.patch.dict("os.environ", {"PDF2MD_LOCAL_CONFIG": str(self.home / "config.json")}),
            mock.patch.object(pdf2md, "HOME", self.home),
            mock.patch.object(pdf2md, "hardware_report", return_value=good),
            mock.patch.object(pdf2md, "detect_tool", return_value={"cmd": "mineru-kit", "python": "python"}),
            mock.patch.object(pdf2md, "tool_device", return_value=("gpu", "Test GPU")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._temp.cleanup)

    def setup_args(self, **kwargs):
        return SimpleNamespace(yes=True, accept_gpu=False, inbox=kwargs.get("inbox"), output=kwargs.get("output"))

    def test_setup_creates_the_two_default_folders_with_markers(self):
        pdf2md.cmd_setup(self.setup_args(), pdf2md.load_config())
        base = self.home / "pdf2md-local"
        config = pdf2md.load_config()
        self.assertEqual(config["inbox"], str(base / "input_pdf"))
        self.assertEqual(config["output"], str(base / "output_md"))
        self.assertEqual(pdf2md.read_marker(base / "input_pdf")["id"], config["inbox_id"])
        self.assertEqual(pdf2md.read_marker(base / "output_md")["id"], config["output_id"])

    def test_setup_refuses_below_the_minimum_and_writes_nothing(self):
        bad = {"ok": False, "best": None, "gpus": [], "ram_gb": 8, "disk_free_gb": 100,
               "problems": ["No supported GPU"], "notes": []}
        with mock.patch.object(pdf2md, "hardware_report", return_value=bad):
            with self.assertRaises(pdf2md.UserError):
                pdf2md.cmd_setup(self.setup_args(), pdf2md.load_config())
        self.assertFalse((self.home / "pdf2md-local").exists())
        self.assertFalse((self.home / "config.json").exists())

    def test_setup_refuses_when_mineru_sees_only_the_cpu(self):
        with mock.patch.object(pdf2md, "tool_device", return_value=("cpu", "llama.cpp sees no usable GPU")):
            with self.assertRaises(pdf2md.UserError):
                pdf2md.cmd_setup(self.setup_args(), pdf2md.load_config())
        self.assertFalse((self.home / "config.json").exists())

    def test_setup_refuses_nested_folders(self):
        args = self.setup_args(inbox=str(self.home / "x"), output=str(self.home / "x" / "md"))
        with self.assertRaises(pdf2md.UserError):
            pdf2md.cmd_setup(args, pdf2md.load_config())


if __name__ == "__main__":
    unittest.main()
