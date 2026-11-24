"""Locks, progress parsing, config resolution and the safety rules of a batch."""

import datetime
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf2md_local as pdf2md  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.inbox = self.root / "inbox"
        self.output = self.root / "output"
        self.inbox.mkdir()
        self.output.mkdir()
        self.log_file = self.inbox / pdf2md.LOG_NAME

    def tearDown(self):
        self._temp.cleanup()


class LockTest(TempDirTest):
    def test_acquire_then_busy_then_release(self):
        self.assertIsNone(pdf2md.acquire_lock(self.inbox, "manual", self.log_file))
        holder = pdf2md.acquire_lock(self.inbox, "watch", self.log_file)
        self.assertEqual(holder["pid"], os.getpid())
        pdf2md.release_lock(self.inbox)
        self.assertFalse(pdf2md.lock_path_for(self.inbox).exists())

    def test_stale_lock_is_cleared_and_logged(self):
        lock = pdf2md.lock_path_for(self.inbox)
        lock.write_text("999999\nwatch\n2020-01-01 00:00:00\n", encoding="utf-8")
        self.assertIsNone(pdf2md.acquire_lock(self.inbox, "manual", self.log_file))
        self.assertIn("Removed stale lock: PID 999999", self.log_file.read_text(encoding="utf-8"))
        pdf2md.release_lock(self.inbox)

    def test_reused_pid_counts_as_stale(self):
        # Our own PID, but a lock dated long before this process started
        lock = pdf2md.lock_path_for(self.inbox)
        lock.write_text(f"{os.getpid()}\nwatch\n2000-01-01 00:00:00\n", encoding="utf-8")
        info = pdf2md.read_lock(lock)
        try:
            import psutil  # noqa: F401
        except ImportError:
            self.skipTest("create-time check needs psutil")
        self.assertFalse(pdf2md.lock_owner_alive(info))

    def test_release_never_deletes_someone_elses_lock(self):
        lock = pdf2md.lock_path_for(self.inbox)
        lock.write_text("4\nwatch\n2026-01-01 00:00:00\n", encoding="utf-8")
        pdf2md.release_lock(self.inbox)
        self.assertTrue(lock.exists())

    def test_pid_alive_without_psutil(self):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            self.assertTrue(pdf2md.pid_alive(os.getpid()))
            self.assertFalse(pdf2md.pid_alive(999999))


class ProgressTest(unittest.TestCase):
    def replay(self):
        raw = (FIXTURES / "mineru_stderr_sample.txt").read_text(encoding="utf-8", errors="replace")
        progress = pdf2md.ProgressLine()
        labels = []
        for piece in re.split(r"[\r\n]", raw):
            if piece.strip():
                progress.feed(piece)
                if not labels or labels[-1] != progress.stage_label:
                    labels.append(progress.stage_label)
        return progress, labels

    def test_stages_in_order(self):
        progress, labels = self.replay()
        self.assertEqual(labels, ["Starting", "Loading model", "Layout", "Content", "Inline formulas",
                                  "Text detection", "Text recognition"])
        self.assertEqual(progress.window, (1, 1, 1, 34, 34))

    def test_text_line(self):
        progress, _ = self.replay()
        pdf2md.PROGRESS_STATE["prefix"] = "[1/2]"
        progress.last_update = 10 ** 10  # "just updated", so the bar is shown
        text = progress.text()
        pdf2md.PROGRESS_STATE["prefix"] = ""
        self.assertIn("[1/2] | 34 pages | Text recognition", text)
        self.assertIn("8/8", text)

    def test_display_width(self):
        self.assertEqual(pdf2md.display_width("ab中文"), 6)


class ConfigTest(TempDirTest):
    def test_flags_beat_env_beat_file(self):
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps({"inbox": "/file/in", "output": "/file/out"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"PDF2MD_LOCAL_CONFIG": str(config_file), "PDF2MD_LOCAL_INPUT": "/env/in"}):
            config = pdf2md.load_config()
            self.assertEqual(config["inbox"], "/env/in")
            self.assertEqual(config["output"], "/file/out")
            args = SimpleNamespace(inbox="/flag/in", output=None)
            inbox, output = pdf2md.resolve_folders(args, config)
            self.assertEqual((inbox, output), (Path("/flag/in"), Path("/file/out")))

    def test_missing_folders_is_a_user_error(self):
        with self.assertRaises(pdf2md.UserError):
            pdf2md.resolve_folders(SimpleNamespace(inbox=None, output=None), pdf2md.default_config())

    def test_save_skips_private_keys(self):
        config_file = self.root / "sub" / "config.json"
        with mock.patch.dict(os.environ, {"PDF2MD_LOCAL_CONFIG": str(config_file)}):
            config = pdf2md.default_config()
            config["_source"] = "x"
            pdf2md.save_config(config)
            self.assertNotIn("_source", json.loads(config_file.read_text(encoding="utf-8")))


