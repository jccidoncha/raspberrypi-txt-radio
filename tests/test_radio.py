"""PT/1 host fault tests, filesystem tests and an original fake SPI device.

No radio, GPIO, daemon or unlicensed reference code is executed.
"""
from collections import Counter, deque
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

import quick_mode
import radio_adapter as adapter
import transfer as t


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0, seconds)


class Exchange:
    """Run the real pure receiver behind a clocked lossy fake radio."""

    def __init__(self, clock, rate="250k", drop_requests=(), drop_replies=()):
        self.clock = clock
        self.receiver = t.Receiver(rate)
        self.saved = []
        self.pending = deque()
        self.sent = Counter()
        self.drop_requests = set(drop_requests)
        self.drop_replies = set(drop_replies)

    def send(self, frame):
        kind, session, sequence, body = t.unpack_packet(frame)
        self.sent[sequence] += 1
        attempt = (sequence, self.sent[sequence])
        if attempt in self.drop_requests:
            return False
        reply = self.receiver.accept(frame, self.saved.append)
        if reply is not None and attempt not in self.drop_replies:
            self.pending.append(reply)
        return False  # Deliberately no link ACK; application replies are authoritative.

    def receive(self, timeout):
        if self.pending:
            self.clock.sleep(0.001)
            return self.pending.popleft()
        self.clock.sleep(timeout)
        return None


class Incoming:
    def __init__(self, clock, frames=(), repeat=None):
        self.clock, self.frames = clock, deque(frames)
        self.repeat = repeat
        self.sent = []

    def receive(self, timeout):
        if self.frames or self.repeat is not None:
            self.clock.sleep(min(timeout, 0.01))
            return self.frames.popleft() if self.frames else self.repeat
        self.clock.sleep(timeout)
        return None

    def send(self, frame):
        self.sent.append(frame)
        return True


