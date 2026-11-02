"""Opt-in nrf24 2.0.0 / pigpio 1.78 adapter. Importing opens no devices.

Only the intended radio pins are used. LED and button access is absent.
Private register access is isolated here because two release helpers are faulty;
see reference.md. No source from either unlicensed reference is incorporated.
"""
import importlib
from contextlib import contextmanager
from importlib import metadata
import socket
import time

from transfer import RATE_CODES

CE = 25
CHANNEL = 74
REQUEST_ADDRESS = b"PTX01"
REPLY_ADDRESS = b"PRX01"
SEND_TIMEOUT = 0.1
CLEANUP_TIMEOUT = 0.2
IO_TIMEOUT = 1.0


class _DeadlineExpired(TimeoutError):
    """An operation budget expired between completed daemon commands."""


class _DeadlineSocket:
    """Apply the remaining budget to *each* send/recv in pigpio's SPI calls."""

    def __init__(self, raw, io):
        self.raw, self.io = raw, io

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _call(self, name, *args):
        try:
            self.raw.settimeout(self.io.remaining())
            result = getattr(self.raw, name)(*args)
            self.io.check()
            return result
        except OSError:
            # A partial command/reply cannot safely be followed by another command.
            self.io.broken = True
            raise

    def send(self, *args):
        return self._call("send", *args)

    def sendall(self, *args):
        return self._call("sendall", *args)

    def recv(self, *args):
        result = self._call("recv", *args)
        if not result:
            self.io.broken = True
            raise OSError("pigpio command connection closed")
        return result


class _PiIO:
    """Hold CE low until configuration passes; track partial-init SPI handles."""

    def __init__(self, pi):
        self.pi = pi
        self.enabled = False
        self.handles = []
        self.deadline = None
        self.broken = False
        pi.sl.s = _DeadlineSocket(pi.sl.s, self)

    def check(self):
        if self.broken:
            raise OSError("pigpio command stream failed; reopen radio")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise _DeadlineExpired("radio I/O deadline expired")

    def remaining(self):
        self.check()
        if self.deadline is None:
            return IO_TIMEOUT
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired("radio I/O deadline expired")
        return min(IO_TIMEOUT, remaining)

    @contextmanager
    def budget(self, timeout):
        previous = self.deadline
        self.deadline = time.monotonic() + max(0, timeout)
        try:
            yield
            self.check()
        finally:
            self.deadline = previous

    def call(self, action, *args):
        self.pi.sl.s.settimeout(self.remaining())
        result = action(*args)
        self.check()
        return result

    def sleep(self, seconds):
        time.sleep(min(seconds, self.remaining()))
        self.check()

    def disconnect(self):
        # pigpio.stop() normally sends on its separate notification socket without
        # a timeout. No callbacks are used here: shut that socket locally instead.
        notify = getattr(self.pi, "_notify", None)
        try:
            if notify is not None:
                notify.go = False
                self.pi._notify = None
                try:
                    notify.sl.s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    notify.sl.s.close()
        finally:
            self.pi.stop()  # Only local command-socket close remains in 1.78.

    def set_mode(self, pin, mode):
        if pin != CE:
            raise ValueError("unapproved GPIO")
        if mode == 1:  # OUTPUT: clear a retained high latch before changing mode.
            self.write(CE, 0)
        return self.call(self.pi.set_mode, pin, mode)

    def write(self, pin, value):
        if pin != CE:
            raise ValueError("unapproved GPIO")
        result = self.call(self.pi.write, pin, value if self.enabled else 0)
        if result is not None and result < 0:
            raise OSError("CE write failed")
        return result

    def spi_open(self, channel, speed):
        if channel != 0 or speed != 1000000:
            raise ValueError("unapproved SPI profile")
        self.pi.sl.s.settimeout(self.remaining())
        handle = self.pi.spi_open(channel, speed)
        if handle < 0:
            raise OSError("SPI0 CE0 open failed")
        self.handles.append(handle)
        self.check()
        return handle

    def spi_xfer(self, handle, data):
        count, received = self.call(self.pi.spi_xfer, handle, data)
        if count != len(data) or len(received) != len(data):
            raise OSError("short SPI transaction")
        return count, received


