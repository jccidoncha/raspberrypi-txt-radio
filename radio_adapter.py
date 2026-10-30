"""Opt-in nrf24 2.0.0 / pigpio 1.78 adapter. Importing opens no devices.

Only the intended radio pins are used. LED and button access is absent.
Private register access is isolated here because two release helpers are faulty;
see reference.md. No source from either unlicensed reference is incorporated.
"""
import importlib
from importlib import metadata
import time

from transfer import RATE_CODES

CE = 25
CHANNEL = 74
REQUEST_ADDRESS = b"PTX01"
REPLY_ADDRESS = b"PRX01"
SEND_TIMEOUT = 0.1


class _PiIO:
    """Hold CE low until configuration passes; track partial-init SPI handles."""

    def __init__(self, pi):
        self.pi = pi
        self.enabled = False
        self.handles = []

    def set_mode(self, pin, mode):
        if pin != CE:
            raise ValueError("unapproved GPIO")
        return self.pi.set_mode(pin, mode)

    def write(self, pin, value):
        if pin != CE:
            raise ValueError("unapproved GPIO")
        return self.pi.write(pin, value if self.enabled else 0)

    def spi_open(self, channel, speed):
        if channel != 0 or speed != 1000000:
            raise ValueError("unapproved SPI profile")
        handle = self.pi.spi_open(channel, speed)
        if handle < 0:
            raise OSError("SPI0 CE0 open failed")
        self.handles.append(handle)
        return handle

    def spi_xfer(self, handle, data):
        count, received = self.pi.spi_xfer(handle, data)
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
            if any(self._read_register(reg) != value for reg, value in expected.items()):
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
        # Give the peer time to return to PRX before application replies.
        time.sleep(0.005)
        nrf = self.driver
        try:
            nrf.flush_tx()
            nrf.send(frame)
            deadline = time.monotonic() + SEND_TIMEOUT
            while time.monotonic() < deadline:
                status = nrf.get_status()
                if status & 0x10:  # MAX_RT takes precedence over TX_DS.
                    return False
                if status & 0x20:
                    return True
                time.sleep(0.001)
            return False
        finally:
            nrf.unset_ce()
            nrf.flush_tx()
            nrf.power_up_rx()

    def receive(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            if self.driver.data_ready():
                return bytes(self.driver.get_payload())
            time.sleep(min(0.002, max(0, deadline - time.monotonic())))
        return None

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.io.enabled = False
        errors = []
        actions = [lambda: self.pi.write(CE, 0)]
        if self.driver is not None:
            actions.append(self.driver.power_down)
        actions.extend(lambda handle=handle: self.pi.spi_close(handle)
                       for handle in self.io.handles)
        actions.append(self.pi.stop)
        for action in actions:
            try:
                action()
            except Exception as error:
                errors.append(error)
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
    try:
        pi.sl.s.settimeout(1.0)  # Bound command I/O if the daemon stops responding.
    except BaseException:
        pi.stop()
        raise
    return NrfRadio(pi, nrf24, role, rate)
