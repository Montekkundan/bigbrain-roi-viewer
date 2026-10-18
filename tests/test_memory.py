"""Validate platform units, current/peak separation, and unavailable readings."""

import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openmind import memory


class CurrentRssTests(unittest.TestCase):
    def test_darwin_reads_resident_bytes_from_a_complete_libproc_response(self):
        def read_info(pid, flavor, arg, buffer, size):
            self.assertEqual(pid, memory.os.getpid())
            self.assertEqual((flavor, arg, size), (4, 0, 96))
            info = ctypes.cast(buffer, ctypes.POINTER(memory._ProcTaskInfo)).contents
            info.pti_virtual_size = 9_000_000_000
            info.pti_resident_size = 123_456_789
            return size

        with patch.object(memory.sys, "platform", "darwin"):
            with patch.object(memory, "_proc_pidinfo", return_value=read_info):
                self.assertEqual(memory.current_rss_bytes(), 123_456_789)

    def test_darwin_incomplete_or_failed_response_is_unavailable(self):
        with patch.object(memory.sys, "platform", "darwin"):
            for returned in (-1, 0, 8, 95):
                with self.subTest(returned=returned):
                    with patch.object(memory, "_proc_pidinfo", return_value=Mock(return_value=returned)):
                        self.assertIsNone(memory.current_rss_bytes())

    def test_darwin_unavailable_library_is_cached_without_subprocesses(self):
        memory._proc_pidinfo.cache_clear()
        self.addCleanup(memory._proc_pidinfo.cache_clear)
        with patch.object(memory.sys, "platform", "darwin"):
            with patch.object(memory.ctypes, "CDLL", side_effect=OSError("Unavailable")) as load:
                self.assertIsNone(memory.current_rss_bytes())
                self.assertIsNone(memory.current_rss_bytes())
        load.assert_called_once()

    def test_linux_converts_resident_pages_using_actual_page_size(self):
        with patch.object(memory.sys, "platform", "linux"):
            with patch.object(memory.Path, "read_text", return_value="1000 7 5 0 0 0 0\n"):
                with patch.object(memory.os, "sysconf", return_value=16384):
                    self.assertEqual(memory.current_rss_bytes(), 7 * 16384)

    def test_linux_unreadable_or_invalid_statm_is_unavailable(self):
        with patch.object(memory.sys, "platform", "linux"):
            with patch.object(memory.Path, "read_text", side_effect=PermissionError("Denied")):
                self.assertIsNone(memory.current_rss_bytes())
            for text in ("", "1000", "1000 invalid", "1000 -1"):
                with self.subTest(text=text):
                    with patch.object(memory.Path, "read_text", return_value=text):
                        self.assertIsNone(memory.current_rss_bytes())

    def test_unsupported_current_rss_does_not_fall_back_to_peak(self):
        with patch.object(memory.sys, "platform", "unsupported"):
            with patch.object(memory, "peak_rss_bytes", return_value=999_999) as peak:
                self.assertIsNone(memory.current_rss_bytes())
        peak.assert_not_called()


class PeakRssTests(unittest.TestCase):
    def test_peak_units_are_platform_specific(self):
        usage = SimpleNamespace(ru_maxrss=12345)
        fake_resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(return_value=usage))
        with patch.object(memory, "resource", fake_resource):
            for platform, expected in (("darwin", 12345), ("linux", 12345 * 1024)):
                with self.subTest(platform=platform):
                    with patch.object(memory.sys, "platform", platform):
                        self.assertEqual(memory.peak_rss_bytes(), expected)

    def test_peak_failure_and_unsupported_platform_remain_unavailable(self):
        fake_resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(side_effect=OSError("Denied")))
        with patch.object(memory, "resource", fake_resource):
            with patch.object(memory.sys, "platform", "darwin"):
                self.assertIsNone(memory.peak_rss_bytes())
            with patch.object(memory.sys, "platform", "unsupported"):
                self.assertIsNone(memory.peak_rss_bytes())
        with patch.object(memory, "resource", None):
            self.assertIsNone(memory.peak_rss_bytes())


if __name__ == "__main__":
    unittest.main()
