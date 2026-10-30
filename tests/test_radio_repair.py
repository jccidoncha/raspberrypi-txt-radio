"""Scoped repair regressions; original fakes only, no daemon or hardware.

probe_radio_release.py also runs these against the hash-checked release wheels.
The latency model deliberately ignores socket budgets to exercise post-call
expiry checks; SocketTests separately model timeout-respecting blocking I/O.
"""
from collections import deque
from contextlib import contextmanager
import socket
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import radio_adapter as adapter
from test_radio import Clock, FakePi, FAKE_MODULE


DRIVER = FAKE_MODULE
PIGPIO = None  # Populated only by the explicit, offline release probe.


class Socket:
    def __init__(self, clock):
        self.clock = clock
        self.timeout = 1.0
        self.timeouts = []
        self.latency = 0
        self.replies = deque()
        self.closed = False
        self.sent = []

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def delay(self):
        if self.latency >= self.timeout:
            self.clock.sleep(self.timeout)
            raise socket.timeout("simulated stalled daemon")
        self.clock.sleep(self.latency)

    def send(self, data):
        self.delay()
        self.sent.append(data)
        return len(data)

    def sendall(self, data):
        self.send(data)

    def recv(self, count):
        self.delay()
        return self.replies.popleft()

    def shutdown(self, how):
        self.shutdown_how = how

    def close(self):
        self.closed = True


class ObservedPi(FakePi):
    def __init__(self, clock, bit0=1, corrupt=0, **kwargs):
        super().__init__(**kwargs)
        self.clock, self.bit0, self.corrupt = clock, bit0, corrupt
        self.latency = 0
        self.mode, self.latch = 0, 1  # Input, retained high output latch.
        self.trace = []
        self.sl.s = Socket(clock)

    def record(self, action):
        self.trace.append((action, self.mode == 1 and self.latch == 1))
        self.clock.sleep(self.latency)

    def set_mode(self, pin, mode):
        self.mode = mode
        self.record("set_mode")
        return super().set_mode(pin, mode)

    def write(self, pin, value):
        self.latch = value  # pigpio write clears latch before selecting OUTPUT.
        self.mode = 1
        self.record("write")
        return super().write(pin, value)

    def spi_open(self, channel, speed):
        self.record("spi_open")
        return super().spi_open(channel, speed)

    def spi_close(self, handle):
        self.record("spi_close")
        return super().spi_close(handle)

    def spi_xfer(self, handle, data):
        self.record("spi_xfer")
        count, result = super().spi_xfer(handle, data)
        if data[0] == 6:
            result = bytes([result[0], ((result[1] & 0xfe) | self.bit0) ^ self.corrupt])
        return count, result


@contextmanager
def clocked(clock):
    with patch("radio_adapter.time.monotonic", clock.now), \
            patch("radio_adapter.time.sleep", clock.sleep):
        yield