class SafetyTest(TempDirTest):
    def test_move_never_overwrites(self):
        done = self.inbox / "done"
        done.mkdir()
        (done / "a.pdf").write_text("old", encoding="utf-8")
        pdf = self.inbox / "a.pdf"
        pdf.write_text("new", encoding="utf-8")
        target = pdf2md.move_without_overwrite(pdf, done)
        self.assertEqual(target.name, "a_2.pdf")
        self.assertEqual((done / "a.pdf").read_text(encoding="utf-8"), "old")

    def test_existing_md_is_skipped_not_overwritten(self):
        pdf = self.inbox / "paper.pdf"
        pdf.write_text("pdf", encoding="utf-8")
        target = self.output / "paper" / "paper.md"
        target.parent.mkdir()
        target.write_text("original", encoding="utf-8")
        status = pdf2md.process_one({}, pdf, "mineru", "0", self.output, self.inbox / "done", self.log_file)
        self.assertEqual(status, "skipped")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertTrue(pdf.exists())

    def test_save_output_flattens_referenced_images_only(self):
        work = self.root / "work"
        (work / "images").mkdir(parents=True)
        (work / "images" / "used.jpg").write_bytes(b"x")
        (work / "images" / "unused.jpg").write_bytes(b"y")
        md = work / "markdown.md"
        md.write_text("# T\n\n![](images/used.jpg)\n", encoding="utf-8")
        final = pdf2md.save_output(md, self.output / "doc", "doc", pdf2md.front_matter("doc.pdf", "mineru", "4.2.3"))
        text = final.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('---\nsource_pdf: "doc.pdf"\ntool: mineru\ntool_version: 4.2.3\n'))
        self.assertIn("![](used.jpg)", text)
        self.assertEqual(sorted(p.name for p in final.parent.iterdir()), ["doc.md", "used.jpg"])

    def test_inbox_is_never_created(self):
        missing = self.root / "renamed_inbox"
        opts = SimpleNamespace(inbox=missing, output=self.output, mode="watch", unattended=True,
                               tool="mineru", allow_cpu=False)
        self.assertFalse(pdf2md.run_inbox({}, opts))
        self.assertFalse(missing.exists())

    def test_stable_files_only(self):
        fresh = self.inbox / "fresh.pdf"
        old = self.inbox / "old.pdf"
        fresh.write_text("x", encoding="utf-8")
        old.write_text("x", encoding="utf-8")
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).timestamp()
        os.utime(old, (past, past))
        self.assertEqual(pdf2md.stable_pdfs([fresh, old], 60), [old])

    def test_input_and_output_must_be_separate(self):
        self.assertTrue(pdf2md.nested(self.root / "a", self.root / "a"))
        self.assertTrue(pdf2md.nested(self.root / "a", self.root / "a" / "b"))
        self.assertTrue(pdf2md.nested(self.root / "a" / "b", self.root / "a"))
        self.assertFalse(pdf2md.nested(self.root / "a" / "input_pdf", self.root / "a" / "output_md"))


class ServiceTextTest(unittest.TestCase):
    def test_service_files_mention_watch(self):
        command = ["/usr/bin/python3", "/opt/pdf2md-local/pdf2md_local.py", "watch"]
        self.assertIn("<string>watch</string>", pdf2md.mac_plist(command))
        self.assertIn("ExecStart=/usr/bin/python3 /opt/pdf2md-local/pdf2md_local.py watch", pdf2md.linux_unit(command))
        xml = pdf2md.windows_task_xml(["C:\\py\\pythonw.exe", "-X", "utf8", "C:\\a b\\pdf2md.py", "watch"])
        self.assertIn("<Arguments>-X utf8 &quot;C:\\a b\\pdf2md.py&quot; watch</Arguments>", xml)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", xml)



class ToolEnvTest(unittest.TestCase):
    def test_mineru_runs_with_telemetry_off(self):
        env = pdf2md.tool_env_vars()
        self.assertEqual(env["MINERU_TELEMETRY_DISABLED"], "1")
        self.assertEqual(env["HF_HUB_DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["PYTHONUTF8"], "1")


if __name__ == "__main__":
    unittest.main()