class NrfRadio:
    def __init__(self, pi, module, role, rate):
        self.pi = pi
        self.io = _PiIO(pi)
        self.driver = None
        self.closed = False
        try:
            # pigpio.write sets the latch before OUTPUT; set_mode alone does not.
            self.io.write(CE, 0)
            selected_rate = (module.RF24_DATA_RATE.RATE_250KBPS if rate == "250k"
                             else module.RF24_DATA_RATE.RATE_2MBPS)
            self.driver = module.NRF24(
                self.io, ce=CE, spi_channel=0, spi_speed=1000000,
                channel=CHANNEL, data_rate=selected_rate, pa_level=module.RF24_PA.MIN,
                payload_size=32, address_bytes=5, crc_bytes=2, pad=0)
            nrf = self.driver
            nrf.power_down()
            # Clear any stale carrier/PLL settings. CE is still physically low.
            rf_setup = 0x21 if rate == "250k" else 0x09
            for register, value in ((0x00, 0x0c), (0x06, rf_setup),
                                    (0x01, 3), (0x02, 0), (0x1c, 0), (0x1d, 0)):
                self._write_register(register, value)
            nrf.set_retransmission(5, 2)  # 1500 us, two hardware retransmissions.
            writing, reading = REQUEST_ADDRESS, REPLY_ADDRESS
            if role == "rx":
                writing, reading = reading, writing
            nrf.open_writing_pipe(writing)
            nrf.open_reading_pipe(1, reading)
            expected = {0x00: 0x0c, 0x01: 3, 0x02: 3, 0x03: 3,
                        0x04: 0x52, 0x05: CHANNEL, 0x06: rf_setup,
                        0x11: 32, 0x12: 32, 0x1c: 0, 0x1d: 0}
            for reg, value in expected.items():
                mask = 0xfe if reg == 0x06 else 0xff  # RF_SETUP bit 0 is obsolete.
                if (self._read_register(reg) & mask) != (value & mask):
                    raise OSError("radio profile readback mismatch; check wiring/module")
            if (nrf.get_writing_address() != writing
                    or nrf.get_reading_address(1) != reading):
                raise OSError("radio address readback mismatch")
            nrf.flush_rx()
            nrf.flush_tx()
            nrf.power_up_rx()
            time.sleep(0.005)  # Power-up settling before enabling CE.
            self.io.enabled = True
            self.io.write(CE, 1)
        except BaseException:
            try:
                self.close()
            except Exception:
                pass  # Preserve the initialization error after all cleanup attempts.
            raise

    def _write_register(self, register, value):
        self.io.spi_xfer(self.driver.get_spi_handle(), [0x20 | register, value])

    def _read_register(self, register):
        return self.io.spi_xfer(self.driver.get_spi_handle(), [register, 0])[1][1]

    def send(self, frame):
        if len(frame) != 32:
            raise ValueError("radio requires a fixed 32-byte PT/1 packet")
        nrf = self.driver
        try:
            with self.io.budget(SEND_TIMEOUT):
                # Peer settling, preparation, driver send and polling share a budget.
                self.io.sleep(0.005)
                nrf.flush_tx()
                nrf.send(frame)
                self.io.check()
                while True:
                    status = nrf.get_status()
                    self.io.check()
                    if status & 0x10:  # MAX_RT takes precedence over TX_DS.
                        return False
                    if status & 0x20:
                        return True
                    self.io.sleep(0.001)
        except _DeadlineExpired:
            return False
        finally:
            self._restore_rx()

    def _restore_rx(self):
        try:
            with self.io.budget(CLEANUP_TIMEOUT):
                self.driver.unset_ce()
                self.driver.flush_tx()
                self.driver.power_up_rx()
        except BaseException as error:
            # No second cleanup budget or commands on a possibly partial stream.
            self.closed = True
            self.io.enabled = False
            self.io.broken = True
            self.io.disconnect()
            if isinstance(error, Exception):
                raise OSError("radio recovery failed; inspect hardware state") from error
            raise

    def receive(self, timeout):
        if timeout <= 0:
            return None
        recovering = False
        try:
            with self.io.budget(timeout):
                while True:
                    ready = self.driver.data_ready()
                    self.io.check()
                    if ready:
                        recovering = True
                        payload = bytes(self.driver.get_payload())
                        self.io.check()
                        recovering = False
                        return payload
                    self.io.sleep(0.002)
        except _DeadlineExpired:
            return None
        except OSError:
            recovering = False
            self.close()
            raise
        finally:
            if recovering:
                self._restore_rx()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.io.enabled = False
        errors = []
        actions = [lambda: self.io.write(CE, 0)]
        if self.driver is not None:
            actions.append(self.driver.power_down)
        actions.extend(lambda handle=handle: self.io.call(self.pi.spi_close, handle)
                       for handle in self.io.handles)
        try:
            with self.io.budget(CLEANUP_TIMEOUT):
                for action in actions:
                    try:
                        self.io.check()
                        action()
                    except Exception as error:
                        errors.append(error)
        except Exception as error:
            errors.append(error)
        finally:
            self.io.broken = True
            self.io.disconnect()
        if errors:
            raise OSError("radio cleanup failed; inspect hardware state") from errors[0]

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.close()
        except Exception:
            if kind is None:
                raise


def open_radio(role, rate, hardware_checked=False):
    """Explicit opt-in and profile/version validation precede driver imports."""
    if hardware_checked is not True:
        raise ValueError("--hardware-checked is required before device access")
    if role not in ("tx", "rx") or rate not in RATE_CODES:
        raise ValueError("explicit tx/rx and rate 250k/2m required")
    for name, required in (("nrf24", "2.0.0"), ("pigpio", "1.78")):
        if metadata.version(name) != required:
            raise RuntimeError("requires {}=={}".format(name, required))
    pigpio = importlib.import_module("pigpio")
    nrf24 = importlib.import_module("nrf24")
    # Explicit numeric loopback/port bypass PIGPIO_ADDR and PIGPIO_PORT defaults.
    pi = pigpio.pi("127.0.0.1", 8888, show_errors=False)
    if not pi.connected:
        pi.stop()
        raise OSError("local pigpiod unavailable at 127.0.0.1:8888")
    return NrfRadio(pi, nrf24, role, rate)