class RepairTests(unittest.TestCase):
    def test_retained_high_latch_failed_readback_both_rates(self):
        for rate in ("250k", "2m"):
            clock = Clock()
            pi = ObservedPi(clock, wrong_readback=True)
            with clocked(clock), self.assertRaisesRegex(OSError, "readback"):
                adapter.NrfRadio(pi, DRIVER, "rx", rate)
            self.assertEqual(pi.trace[0], ("write", False))
            self.assertIn(("set_mode", False), pi.trace)
            self.assertFalse(any(high for _, high in pi.trace))
            self.assertEqual(pi.closes, [4])
            self.assertTrue(pi.stopped)

    def test_ce_low_precedes_constructor_and_partial_failure(self):
        clock, seen = Clock(), []
        pi = ObservedPi(clock)

        def broken(io, **kwargs):
            seen.append(list(pi.trace))
            io.set_mode(25, 1)
            io.spi_open(0, 1000000)
            raise OSError("construction failed")

        module = SimpleNamespace(NRF24=broken, RF24_PA=DRIVER.RF24_PA,
                                 RF24_DATA_RATE=DRIVER.RF24_DATA_RATE)
        with clocked(clock), self.assertRaisesRegex(OSError, "construction"):
            adapter.NrfRadio(pi, module, "tx", "250k")
        self.assertEqual(seen, [[("write", False)]])
        self.assertFalse(any(high for _, high in pi.trace))
        self.assertEqual(pi.closes, [4])
        self.assertTrue(pi.stopped)

    def test_failed_safe_write_never_constructs_driver(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock), patch.object(pi, "write", return_value=-1), \
                patch.object(DRIVER, "NRF24") as constructor:
            with self.assertRaisesRegex(OSError, "CE write"):
                adapter.NrfRadio(pi, DRIVER, "tx", "250k")
        constructor.assert_not_called()
        self.assertEqual(pi.modes, [])
        self.assertEqual(pi.opens, [])
        self.assertTrue(pi.stopped)

    def test_both_bit0_values_roles_rates_and_send_statuses(self):
        for bit0 in (0, 1):
            for role in ("tx", "rx"):
                for rate in ("250k", "2m"):
                    for status, expected in ((0x20, True), (0x10, False),
                                             (0x30, False), (0, False)):
                        with self.subTest(bit0=bit0, role=role, rate=rate, status=status):
                            clock = Clock()
                            pi = ObservedPi(clock, bit0=bit0, send_status=status)
                            with clocked(clock), adapter.NrfRadio(pi, DRIVER, role, rate) as radio:
                                pi.latency = 0.001
                                self.assertIs(radio.send(b"x" * 32), expected)
                                pi.payloads.append(b"y" * 32)
                                self.assertEqual(radio.receive(0.1), b"y" * 32)
                            self.assertTrue(pi.stopped)
                            self.assertEqual(pi.latch, 0)

    def test_every_other_rf_setup_bit_still_checked(self):
        for rate in ("250k", "2m"):
            for bit in range(1, 8):
                with self.subTest(rate=rate, bit=bit):
                    clock = Clock()
                    pi = ObservedPi(clock, corrupt=1 << bit)
                    with clocked(clock), self.assertRaisesRegex(OSError, "readback"):
                        adapter.NrfRadio(pi, DRIVER, "rx", rate)
                    self.assertFalse(any(high for _, high in pi.trace))

    def test_send_includes_preparation_with_nonzero_io_latency(self):
        for rate in ("250k", "2m"):
            clock = Clock()
            pi = ObservedPi(clock)
            with clocked(clock), adapter.NrfRadio(pi, DRIVER, "tx", rate) as radio:
                pi.latency = 0.020
                start = clock.now()
                self.assertFalse(radio.send(b"x" * 32))
                self.assertLessEqual(clock.now() - start,
                                     adapter.SEND_TIMEOUT + adapter.CLEANUP_TIMEOUT)
                self.assertEqual(pi.regs[0], [0x0f])
                self.assertTrue(any(0 < x < 0.020 for x in pi.sl.s.timeouts))

    def test_late_status_cannot_report_success(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock), adapter.NrfRadio(pi, DRIVER, "tx", "250k") as radio:
            def late():
                clock.sleep(adapter.SEND_TIMEOUT)
                return 0x20
            with patch.object(radio.driver, "get_status", side_effect=late):
                self.assertFalse(radio.send(b"x" * 32))

    def test_receive_deadline_covers_ready_and_payload_io(self):
        for timeout in (0.01, 0.05, 0.09):
            clock = Clock()
            pi = ObservedPi(clock)
            with clocked(clock), adapter.NrfRadio(pi, DRIVER, "rx", "250k") as radio:
                pi.payloads.append(b"x" * 32)
                pi.latency = 0.020
                start = clock.now()
                result = radio.receive(timeout)
                elapsed = clock.now() - start
                if elapsed >= timeout:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result, b"x" * 32)
                self.assertLessEqual(elapsed, timeout + 0.020 + adapter.CLEANUP_TIMEOUT)
                self.assertEqual(pi.regs[0], [0x0f])
                self.assertEqual(pi.latch, 1)

    def test_payload_completed_late_is_discarded_and_rx_restored(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock), adapter.NrfRadio(pi, DRIVER, "rx", "2m") as radio:
            pi.payloads.append(b"x" * 32)
            original = radio.driver.get_payload
            def late():
                payload = original()
                clock.sleep(0.02)
                return payload
            with patch.object(radio.driver, "get_payload", side_effect=late):
                self.assertIsNone(radio.receive(0.01))
            self.assertEqual(pi.latch, 1)
            self.assertEqual(pi.regs[0], [0x0f])

    def test_zero_receive_budget_does_no_io(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock), adapter.NrfRadio(pi, DRIVER, "rx", "2m") as radio:
            before = list(pi.trace)
            self.assertIsNone(radio.receive(0))
            self.assertIsNone(radio.receive(-1))
            self.assertEqual(pi.trace, before)

    def test_close_budget_is_shared_and_still_disconnects(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock):
            radio = adapter.NrfRadio(pi, DRIVER, "rx", "250k")
            pi.latency = adapter.CLEANUP_TIMEOUT
            before = len(pi.trace)
            start = clock.now()
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                radio.close()
            self.assertAlmostEqual(clock.now() - start, adapter.CLEANUP_TIMEOUT)
            self.assertEqual(len(pi.trace) - before, 1)
            self.assertTrue(pi.stopped)
            radio.close()

    def test_recovery_budget_does_not_start_second_cleanup(self):
        clock = Clock()
        pi = ObservedPi(clock)
        with clocked(clock):
            radio = adapter.NrfRadio(pi, DRIVER, "tx", "250k")
            original = radio.driver.unset_ce
            def stalled_recovery():
                pi.latency = adapter.CLEANUP_TIMEOUT
                original()
            # Make the send itself finish first; then stall the recovery's first write.
            with patch.object(radio.driver, "send"), \
                    patch.object(radio.driver, "get_status", return_value=0x20), \
                    patch.object(radio.driver, "unset_ce", side_effect=stalled_recovery):
                start = clock.now()
                with self.assertRaisesRegex(OSError, "recovery failed"):
                    radio.send(b"x" * 32)
            self.assertAlmostEqual(clock.now() - start, 0.005 + adapter.CLEANUP_TIMEOUT)
            self.assertTrue(radio.closed)
            self.assertTrue(pi.stopped)
            before = len(pi.trace)
            radio.close()
            self.assertEqual(len(pi.trace), before)


