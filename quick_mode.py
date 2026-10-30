"""Local SD TXT CLI: legacy dry-run or explicitly gated PT/1 tx/rx."""
import argparse
import importlib
from importlib import metadata
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time

from transfer import (MAX_BYTES, TOTAL_TIMEOUT, RECEIPT_LINGER,
                      crc32, decode, encode, receive_v1, send_v1)


def read_txt(path):
    if path.suffix.lower() != ".txt":
        raise ValueError("input must be a .txt file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("input must be a regular local file")
        data = source.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file exceeds 1 MiB limit")
    return data


class CompletedFile:
    """Same-directory staging + atomic no-replace publication on a local POSIX FS."""

    def __init__(self, path):
        self.path = path
        self.stage = None
        if path.suffix.lower() != ".txt":
            raise ValueError("output must be an explicit .txt path")
        if os.path.lexists(path):
            raise FileExistsError("output already exists; choose a new path")

    def __enter__(self):
        descriptor, name = tempfile.mkstemp(prefix=".pi-txt-", suffix=".partial",
                                            dir=self.path.parent)
        self.stage = Path(name)
        os.close(descriptor)
        return self

    def save(self, content):
        with self.stage.open("wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        # link() fails if the final name appeared since preflight, including symlinks.
        # The final name is never visible with partial content.
        os.link(self.stage, self.path)
        self.stage.unlink()
        self.stage = None
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def __exit__(self, kind, value, traceback):
        if self.stage is not None:
            self.stage.unlink(missing_ok=True)


@contextmanager
def hardware_deadline():
    """Linux main-thread watchdog also bounds dependency startup and file I/O."""
    def expired(signum, frame):
        raise TimeoutError("hardware operation deadline; remote save may be unconfirmed")

    old_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, TOTAL_TIMEOUT + RECEIPT_LINGER + 15)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


@contextmanager
def exclusive_radio():
    """One process owns this radio; no simultaneous local TX and RX."""
    descriptor = os.open("/tmp/pi-astra-radio.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)



class ButtonLed:
    """Opt-in common-cathode BCM17/27/22 RGB and BCM23/GND button.

    Uses the already-required pigpio client, no gpiozero import or callbacks.
    Waiting does not open the radio. One invocation sends at most once.
    """
    PINS = (17, 27, 22)
    BUTTON = 23
    DEBOUNCE = 0.05

    def __init__(self):
        self.pi = None
        self.io = None
        self.outputs = []

    def _call(self, action, *args):
        with self.io.budget(1.0):
            result = self.io.call(action, *args)
        if isinstance(result, int) and result < 0:
            raise OSError("button/LED GPIO command failed")
        return result

    def __enter__(self):
        with hardware_deadline():
            return self._open()

    def _open(self):
        if metadata.version("pigpio") != "1.78":
            raise RuntimeError("requires pigpio==1.78")
        from radio_adapter import _PiIO
        pigpio = importlib.import_module("pigpio")
        try:
            self.pi = pigpio.pi("127.0.0.1", 8888, show_errors=False)
            if not self.pi.connected:
                raise OSError("local pigpiod unavailable at 127.0.0.1:8888")
            self.io = _PiIO(self.pi)
            for pin in self.PINS:
                # Common GND: latch low BEFORE output, including retained state.
                self._call(self.pi.write, pin, 0)
                self.outputs.append(pin)
                self._call(self.pi.set_mode, pin, pigpio.OUTPUT)
            self._call(self.pi.set_mode, self.BUTTON, pigpio.INPUT)
            self._call(self.pi.set_pull_up_down, self.BUTTON, pigpio.PUD_UP)
            return self
        except BaseException:
            self.close()
            raise

    def color(self, value):
        # Switch all channels off first to avoid intermediate mixed colors.
        for pin in self.outputs:
            self._call(self.pi.write, pin, 0)
        for pin, active in zip(self.PINS, value):
            if active:
                self._call(self.pi.write, pin, 1)

    def _stable(self, level):
        since = None
        while True:
            value = self._call(self.pi.read, self.BUTTON)
            if value not in (0, 1):
                raise OSError("invalid button GPIO level")
            now = time.monotonic()
            if value == level:
                if since is None:
                    since = now
                elif now - since >= self.DEBOUNCE:
                    return
            else:
                since = None
            time.sleep(0.01)

    def wait(self):
        print("Release button, then press and release to send once; Ctrl-C cancels.",
              file=sys.stderr, flush=True)
        # A button held at startup must be released before arming a new press.
        for level in (1, 0, 1):
            self._stable(level)

    def result(self, saved):
        try:
            self.color((0, 1, 0) if saved else (1, 0, 0))
            time.sleep(2)
        except Exception as error:
            print("LED indication unavailable: {}".format(error), file=sys.stderr)

    def close(self):
        if self.pi is None:
            return
        try:
            if self.io is not None and self.pi.connected:
                if self.io.broken:
                    print("GPIO connection failed; LED off cannot be verified.",
                          file=sys.stderr)
                else:
                    with self.io.budget(0.2):
                        for pin in self.outputs:
                            self.io.call(self.pi.write, pin, 0)
        except Exception as error:
            print("LED off not verified: {}".format(error), file=sys.stderr)
        finally:
            if self.io is not None:
                self.io.disconnect()
            else:
                self.pi.stop()
            self.pi = None

    def __exit__(self, kind, value, traceback):
        self.close()


def radio_main(argv):
    parser = argparse.ArgumentParser(description="PT/1 single-file radio demo")
    parser.add_argument("role", choices=("tx", "rx"))
    parser.add_argument("file", type=Path, help="explicit SD input (TX) or new output (RX)")
    parser.add_argument("--rate", choices=("250k", "2m"), required=True,
                        help="both peers must match; no inferred MRM/NM/SRI mode")
    parser.add_argument("--hardware-checked", action="store_true", required=True,
                        help="attest reviewed module, power, wiring and antenna checks")
    parser.add_argument("--button-led", action="store_true",
                        help="TX only: common-GND RGB, press/release once to send")
    args = parser.parse_args(argv)
    if args.button_led and args.role != "tx":
        parser.error("--button-led is TX only; RX still starts immediately")
    try:
        # Validate paths/data before any driver import or GPIO/device access.
        content = read_txt(args.file) if args.role == "tx" else None
        output = CompletedFile(args.file) if args.role == "rx" else None
        from radio_adapter import open_radio

        with exclusive_radio(), (ButtonLed() if args.button_led else nullcontext()) as ui:
            if ui is not None:
                ui.wait()
            try:
                with hardware_deadline():
                    report = perform_transfer(args, content, output, open_radio, ui)
            except BaseException:
                if ui is not None:
                    ui.result(False)
                raise
            if ui is not None:
                ui.result(True)
        report.update({"channel": 74, "rate": args.rate, "pa": "MIN"})
        print(json.dumps(report))
        return 0
    except KeyboardInterrupt:
        print("Interrupted; transfer not confirmed. Inspect any completed RX file.",
              file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print("Failed: {}. No transfer success claimed.".format(error), file=sys.stderr)
        return 1


def perform_transfer(args, content, output, open_radio, ui):
    if ui is not None:
        ui.color((0, 0, 1))
    if args.role == "tx":
        session = int.from_bytes(os.urandom(8), "big") or 1
        with open_radio(args.role, args.rate, args.hardware_checked) as radio:
            frames = send_v1(content, session, args.rate, radio, time.monotonic)
        report = {"mode": "tx", "protocol": "PT/1", "bytes": len(content),
                  "frames": frames, "remote_saved_receipt": True,
                  "crc32": "{:08x}".format(crc32(content))}
    else:
        with output:
            with open_radio(args.role, args.rate, args.hardware_checked) as radio:
                count = receive_v1(args.rate, radio, output.save, time.monotonic)
        report = {"mode": "rx", "protocol": "PT/1", "bytes": count,
                  "local_file_saved": True, "output": str(args.file)}
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("tx", "rx"):
        return radio_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="explicit local .txt input")
    parser.add_argument("--dry-run", action="store_true", required=True,
                        help="legacy host framing only; use tx/rx for PT/1")
    args = parser.parse_args(argv)
    if args.file.suffix.lower() != ".txt":
        parser.error("input must be a .txt file")
    try:
        data = read_txt(args.file)
        frames = encode(data)
        if decode(frames) != data:
            raise ValueError("local reconstruction mismatch")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Failed: {error}\n")
    print(json.dumps({"mode": "host-dry-run", "bytes": len(data),
                      "data_frames": len(frames) - 1,
                      "radio_transmitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
