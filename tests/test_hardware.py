"""Converter hardware requirements: a GPU is required, CPU-only machines are refused."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf2md_local as pdf2md  # noqa: E402


def verdict(name, vram=None):
    return pdf2md.classify_gpu(name, vram)[:2]


class ClassifyTest(unittest.TestCase):
    def test_discrete(self):
        self.assertEqual(verdict("NVIDIA GeForce RTX 3060", 12), ("discrete", "ok"))
        self.assertEqual(verdict("Advanced Micro Devices, Inc. [AMD/ATI] Navi 23 [Radeon RX 6600]"), ("discrete", "ok"))
        self.assertEqual(verdict("Intel(R) Arc(TM) A770 Graphics"), ("discrete", "ok"))
        self.assertEqual(verdict("NVIDIA GeForce MX450", 2), ("discrete", "no"))

    def test_integrated(self):
        self.assertEqual(verdict("Intel(R) Iris(R) Xe Graphics"), ("integrated", "ok"))
        self.assertEqual(verdict("AMD Radeon 780M"), ("integrated", "ok"))
        # Measured: UHD 770 converts correctly but at about 142 s per page, below the Iris Xe minimum
        self.assertEqual(verdict("Intel(R) UHD Graphics 770"), ("integrated", "no"))
        self.assertEqual(verdict("Intel Corporation Alder Lake-S GT1 [UHD Graphics 730]"), ("integrated", "no"))

    def test_too_weak_or_software(self):
        for name in ("Intel(R) HD Graphics 630", "Intel(R) UHD Graphics 620", "llvmpipe (LLVM 15.0.7, 256 bits)",
                     "Microsoft Basic Display Adapter", ""):
            with self.subTest(name=name):
                self.assertEqual(verdict(name)[1], "no")

    def test_unrecognised_needs_accept_gpu(self):
        self.assertEqual(verdict("AMD Radeon(TM) Graphics"), ("integrated", "unknown"))

    def test_apple(self):
        self.assertEqual(pdf2md.classify_gpu("Apple GPU", machine="apple")[:2], ("apple", "ok"))


class ReportTest(unittest.TestCase):
    def report(self, gpus, ram=32, disk=100, **kwargs):
        return pdf2md.hardware_report(gpus=gpus, ram_gb=ram, disk_free_gb=disk, **kwargs)

    def test_cpu_only_is_refused(self):
        report = self.report([("Microsoft Basic Display Adapter", None)])
        self.assertFalse(report["ok"])
        self.assertIn("CPU-only", report["problems"][0])
        self.assertFalse(self.report([]).get("ok"))

    def test_cpu_only_is_refused_even_with_accept_gpu(self):
        self.assertFalse(self.report([("llvmpipe (LLVM 15)", None)], accept_gpu=True)["ok"])

    def test_best_gpu_wins(self):
        report = self.report([("Intel(R) UHD Graphics 770", None), ("NVIDIA GeForce RTX 3060", 12.0)], ram=16)
        self.assertTrue(report["ok"])
        self.assertEqual(report["best"]["name"], "NVIDIA GeForce RTX 3060")

    def test_uhd_7xx_alone_is_refused_even_with_accept_gpu(self):
        gpus = [("Intel Corporation Alder Lake-S GT1 [UHD Graphics 730]", None)]
        self.assertFalse(self.report(gpus, ram=16)["ok"])
        self.assertFalse(self.report(gpus, ram=16, accept_gpu=True)["ok"])

    def test_accepted_unknown_gpu_passes_with_a_note(self):
        report = self.report([("AMD Radeon(TM) Graphics", None)], ram=16, accept_gpu=True)
        self.assertTrue(report["ok"])
        self.assertTrue(report["notes"])

    def test_igpu_needs_16_gb(self):
        report = self.report([("Intel(R) Iris(R) Xe Graphics", None)], ram=8)
        self.assertFalse(report["ok"])
        self.assertIn("16 GB", report["problems"][0])

    def test_unknown_gpu_only_with_accept_gpu(self):
        gpus = [("AMD Radeon(TM) Graphics", None)]
        self.assertFalse(self.report(gpus)["ok"])
        self.assertTrue(self.report(gpus, accept_gpu=True)["ok"])

    def test_disk_and_macos(self):
        self.assertFalse(self.report([("NVIDIA GeForce RTX 3060", 12.0)], disk=5)["ok"])
        # After MinerU is installed (installer step 4) only its models and the md files still need room
        self.assertTrue(self.report([("NVIDIA GeForce RTX 3060", 12.0)], disk=13, mineru_installed=True)["ok"])
        self.assertFalse(self.report([("NVIDIA GeForce RTX 3060", 12.0)], disk=13, mineru_installed=False)["ok"])
        self.assertFalse(self.report([("NVIDIA GeForce RTX 3060", 12.0)], disk=3, mineru_installed=True)["ok"])
        apple = self.report([("Apple GPU", None)], ram=16, machine="apple", macos_version=(13, 6))
        self.assertFalse(apple["ok"])
        self.assertTrue(self.report([("Apple GPU", None)], ram=16, machine="apple", macos_version=(14, 2))["ok"])
        self.assertFalse(self.report([], machine="intel-mac")["ok"])


class ToolDeviceTest(unittest.TestCase):
    def test_reads_llama_cpp_devices(self):
        listing = ("Available devices:\n  Vulkan0: Intel(R) UHD Graphics 770 (7989 MiB, 7333 MiB free)\n"
                   "  Vulkan1: NVIDIA GeForce RTX 3060 (12113 MiB, 11345 MiB free)\n")
        with mock.patch.object(pdf2md, "tool_paths", return_value=(Path("x"), Path("py"))), \
                mock.patch.object(pdf2md, "run_python", return_value=listing):
            kind, description = pdf2md.tool_device({}, "mineru")
        self.assertEqual(kind, "gpu")
        self.assertEqual(description, "NVIDIA GeForce RTX 3060")   # the UHD 770 is below the minimum

    def test_only_a_uhd_igpu_means_no_usable_gpu(self):
        listing = "Available devices:\n  Vulkan0: Intel(R) UHD Graphics 730 (7989 MiB, 7333 MiB free)\n"
        with mock.patch.object(pdf2md, "tool_paths", return_value=(Path("x"), Path("py"))), \
                mock.patch.object(pdf2md, "run_python", return_value=listing):
            self.assertEqual(pdf2md.tool_device({}, "mineru")[0], "cpu")

    def test_no_devices_means_cpu_and_batches_refuse(self):
        with mock.patch.object(pdf2md, "tool_paths", return_value=(Path("x"), Path("py"))), \
                mock.patch.object(pdf2md, "run_python", return_value="Available devices:\n"):
            self.assertEqual(pdf2md.tool_device({}, "mineru")[0], "cpu")
            with self.assertRaises(pdf2md.UserError):
                pdf2md.require_gpu({}, "mineru", allow_cpu=False)


if __name__ == "__main__":
    unittest.main()
