import os
import sys
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kiosk_center"))

from center.options import options  # noqa: E402
from center.server import Center, STATUS_SECONDS, VERSION  # noqa: E402


class FakeHA:
    def listen(self, _callback):
        pass


class NtpServerConfigTests(unittest.TestCase):
    def test_server_option_defaults_and_reads_environment_override(self):
        with patch("builtins.open", side_effect=FileNotFoundError), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(options()["ntp_server"], "pool.ntp.org")
        with patch("builtins.open", mock_open(read_data='{"ntp_server":"time.home.arpa"}')), \
                patch.dict(os.environ, {}, clear=True):
            self.assertEqual(options()["ntp_server"], "time.home.arpa")
        with patch("builtins.open", side_effect=FileNotFoundError), patch.dict(
                os.environ, {"NTP_SERVER": "time.example.net"}, clear=True):
            self.assertEqual(options()["ntp_server"], "time.example.net")
        with patch("builtins.open", side_effect=FileNotFoundError), patch.dict(
                os.environ, {"NTP_SERVER": "https://time.example.net"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Invalid NTP server"):
                options()

    def test_center_includes_ntp_server_in_public_configuration(self):
        center = Center(None, FakeHA(), None, ntp_server="time.example.net")
        self.assertEqual(center._center_public(), {
            "version": VERSION,
            "status_seconds": STATUS_SECONDS,
            "ntp_server": "time.example.net",
        })


if __name__ == "__main__":
    unittest.main()
