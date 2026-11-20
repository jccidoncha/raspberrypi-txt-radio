import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_autorun as ua

LSBLK = {"blockdevices": [
    {"path": "/dev/mmcblk0", "type": "disk", "tran": None, "fstype": None, "children": [
        {"path": "/dev/mmcblk0p2", "type": "part", "fstype": "ext4", "mountpoint": "/"}]},
    {"path": "/dev/sda", "type": "disk", "tran": "usb", "fstype": None, "children": [
        {"path": "/dev/sda1", "type": "part", "fstype": "vfat", "mountpoint": None,
         "label": "TX"}]}]}


class UsbAutorunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_candidates_only_usb_never_sd(self):
        found = ua.usb_candidates(LSBLK)
        self.assertEqual([f["path"] for f in found], ["/dev/sda1"])

    def test_candidates_whole_disk_without_partition_table(self):
        data = {"blockdevices": [{"path": "/dev/sdb", "type": "disk", "tran": "usb",
                                  "fstype": "exfat"}]}
        self.assertEqual(ua.usb_candidates(data)[0]["path"], "/dev/sdb")

    def test_no_usb(self):
        self.assertEqual(ua.usb_candidates({"blockdevices": []}), [])

    def test_root_txt_ignores_hidden_and_dirs(self):
        for name in ("a.txt", "B.TXT", "._a.txt", "x.bin"):
            (self.root / name).write_bytes(b"x")
        (self.root / "dir.txt").mkdir()
        self.assertEqual([p.name for p in ua.root_txt_files(self.root)], ["B.TXT", "a.txt"])

    def test_copy_never_overwrites(self):
        src = self.root / "s.txt"
        src.write_bytes(b"hello")
        dst = self.root / "d.txt"
        dst.write_bytes(b"other")
        with self.assertRaises(FileExistsError):
            ua.copy_new_verified(src, dst)
        self.assertEqual(dst.read_bytes(), b"other")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".pt-")], [])

    def test_copy_ok_and_verified(self):
        src = self.root / "s.txt"
        src.write_bytes(b"line\n")
        dst = self.root / "d.txt"
        ua.copy_new_verified(src, dst)
        self.assertEqual(dst.read_bytes(), b"line\n")

    def test_copy_detects_mismatch_and_cleans(self):
        src = self.root / "s.txt"
        src.write_bytes(b"abc")
        dst = self.root / "d.txt"
        with mock.patch.object(ua, "same_bytes", return_value=False):
            with self.assertRaises(ua.FlowError):
                ua.copy_new_verified(src, dst)
        self.assertFalse(dst.exists())
        self.assertEqual(list(self.root.glob(".pt-*")), [])

    def test_stage_tx_clears_old_and_copies(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "old.txt").write_bytes(b"old")
        src = self.root / "in.txt"
        src.write_bytes(b"new")
        staged = ua.stage_tx(src, stage)
        self.assertEqual(staged.read_bytes(), b"new")
        self.assertEqual([p.name for p in stage.iterdir()], ["tx.txt"])

    def test_next_rx_path_increments(self):
        rx = self.root / "rx"
        first = ua.next_rx_path(rx)
        first.write_bytes(b"1")
        self.assertEqual(ua.next_rx_path(rx).name, "rx_0002.txt")

    def test_save_to_usb_new_identical_and_different(self):
        usb = self.root / "usb"
        usb.mkdir()
        rec = self.root / "rx.txt"
        rec.write_bytes(b"data")
        first = ua.save_to_usb(rec, usb)
        self.assertEqual(first, usb / "received" / "rx_0001.txt")
        self.assertEqual(ua.save_to_usb(rec, usb), first)  # identical: no second copy
        self.assertEqual(len(list((usb / "received").glob("*.txt"))), 1)
        rec.write_bytes(b"other")
        second = ua.save_to_usb(rec, usb)
        self.assertEqual(second.name, "rx_0002.txt")
        self.assertEqual(first.read_bytes(), b"data")  # never overwritten

    def test_config_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ua.FlowError):
                ua.config()
        with mock.patch.dict(os.environ, {"PT_RATE": "250k"}, clear=True):
            with self.assertRaises(ua.FlowError):
                ua.config()
        env = {"PT_RATE": "2m", "PT_HARDWARE_CHECKED": "yes"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ua.config(), ("2m", ["--batch"]))

    def test_radio_command_has_explicit_gates(self):
        command = ua.radio_command("rx", "/x/rx_0001.txt", "250k", ["--batch"])
        self.assertEqual(command[2:4], ["rx", "/x/rx_0001.txt"])
        self.assertIn("--hardware-checked", command)
        self.assertEqual(command[command.index("--rate") + 1], "250k")

    def _cycle(self, usb_files, radio_code=0, rx_writes=None):
        usb = self.root / "usb"
        usb.mkdir(exist_ok=True)
        for name, data in usb_files.items():
            (usb / name).write_bytes(data)
        calls = []

        def fake_radio(role, path, rate, extra):
            calls.append((role, Path(path)))
            if role == "rx" and rx_writes is not None and radio_code == 0:
                Path(path).write_bytes(rx_writes)
            return radio_code

        dev = {"path": "/dev/sda1", "fstype": "vfat", "mountpoint": None}
        env = {"PT_RATE": "250k", "PT_HARDWARE_CHECKED": "yes"}
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(ua, "MOUNT", usb),
                   mock.patch.object(ua, "STATE", self.root / "state"),
                   mock.patch.object(ua, "mount_usb"), mock.patch.object(ua, "unmount_usb"),
                   mock.patch.object(ua, "remount_rw"),
                   mock.patch.object(ua, "find_usb", return_value=dev),
                   mock.patch.object(ua, "run_radio", fake_radio)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return ua.cycle(dev), calls, usb

    def test_cycle_tx_stages_to_sd_then_sends(self):
        ok, calls, usb = self._cycle({"msg.txt": b"hi"})
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "tx")
        self.assertEqual(calls[0][1].read_bytes(), b"hi")
        self.assertTrue(str(calls[0][1]).startswith(str(self.root / "state")))

    def test_cycle_tx_ambiguous_stick_fails_before_radio(self):
        with self.assertRaises(ua.FlowError):
            self._cycle({"a.txt": b"1", "b.txt": b"2"})
        # radio must not have been started

    def test_cycle_tx_failure_reported(self):
        with self.assertRaises(ua.FlowError):
            self._cycle({"msg.txt": b"hi"}, radio_code=1)

    def test_cycle_rx_saves_on_sd_then_copies_to_usb(self):
        ok, calls, usb = self._cycle({}, rx_writes=b"got")
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "rx")
        self.assertTrue(str(calls[0][1]).startswith(str(self.root / "state")))
        self.assertEqual((usb / "received" / "rx_0001.txt").read_bytes(), b"got")
        state_rx = self.root / "state" / "rx"
        self.assertEqual(sorted(p.name for p in state_rx.iterdir()), ["rx_0001.delivered"])

    def test_cycle_rx_failure_writes_nothing_to_usb(self):
        with self.assertRaises(ua.FlowError):
            self._cycle({}, radio_code=1)
        self.assertFalse((self.root / "usb" / "received").exists())

    def test_find_usb_requests_tree_output(self):
        import json
        with mock.patch.object(ua, "run") as fake:
            fake.return_value.stdout = json.dumps(LSBLK)
            self.assertEqual(ua.find_usb()["path"], "/dev/sda1")
        self.assertIn("--tree", fake.call_args[0])

    def test_step_without_config_never_touches_led(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(ua, "show") as show, \
                mock.patch.object(ua, "find_usb", side_effect=ua.FlowError("x")), \
                mock.patch.object(ua.time, "sleep"):
            ua.step()
        show.assert_not_called()

    def test_failed_delivery_is_retried_without_radio(self):
        with self.assertRaises(OSError):
            with mock.patch.object(ua, "save_to_usb", side_effect=OSError("boom")):
                self._cycle({}, rx_writes=b"got")
        pending = list((self.root / "state" / "rx").glob("rx_*.txt"))
        self.assertEqual(len(pending), 1)
        radio = mock.Mock()
        usb = self.root / "usb"
        dev = {"path": "/dev/sda1", "fstype": "vfat", "mountpoint": None}
        with mock.patch.object(ua, "run_radio", radio):
            self.assertTrue(ua.cycle(dev))
        radio.assert_not_called()
        self.assertEqual((usb / "received" / "rx_0001.txt").read_bytes(), b"got")
        self.assertEqual(list((self.root / "state" / "rx").glob("rx_*.txt")), [])

    def test_never_deliver_to_stick_with_root_txt(self):
        with self.assertRaises(OSError):
            with mock.patch.object(ua, "save_to_usb", side_effect=OSError("boom")):
                self._cycle({}, rx_writes=b"got")
        (self.root / "usb" / "tx.txt").write_bytes(b"mine")
        with mock.patch.object(ua, "save_to_usb") as save, \
                mock.patch.object(ua, "stage_tx", return_value=Path("x")), \
                mock.patch.object(ua, "run_radio", return_value=0):
            ua.cycle({"path": "/dev/sdb1", "fstype": "vfat", "mountpoint": None})
        save.assert_not_called()
        with self.assertRaises(ua.FlowError):
            ua.deliver_pending({"path": "/dev/sdb1", "fstype": "vfat", "mountpoint": None},
                               self.root / "state" / "rx")

    def test_received_symlink_rejected(self):
        usb = self.root / "usb"
        usb.mkdir()
        sd = self.root / "sd"
        sd.mkdir()
        (usb / "received").symlink_to(sd)
        rec = self.root / "rx.txt"
        rec.write_bytes(b"x")
        with self.assertRaises(ua.FlowError):
            ua.save_to_usb(rec, usb)

    def test_txt_symlink_not_counted_as_copy(self):
        usb = self.root / "usb"
        (usb / "received").mkdir(parents=True)
        rec = self.root / "rx.txt"
        rec.write_bytes(b"x")
        (usb / "received" / "rx_0001.txt").symlink_to(rec)
        target = ua.save_to_usb(rec, usb)
        self.assertNotEqual(target.name, "rx_0001.txt")
        self.assertFalse(target.is_symlink())

    def test_radio_command_auto_finish(self):
        self.assertIn("--auto-finish", ua.radio_command("tx", "f.txt", "2m", []))

    def test_auto_finish_skips_green_and_press(self):
        import quick_mode
        led = quick_mode.ButtonLed("rx", interactive_led=True, auto_finish=True)
        with mock.patch.object(led, "color") as color, mock.patch.object(led, "_stable") as stable:
            led.result(True)
        color.assert_called_once_with((0, 0, 0))
        stable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