class SocketTests(unittest.TestCase):
    def connection(self, clock):
        raw = Socket(clock)
        if PIGPIO is None:
            pi = FakePi()
        else:
            # Use actual pigpio methods without invoking its connecting constructor.
            import threading
            pi = PIGPIO.pi.__new__(PIGPIO.pi)
            pi.connected = True
            pi._notify = None
            pi.sl = SimpleNamespace(l=threading.Lock())
        pi.sl.s = raw
        return pi, raw, adapter._PiIO(pi)

    def test_each_socket_phase_uses_remaining_budget(self):
        clock = Clock()
        pi, raw, io = self.connection(clock)
        raw.latency = 0.020
        raw.replies.extend([struct.pack("IIII", 0, 0, 0, 2), b"a", b"b"])
        with clocked(clock), io.budget(0.1):
            if PIGPIO is None:
                pi.sl.s.sendall(b"command")
                pi.sl.s.recv(16)
                pi.sl.s.recv(2)
                pi.sl.s.recv(1)
            else:
                self.assertEqual(io.spi_xfer(4, [0xff, 0]), (2, bytearray(b"ab")))
        self.assertAlmostEqual(clock.now(), 0.080)
        self.assertAlmostEqual(raw.timeouts[-1], 0.040)
        io.disconnect()

    def test_trickled_payload_expires_within_total_budget(self):
        clock = Clock()
        pi, raw, io = self.connection(clock)
        raw.latency = 0.020
        raw.replies.extend([struct.pack("IIII", 0, 0, 0, 2), b"a", b"b"])
        with clocked(clock), self.assertRaises(TimeoutError):
            with io.budget(0.070):
                if PIGPIO is None:
                    pi.sl.s.sendall(b"command")
                    pi.sl.s.recv(16)
                    pi.sl.s.recv(2)
                    pi.sl.s.recv(1)
                else:
                    io.spi_xfer(4, [0xff, 0])
        self.assertAlmostEqual(clock.now(), 0.070)
        self.assertTrue(io.broken)
        with self.assertRaisesRegex(OSError, "stream failed"):
            io.write(25, 0)
        io.disconnect()

    def test_stalled_socket_does_not_get_one_second_per_call(self):
        clock = Clock()
        pi, raw, io = self.connection(clock)
        raw.latency = 10
        with clocked(clock), self.assertRaises(TimeoutError):
            with io.budget(0.010):
                if PIGPIO is None:
                    pi.sl.s.send(b"command")
                else:
                    io.write(25, 0)
        self.assertAlmostEqual(clock.now(), 0.010)
        self.assertTrue(io.broken)
        io.disconnect()

    def test_expiry_between_command_and_reply_invalidates_stream(self):
        clock = Clock()
        pi, raw, io = self.connection(clock)
        with clocked(clock), self.assertRaises(TimeoutError):
            with io.budget(0.010):
                pi.sl.s.send(b"command")
                clock.sleep(0.010)
                pi.sl.s.recv(16)
        self.assertTrue(io.broken)
        self.assertEqual(raw.sent, [b"command"])
        io.disconnect()

    def test_disconnect_never_sends_on_notification_socket(self):
        clock = Clock()
        pi, raw, io = self.connection(clock)
        notification = Socket(clock)
        notification.latency = 100
        notify = SimpleNamespace(go=True, sl=SimpleNamespace(s=notification))
        pi._notify = notify
        with clocked(clock):
            io.disconnect()
        self.assertFalse(notify.go)
        self.assertIsNone(pi._notify)
        self.assertTrue(notification.closed)
        self.assertEqual(notification.shutdown_how, socket.SHUT_RDWR)
        self.assertEqual(notification.sent, [])
        self.assertEqual(clock.now(), 0)
        if PIGPIO is not None:
            self.assertTrue(raw.closed)


if __name__ == "__main__":
    unittest.main()
