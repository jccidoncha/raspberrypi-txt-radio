"""Offline release probes. Usage: python3 tests/probe_radio_release.py WHEEL_DIR.

Only hash-checked public wheels are imported; no install, daemon, GPIO or network.
"""
import hashlib
from pathlib import Path
import sys
import unittest


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 tests/probe_radio_release.py WHEEL_DIR")
    wheels = {
        "nrf24-2.0.0-py3-none-any.whl":
            "1c72094ccd5dee887b1f01ac444a4efa0ddd10139df9a8a542565dd878adfbc3",
        "pigpio-1.78-py2.py3-none-any.whl":
            "81e46f640c4e6342881fa9bbe290dbcd4fc179619dc6591e57a9d4a084dc49fa",
    }
    for name, digest in wheels.items():
        path = Path(sys.argv[1]).resolve() / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit("release wheel digest mismatch: " + name)
        sys.path.insert(0, str(path))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import nrf24
    import pigpio
    import test_radio_repair
    import radio_adapter as adapter
    import struct
    import threading
    from types import SimpleNamespace
    test_radio_repair.DRIVER = nrf24
    test_radio_repair.PIGPIO = pigpio

    class DaemonSocket(test_radio_repair.Socket):
        """Original in-memory daemon replies to the release's GPIO/SPI commands."""

        def __init__(self, clock, device):
            super().__init__(clock)
            self.device = device
            self.pending = bytearray()

        def send(self, data):
            super().send(data)
            command, p1, p2, size = struct.unpack("IIII", data[:16])
            payload = bytes(data[16:])
            if len(payload) != size:
                raise AssertionError("unexpected daemon command length")
            result, body = 0, b""
            if command == pigpio._PI_CMD_MODES:
                self.device.set_mode(p1, p2)
            elif command == pigpio._PI_CMD_WRITE:
                self.device.write(p1, p2)
            elif command == pigpio._PI_CMD_SPIO:
                result = self.device.spi_open(p1, p2)
            elif command == pigpio._PI_CMD_SPIC:
                self.device.spi_close(p1)
            elif command == pigpio._PI_CMD_SPIX:
                result, body = self.device.spi_xfer(p1, payload)
            else:
                raise AssertionError("unexpected daemon command " + str(command))
            self.pending.extend(struct.pack("IIII", 0, 0, 0, result) + body)
            return len(data)

        def recv(self, count):
            self.delay()
            result = bytes(self.pending[:count])
            del self.pending[:count]
            return result

    class CombinedReleaseTests(unittest.TestCase):
        def connection(self, clock, **kwargs):
            device = test_radio_repair.ObservedPi(clock, **kwargs)
            raw = DaemonSocket(clock, device)
            pi = pigpio.pi.__new__(pigpio.pi)
            pi.connected, pi._notify = True, None
            pi.sl = SimpleNamespace(s=raw, l=threading.Lock())
            return pi, raw, device

        def test_both_releases_latch_bit0_status_matrix(self):
            for bit0 in (0, 1):
                for role in ("tx", "rx"):
                    for rate in ("250k", "2m"):
                        for status in (0x20, 0x10, 0):
                            with self.subTest(bit0=bit0, role=role, rate=rate, status=status):
                                clock = test_radio_repair.Clock()
                                pi, raw, device = self.connection(clock, bit0=bit0,
                                                                  send_status=status)
                                with test_radio_repair.clocked(clock):
                                    with adapter.NrfRadio(pi, nrf24, role, rate) as radio:
                                        self.assertEqual(device.trace[0], ("write", False))
                                        raw.latency = 0.001
                                        if status == 0:
                                            # This timing expires inside a socket call.
                                            # A partial command stream must be closed.
                                            start = clock.now()
                                            with self.assertRaises(OSError):
                                                radio.send(b"x" * 32)
                                            self.assertAlmostEqual(clock.now() - start, 0.1)
                                            self.assertTrue(radio.closed)
                                            self.assertTrue(raw.closed)
                                            continue
                                        self.assertIs(radio.send(b"x" * 32), status == 0x20)
                                        device.payloads.append(b"y" * 32)
                                        self.assertEqual(radio.receive(0.1), b"y" * 32)
                                self.assertTrue(raw.closed)
                                self.assertEqual(device.latch, 0)

        def test_both_releases_failed_init_retained_high_latch(self):
            for rate in ("250k", "2m"):
                clock = test_radio_repair.Clock()
                pi, raw, device = self.connection(clock, wrong_readback=True)
                with test_radio_repair.clocked(clock), self.assertRaises(OSError):
                    adapter.NrfRadio(pi, nrf24, "rx", rate)
                self.assertFalse(any(high for _, high in device.trace))
                self.assertEqual(device.closes, [4])
                self.assertTrue(raw.closed)

        def test_both_releases_full_send_and_receive_deadlines(self):
            for rate in ("250k", "2m"):
                for operation, budget in (("send", 0.1), ("receive", 0.01)):
                    clock = test_radio_repair.Clock()
                    pi, raw, device = self.connection(clock)
                    with test_radio_repair.clocked(clock):
                        radio = adapter.NrfRadio(pi, nrf24, "tx", rate)
                        device.payloads.append(b"y" * 32)
                        raw.latency = 0.020
                        start = clock.now()
                        with self.assertRaises(OSError):
                            if operation == "send":
                                radio.send(b"x" * 32)
                            else:
                                radio.receive(budget)
                        self.assertAlmostEqual(clock.now() - start, budget)
                        self.assertTrue(radio.closed)
                        self.assertTrue(raw.closed)
                        sent = len(raw.sent)
                        radio.close()
                        self.assertEqual(len(raw.sent), sent)

        def test_both_releases_stalled_close_has_one_cleanup_budget(self):
            clock = test_radio_repair.Clock()
            pi, raw, device = self.connection(clock)
            with test_radio_repair.clocked(clock):
                radio = adapter.NrfRadio(pi, nrf24, "rx", "250k")
                raw.latency = 10
                start = clock.now()
                with self.assertRaisesRegex(OSError, "cleanup failed"):
                    radio.close()
                self.assertAlmostEqual(clock.now() - start, adapter.CLEANUP_TIMEOUT)
                self.assertTrue(raw.closed)

    suite = unittest.defaultTestLoader.loadTestsFromModule(test_radio_repair)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(CombinedReleaseTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