class ProtocolTests(unittest.TestCase):
    def test_crc_vectors_and_independent_implementation(self):
        self.assertEqual(t.crc32(b"123456789"), 0xcbf43926)
        for data in (b"", bytes(range(256)), b"\0EOF\xff" * 100):
            self.assertEqual(t.crc32(data), zlib.crc32(data))

    def test_explicit_wire_vector(self):
        frame = next(iter(t.v1_frames(b"", 0x0102030405060708, "250k")))
        self.assertEqual(frame.hex(),
                         "50540101010203040506070800000000" + "00000000000000000100000000000000")
        with self.assertRaises(t.TransferError):
            t.unpack_packet(t.encode(b"hello")[0])

    def test_roundtrip_lengths_zeros_and_sequence_above_256(self):
        for rate in ("250k", "2m"):
            for length in (0, 1, 15, 16, 17, 4097, t.MAX_BYTES):
                with self.subTest(rate=rate, length=length):
                    data = (bytes(range(256)) * (length // 256 + 1))[:length]
                    receiver, saved = t.Receiver(rate), []
                    last = None
                    for frame in t.v1_frames(data, 17, rate):
                        self.assertEqual(len(frame), 32)
                        last = receiver.accept(frame, saved.append)
                    self.assertEqual(saved, [data])
                    self.assertEqual(t.unpack_packet(last)[0], t.SAVED)

    def test_loss_and_duplicates_initial_plus_two_repairs(self):
        clock = Clock()
        radio = Exchange(clock, drop_requests={(0, 1), (1, 1)},
                         drop_replies={(0, 2), (1, 2), (3, 1), (3, 2)})
        data = b"a" * 17
        self.assertEqual(t.send_v1(data, 7, "250k", radio, clock.now), 4)
        self.assertEqual(radio.saved, [data])
        self.assertEqual(radio.sent[0], 3)
        self.assertEqual(radio.sent[1], 3)
        self.assertEqual(radio.sent[3], 3)

    def test_exhausted_data_never_skips(self):
        clock = Clock()
        radio = Exchange(clock, drop_requests={(1, n) for n in (1, 2, 3)})
        with self.assertRaisesRegex(t.TransferError, "unconfirmed"):
            t.send_v1(b"a" * 40, 7, "250k", radio, clock.now)
        self.assertEqual(radio.sent, {0: 1, 1: 3})
        self.assertEqual(radio.saved, [])

    def test_lost_all_receipts_is_uncertain_despite_saved_file(self):
        clock = Clock()
        radio = Exchange(clock, drop_replies={(1, n) for n in (1, 2, 3)})
        with self.assertRaisesRegex(t.TransferError, "unconfirmed"):
            t.send_v1(b"", 7, "250k", radio, clock.now)
        self.assertEqual(radio.saved, [b""])
        self.assertEqual(radio.sent[1], 3)

    def test_wrong_rate_foreign_session_order_and_legacy_ignored(self):
        frames = list(t.v1_frames(b"a" * 17, 7, "250k"))
        receiver, saved = t.Receiver("250k"), []
        bad_start = next(iter(t.v1_frames(b"x", 8, "2m")))
        for frame in (bad_start, frames[1], b"\0" * 4,
                      t.packet(t.START, 8, 0, struct.pack("!IIB", t.MAX_BYTES + 1, 0, 1))):
            self.assertIsNone(receiver.accept(frame, saved.append))
        receiver.accept(frames[0], saved.append)
        for frame in (frames[2], t.packet(t.DATA, 8, 1, b"foreign")):
            self.assertIsNone(receiver.accept(frame, saved.append))
        for frame in frames[1:]:
            receiver.accept(frame, saved.append)
        self.assertEqual(saved, [b"a" * 17])

    def test_corruption_and_premature_finish_never_save(self):
        frames = list(t.v1_frames(b"a" * 16, 7, "250k"))
        for corrupt in (t.packet(t.DATA, 7, 1, b"b" * 16),
                        t.packet(t.FINISH, 7, 1, frames[0][16:])):
            receiver, saved = t.Receiver("250k"), []
            receiver.accept(frames[0], saved.append)
            with self.assertRaises(t.TransferError):
                receiver.accept(corrupt, saved.append)
                receiver.accept(frames[-1], saved.append)
            self.assertEqual(saved, [])

    def test_nonzero_padding_and_mismatched_finish_rejected(self):
        frames = list(t.v1_frames(b"a", 7, "250k"))
        receiver = t.Receiver("250k")
        receiver.accept(frames[0], self.fail)
        with self.assertRaises(t.TransferError):
            receiver.accept(frames[1][:-1] + b"x", self.fail)
        receiver.accept(frames[1], self.fail)
        with self.assertRaises(t.TransferError):
            receiver.accept(frames[-1][:-1] + b"x", self.fail)

    def test_save_error_never_issues_saved_reply(self):
        receiver = t.Receiver("250k")
        frames = list(t.v1_frames(b"", 7, "250k"))
        receiver.accept(frames[0], self.fail)
        with self.assertRaises(OSError):
            receiver.accept(frames[-1], lambda data: (_ for _ in ()).throw(OSError("disk")))
        self.assertFalse(receiver.saved)
        self.assertEqual(t.unpack_packet(receiver.last_reply)[0], t.ACK)

    def test_receiver_timeouts_noise_and_duplicate_flood(self):
        start = next(iter(t.v1_frames(b"a", 7, "250k")))
        for frames, repeat, limit in (((), None, t.START_TIMEOUT),
                                      ((), b"noise", t.START_TIMEOUT),
                                      ((start,), start, t.IDLE_TIMEOUT + 0.02)):
            clock = Clock()
            with self.assertRaises(t.TransferError):
                t.receive_v1("250k", Incoming(clock, frames, repeat), self.fail, clock.now)
            self.assertLessEqual(clock.now(), limit + 0.01)

    def test_receiver_lingers_but_only_saves_once(self):
        frames = list(t.v1_frames(b"exact\0\0", 7, "2m"))
        clock, saved = Clock(), []
        radio = Incoming(clock, frames + [frames[-1]] * 2)
        self.assertEqual(t.receive_v1("2m", radio, saved.append, clock.now), 7)
        self.assertEqual(saved, [b"exact\0\0"])
        self.assertEqual(sum(t.unpack_packet(f)[0] == t.SAVED for f in radio.sent), 3)
        self.assertLess(clock.now(), t.RECEIPT_LINGER + 1)

    def test_tx_total_deadline_even_with_slow_progress(self):
        clock = Clock()
        radio = Exchange(clock)
        receive = radio.receive

        def slow(timeout):
            clock.sleep(0.9)
            return receive(timeout)

        radio.receive = slow
        with self.assertRaises(t.TransferError):
            t.send_v1(b"a" * 10000, 7, "250k", radio, clock.now)
        self.assertLess(clock.now(), t.TOTAL_TIMEOUT + 1)
        self.assertEqual(radio.saved, [])

    def test_bad_version_session_and_size(self):
        good = t.packet(t.DATA, 7, 1, b"EOF\0")
        for frame in (good[:-1], good + b"x", b"XX" + good[2:],
                      good[:2] + b"\x02" + good[3:], good[:4] + b"\0" * 8 + good[12:]):
            with self.assertRaises(t.TransferError):
                t.unpack_packet(frame)
        with self.assertRaises(ValueError):
            list(t.v1_frames(b"a" * (t.MAX_BYTES + 1), 1, "2m"))

    def test_late_finish_cannot_save_after_receiver_deadline(self):
        clock, saved = Clock(), []
        frames = list(t.v1_frames(b"", 7, "250k"))
        radio = Incoming(clock, frames)
        receive = radio.receive

        def late(timeout):
            result = receive(timeout)
            if result == frames[-1]:
                clock.sleep(t.IDLE_TIMEOUT + 1)
            return result

        radio.receive = late
        with self.assertRaises(t.TransferError):
            t.receive_v1("250k", radio, saved.append, clock.now)
        self.assertEqual(saved, [])

    def test_stale_ack_and_late_reply_cannot_confirm_tx(self):
        for late in (False, True):
            clock = Clock()
            radio = Exchange(clock)
            receive = radio.receive

            def invalid_reply(timeout):
                reply = receive(timeout)
                if late:
                    clock.sleep(timeout + 0.01)
                    return reply
                if reply is not None:
                    return t.packet(t.ACK, 999, 0, bytes([t.START]))
                return None

            radio.receive = invalid_reply
            with self.assertRaises(t.TransferError):
                t.send_v1(b"hello", 7, "250k", radio, clock.now)
            self.assertEqual(radio.sent, {0: 3})
            self.assertEqual(radio.saved, [])

    def test_receiver_total_deadline_despite_valid_progress(self):
        clock, saved = Clock(), []
        radio = Incoming(clock, t.v1_frames(b"a" * 1600, 7, "250k"))
        with patch("transfer.TOTAL_TIMEOUT", 0.1):
            with self.assertRaises(t.TransferError):
                t.receive_v1("250k", radio, saved.append, clock.now)
        self.assertEqual(saved, [])
        self.assertLessEqual(clock.now(), 0.11)


class FileCliTests(unittest.TestCase):
    def test_atomic_save_and_no_overwrite_including_race_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.txt"
            with quick_mode.CompletedFile(target) as output:
                self.assertFalse(target.exists())
                output.save(b"original\0")
                self.assertEqual(target.read_bytes(), b"original\0")
            with self.assertRaises(FileExistsError):
                quick_mode.CompletedFile(target)
            new = Path(folder) / "race.txt"
            with quick_mode.CompletedFile(new) as output:
                new.symlink_to(target)
                with self.assertRaises(FileExistsError):
                    output.save(b"overwrite")
            self.assertEqual(target.read_bytes(), b"original\0")
            self.assertEqual(list(Path(folder).glob("*.partial")), [])

    def test_cleanup_on_timeout_interrupt_and_disk_error(self):
        with tempfile.TemporaryDirectory() as folder:
            for failure in (t.TransferError("timeout"), KeyboardInterrupt(), OSError("disk")):
                target = Path(folder) / "result.txt"
                with self.assertRaises(type(failure)):
                    with quick_mode.CompletedFile(target):
                        raise failure
                self.assertEqual(list(Path(folder).iterdir()), [])
            with quick_mode.CompletedFile(target) as output:
                with patch("quick_mode.os.fsync", side_effect=OSError("disk")):
                    with self.assertRaises(OSError):
                        output.save(b"hello")
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_paths_optin_and_rate_fail_before_hardware(self):
        with patch("radio_adapter.open_radio") as opening:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "source.txt"
                path.write_bytes(b"data")
                with redirect_stderr(io.StringIO()):
                    for args in (("tx", str(path), "--rate", "250k"),
                                 ("rx", str(path), "--hardware-checked"),
                                 ("tx", str(path), "--rate", "1m", "--hardware-checked")):
                        with self.assertRaises(SystemExit):
                            quick_mode.main(args)
                    for role, name in (("tx", "missing.txt"), ("tx", "wrong.bin"),
                                       ("rx", "source.txt"), ("rx", "wrong.bin")):
                        self.assertEqual(quick_mode.main(
                            [role, str(Path(folder) / name), "--rate", "250k",
                             "--hardware-checked"]), 1)
                opening.assert_not_called()

    def test_fifo_and_oversized_input_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.txt"
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                quick_mode.read_txt(path)
            path.unlink()
            path.write_bytes(b"a" * (t.MAX_BYTES + 1))
            with self.assertRaises(ValueError):
                quick_mode.read_txt(path)

    def test_cli_tx_and_rx_with_fake_radio(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "in.txt", Path(folder) / "out.txt"
            source.write_bytes(b"hello\0")
            for role, path in (("tx", source), ("rx", target)):
                clock = Clock()
                radio = (Exchange(clock) if role == "tx" else Incoming(
                    clock, t.v1_frames(b"hello\0", 7, "250k")))
                manager = unittest.mock.MagicMock()
                manager.__enter__.return_value = radio
                stdout = io.StringIO()
                with patch("radio_adapter.open_radio", return_value=manager), \
                        patch("quick_mode.time.monotonic", clock.now), \
                        redirect_stdout(stdout):
                    self.assertEqual(quick_mode.main(
                        [role, str(path), "--rate", "250k", "--hardware-checked"]), 0)
                self.assertIn('"protocol": "PT/1"', stdout.getvalue())
            self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_import_has_no_driver_or_device_effects(self):
        code = """
import sys
class DenyDriver:
    def find_spec(self, name, path=None, target=None):
        if name in ('pigpio', 'nrf24'):
            raise RuntimeError('driver import forbidden')
sys.meta_path.insert(0, DenyDriver())
import transfer, quick_mode, radio_adapter
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_timeout_cleans_stage_and_closes_radio(self):
        clock = Clock()
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = Incoming(clock)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out.txt"
            with patch("radio_adapter.open_radio", return_value=manager), \
                    patch("quick_mode.time.monotonic", clock.now), \
                    redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(quick_mode.main(
                    ["rx", str(target), "--rate", "250k", "--hardware-checked"]), 1)
            self.assertEqual(list(Path(folder).iterdir()), [])
            manager.__exit__.assert_called_once()

    def test_directory_fsync_failure_retains_complete_file_without_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out.txt"
            receiver = t.Receiver("250k")
            with quick_mode.CompletedFile(target) as output:
                frames = list(t.v1_frames(b"done", 7, "250k"))
                for frame in frames[:-1]:
                    receiver.accept(frame, output.save)
                with patch("quick_mode.os.fsync", side_effect=[None, OSError("directory")]):
                    with self.assertRaises(OSError):
                        receiver.accept(frames[-1], output.save)
            self.assertEqual(target.read_bytes(), b"done")
            self.assertFalse(receiver.saved)
            self.assertEqual(list(Path(folder).iterdir()), [target])


class FakePi:
    """Minimal register/FIFO model, reusable with the actual released driver."""

    def __init__(self, wrong_readback=False, send_status=0x20):
        self.regs = {0: [0x0c], 1: [0x3f], 2: [3], 3: [3], 6: [0x8f]}
        self.wrong_readback, self.send_status = wrong_readback, send_status
        self.status = 0
        self.writes, self.modes, self.opens, self.closes = [], [], [], []
        self.payloads = deque()
        self.stopped = False
        self.connected = True
        self.sl = SimpleNamespace(s=SimpleNamespace(settimeout=lambda value: None))

    def set_mode(self, pin, mode):
        self.modes.append((pin, mode))

    def write(self, pin, value):
        self.writes.append((pin, value))
        if value == 1 and self.regs.get(0, [0])[0] & 1 == 0:
            self.status = self.send_status

    def spi_open(self, channel, speed):
        self.opens.append((channel, speed))
        return 4

    def spi_close(self, handle):
        self.closes.append(handle)

    def stop(self):
        self.stopped = True

    def spi_xfer(self, handle, data):
        command, result = data[0], [self.status] + [0] * (len(data) - 1)
        if 0x20 <= command <= 0x3f:
            register = command & 31
            if register == 7:
                self.status &= ~data[1]
            else:
                self.regs[register] = list(data[1:])
        elif command <= 0x1f:
            if command == 0x17:
                values = [0 if self.payloads else 1]
            else:
                values = self.regs.get(command, [0] * (len(data) - 1))
            result[1:] = values[:len(data) - 1]
            if self.wrong_readback and command == 5:
                result[1] = 76
        elif command == 0x61:
            result[1:] = list(self.payloads.popleft())
        elif command == 0xe2:
            self.payloads.clear()
        return len(result), bytes(result)


class FakeDriver:
    """Only the adapter's release API contract; SPI is modelled by FakePi."""

    def __init__(self, pi, **kwargs):
        self.pi, self.kwargs = pi, kwargs
        pi.set_mode(kwargs["ce"], 1)
        pi.write(25, 0)
        self.handle = pi.spi_open(kwargs["spi_channel"], kwargs["spi_speed"])
        self.reg(3, kwargs["address_bytes"] - 2)
        self.reg(5, kwargs["channel"])
        pi.write(25, 1)  # Must be suppressed during construction.

    def reg(self, register, value):
        self.pi.spi_xfer(self.handle, [0x20 | register, value])

    def get_spi_handle(self):
        return self.handle

    def power_down(self):
        self.pi.write(25, 0)
        self.reg(0, 0x0c)

    def power_up_rx(self):
        self.reg(0, 0x0f)
        self.reg(7, 0x70)
        self.pi.write(25, 1)

    def set_retransmission(self, delay, count):
        self.reg(4, delay * 16 + count)

    def open_writing_pipe(self, address):
        self.writing = address
        self.reg(2, 1)
        self.reg(0x11, 32)

    def open_reading_pipe(self, pipe, address):
        self.reading = address
        self.reg(2, 3)
        self.reg(0x12, 32)

    def get_writing_address(self):
        return self.writing

    def get_reading_address(self, pipe):
        return self.reading

    def flush_rx(self):
        self.pi.spi_xfer(self.handle, [0xe2])

    def flush_tx(self):
        self.pi.spi_xfer(self.handle, [0xe1])

    def unset_ce(self):
        self.pi.write(25, 0)

    def send(self, frame):
        self.reg(7, 0x70)
        self.reg(0, 0x0e)
        self.pi.write(25, 1)

    def get_status(self):
        return self.pi.spi_xfer(self.handle, [0xff])[1][0]

    def data_ready(self):
        return not self.pi.spi_xfer(self.handle, [0x17, 0])[1][1] & 1

    def get_payload(self):
        return self.pi.spi_xfer(self.handle, [0x61] + [0] * 32)[1][1:]


FAKE_MODULE = SimpleNamespace(NRF24=FakeDriver,
                              RF24_PA=SimpleNamespace(MIN=0),
                              RF24_DATA_RATE=SimpleNamespace(RATE_250KBPS=2, RATE_2MBPS=1))


class AdapterTests(unittest.TestCase):
    def test_profiles_and_cleanup_both_rates_roles(self):
        for role in ("tx", "rx"):
            for rate, setup in (("250k", 0x21), ("2m", 0x09)):
                pi = FakePi()
                with patch("radio_adapter.time.sleep"):
                    with adapter.NrfRadio(pi, FAKE_MODULE, role, rate) as radio:
                        self.assertEqual(pi.regs[6], [setup])
                        self.assertEqual(pi.regs[5], [74])
                        self.assertEqual(pi.writes.count((25, 1)), 1)
                        self.assertEqual(pi.opens, [(0, 1000000)])
                        self.assertEqual(radio.driver.kwargs["pa_level"], 0)
                        self.assertEqual(radio.driver.writing, adapter.REQUEST_ADDRESS
                                         if role == "tx" else adapter.REPLY_ADDRESS)
                self.assertEqual(pi.writes[-1], (25, 0))
                self.assertEqual(pi.closes, [4])
                self.assertTrue(pi.stopped)

    def test_ack_max_retry_timeout_and_return_to_receive(self):
        for status, expected in ((0x20, True), (0x10, False), (0, False)):
            pi, clock = FakePi(send_status=status), Clock()
            with patch("radio_adapter.time.monotonic", clock.now), \
                    patch("radio_adapter.time.sleep", clock.sleep):
                with adapter.NrfRadio(pi, FAKE_MODULE, "tx", "250k") as radio:
                    self.assertIs(radio.send(b"x" * 32), expected)
                    self.assertEqual(pi.regs[0], [0x0f])
                    self.assertLess(clock.now(), 0.2)
                    pi.payloads.append(b"y" * 32)
                    self.assertEqual(radio.receive(0.1), b"y" * 32)

    def test_bad_readback_and_partial_init_keep_ce_low_and_cleanup(self):
        for module in (FAKE_MODULE, SimpleNamespace(**{**vars(FAKE_MODULE),
                                                       "NRF24": self.broken_driver})):
            pi = FakePi(wrong_readback=True)
            with self.assertRaises(OSError):
                adapter.NrfRadio(pi, module, "rx", "250k")
            self.assertNotIn((25, 1), pi.writes)
            self.assertEqual(pi.closes, [4])
            self.assertTrue(pi.stopped)

    @staticmethod
    def broken_driver(pi, **kwargs):
        pi.spi_open(0, 1000000)
        raise OSError("initialization failed")

    def test_optin_profile_and_dependency_validation_before_import(self):
        with patch.object(adapter.importlib, "import_module") as importing:
            for role, rate, checked in (("tx", "250k", False), ("rx", None, True),
                                        ("carrier", "2m", True)):
                with self.assertRaises(ValueError):
                    adapter.open_radio(role, rate, checked)
            with patch.object(adapter.metadata, "version", return_value="wrong"):
                with self.assertRaises(RuntimeError):
                    adapter.open_radio("tx", "2m", True)
            importing.assert_not_called()

    def test_loopback_explicit_ignores_environment(self):
        pi = FakePi()
        factory = unittest.mock.Mock(return_value=pi)
        modules = {"pigpio": SimpleNamespace(pi=factory), "nrf24": FAKE_MODULE}
        with patch.dict(os.environ, {"PIGPIO_ADDR": "remote", "PIGPIO_PORT": "9999"}), \
                patch.object(adapter.metadata, "version", side_effect=lambda n: {
                    "nrf24": "2.0.0", "pigpio": "1.78"}[n]), \
                patch.object(adapter.importlib, "import_module", side_effect=modules.get), \
                patch.object(adapter.time, "sleep"):
            with adapter.open_radio("rx", "2m", True):
                pass
        factory.assert_called_once_with("127.0.0.1", 8888, show_errors=False)

    def test_cleanup_continues_after_power_down_error(self):
        pi = FakePi()
        with patch("radio_adapter.time.sleep"):
            radio = adapter.NrfRadio(pi, FAKE_MODULE, "rx", "250k")
        radio.driver.power_down = unittest.mock.Mock(side_effect=OSError("daemon"))
        with self.assertRaises(OSError):
            radio.close()
        self.assertEqual(pi.writes[-1], (25, 0))
        self.assertEqual(pi.closes, [4])
        self.assertTrue(pi.stopped)


if __name__ == "__main__":
    unittest.main()
