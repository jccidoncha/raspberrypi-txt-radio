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
                      crc32, decode, encode, receive_v1, send_v1,
                      unpack_packet, expected_reply, START, DATA, FINISH)


def console(message):
    """Milestones go to stderr; stdout remains the final machine-readable JSON."""
    print("[{:.3f}] {}".format(time.monotonic(), message), file=sys.stderr, flush=True)


class LoggedTxRadio:
    """Observe actual TX requests/replies without changing PT/1 or claiming RF proof."""
    def __init__(self, radio, total, debug=False):
        self.radio, self.total, self.debug = radio, total, debug
        self.pending = None
        self.attempts = 0
        self.accepted_bytes = 0
        self.next_progress = 0

    def send(self, frame):
        kind, session, sequence, body = unpack_packet(frame)
        if self.pending == frame:
            self.attempts += 1
        else:
            self.pending, self.attempts = frame, 1
        if self.debug or self.attempts > 1 or kind in (START, FINISH):
            console("TX {} seq={} attempt={} (link ACK is not file saved)".format(
                {START: "START", DATA: "DATA", FINISH: "FINISH"}[kind],
                sequence, self.attempts))
        ack = self.radio.send(frame)
        if self.debug:
            console("TX link ACK observed={}".format(bool(ack)))
        return ack

    def receive(self, timeout):
        frame = self.radio.receive(timeout)
        if frame is None:
            if self.debug:
                console("TX reply wait expired; no matching receipt yet")
        elif self.pending is not None and frame == expected_reply(self.pending):
            kind, session, sequence, body = unpack_packet(self.pending)
            if kind == DATA:
                self.accepted_bytes = min(self.total, sequence * 16)
                percent = (100 * self.accepted_bytes // self.total) if self.total else 100
                if self.debug or percent >= self.next_progress:
                    console("TX peer accepted data {}/{} bytes ({}%); NOT saved yet".format(
                        self.accepted_bytes, self.total, percent))
                    self.next_progress = (percent // 10 + 1) * 10
            elif kind == FINISH:
                console("TX matching remote SAVED receipt observed; awaiting normal cleanup")
            elif self.debug:
                console("TX matching START ACK observed")
        elif self.debug:
            console("TX unrelated reply ignored by protocol")
        return frame


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
            console("Button/RGB ready: common GND; LED off; local pigpiod")
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
        console("LED command completed: {} (physical light not sensed)".format(
            {(0, 0, 0): "off", (0, 0, 1): "blue / attempting TX",
             (0, 1, 0): "green / saved receipt confirmed",
             (1, 0, 0): "red / attempt unconfirmed"}.get(value, "custom")))

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
        for level, message in ((1, "Button armed: released"),
                               (0, "Button press stable; waiting for release"),
                               (1, "Button release stable; starting one TX")):
            self._stable(level)
            console(message)

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
    parser.add_argument("--debug", action="store_true",
                        help="TX: log every request/link ACK/reply; never file contents")
    args = parser.parse_args(argv)
    if args.button_led and args.role != "tx":
        parser.error("--button-led is TX only; RX still starts immediately")
    try:
        # Validate paths/data before any driver import or GPIO/device access.
        content = read_txt(args.file) if args.role == "tx" else None
        output = CompletedFile(args.file) if args.role == "rx" else None
        if args.role == "tx":
            console("TXT loaded: {} ({} bytes; snapshot held in memory)".format(
                args.file, len(content)))
        else:
            console("RX destination checked: {} (must not exist)".format(args.file))
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
        console("{} complete: {}".format(args.role.upper(),
                "remote SAVED confirmed" if args.role == "tx" else "local file saved"))
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
    console("Opening {} radio: channel74 rate={} PA=MIN".format(args.role.upper(), args.rate))
    if args.role == "tx":
        session = int.from_bytes(os.urandom(8), "big") or 1
        with open_radio(args.role, args.rate, args.hardware_checked) as radio:
            console("TX radio ready; beginning START/data/FINISH ({} data blocks)".format(
                (len(content) + 15) // 16))
            observed = LoggedTxRadio(radio, len(content), args.debug)
            frames = send_v1(content, session, args.rate, observed, time.monotonic)
        report = {"mode": "tx", "protocol": "PT/1", "bytes": len(content),
                  "frames": frames, "remote_saved_receipt": True,
                  "crc32": "{:08x}".format(crc32(content))}
    else:
        with output:
            with open_radio(args.role, args.rate, args.hardware_checked) as radio:
                console("RX radio ready; waiting for START (30s); RX also transmits replies")
                def save_and_log(data):
                    output.save(data)
                    console("RX TXT saved atomically: {} bytes; sending SAVED reply".format(len(data)))
                count = receive_v1(args.rate, radio, save_and_log, time.monotonic)
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
