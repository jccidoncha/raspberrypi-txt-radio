"""Local SD TXT CLI: legacy dry-run or explicitly gated PT/1 tx/rx (single file or directory)."""
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
import zlib

import transfer
import radio_adapter

def fast_crc32(content: bytes) -> int:
    return zlib.crc32(content) & 0xffffffff

transfer.crc32 = fast_crc32

transfer.START_TIMEOUT = 180.0
transfer.IDLE_TIMEOUT = 60.0
transfer.TOTAL_TIMEOUT = 3600.0
transfer.ATTEMPTS = 25
transfer.REPLY_TIMEOUT = 0.40
transfer.RECEIPT_LINGER = 0.40

def _safe_socket_call(self, name, *args):
    try:
        self.raw.settimeout(radio_adapter.IO_TIMEOUT)
        return getattr(self.raw, name)(*args)
    except OSError:
        self.io.broken = True
        raise

radio_adapter._DeadlineSocket._call = _safe_socket_call
radio_adapter.SEND_TIMEOUT = 0.35
radio_adapter.CLEANUP_TIMEOUT = 0.50

def _reliable_send(self, frame):
    if len(frame) != 32:
        raise ValueError("radio requires a fixed 32-byte PT/1 packet")
    nrf = self.driver
    try:
        with self.io.budget(radio_adapter.SEND_TIMEOUT):
            self.io.sleep(0.003)
            nrf.flush_tx()
            nrf.send(frame)
            self.io.check()
            while True:
                status = nrf.get_status()
                self.io.check()
                if status & 0x10:
                    return False
                if status & 0x20:
                    return True
                self.io.sleep(0.0005)
    except radio_adapter._DeadlineExpired:
        return False
    finally:
        self._restore_rx()

def _reliable_receive(self, timeout):
    if timeout <= 0:
        return None
    recovering = False
    try:
        with self.io.budget(timeout):
            while True:
                ready = self.driver.data_ready()
                if ready:
                    recovering = True
                    payload = bytes(self.driver.get_payload())
                    recovering = False
                    return payload
                self.io.check()
                self.io.sleep(0.001)
    except radio_adapter._DeadlineExpired:
        return None
    except OSError:
        recovering = False
        self.close()
        raise
    finally:
        if recovering:
            self._restore_rx()

radio_adapter.NrfRadio.send = _reliable_send
radio_adapter.NrfRadio.receive = _reliable_receive

from transfer import (MAX_BYTES, RECEIPT_LINGER,
                     crc32, decode, encode, receive_v1, send_v1,
                     unpack_packet, expected_reply, START, DATA, FINISH)

BATCH_MAGIC = b"PT1DIR\x00\n"
COMPRESS_MAGIC = b"PT1Z\x00\n"
EXTENDED_WALL_TIMEOUT = int(transfer.TOTAL_TIMEOUT + RECEIPT_LINGER + 120)


def console(message):
    print("[{:.3f}] {}".format(time.monotonic(), message), file=sys.stderr, flush=True)


def maybe_compress(data: bytes, enabled: bool) -> bytes:
    if not enabled or len(data) < 256 or data.startswith(COMPRESS_MAGIC):
        return data
    compressed = COMPRESS_MAGIC + zlib.compress(data, level=9)
    if len(compressed) < len(data):
        console("Lossless air compression active: {} bytes -> {} bytes ({}% smaller)".format(
            len(data), len(compressed), 100 - (100 * len(compressed) // len(data))))
        return compressed
    return data


def maybe_decompress(data: bytes) -> bytes:
    if data.startswith(COMPRESS_MAGIC):
        raw = zlib.decompress(data[len(COMPRESS_MAGIC):])
        console("Lossless air decompression complete: {} air bytes -> {} original TXT bytes".format(
            len(data), len(raw)))
        return raw
    return data


class LoggedTxRadio:
    def __init__(self, radio, total, debug=False, ui=None):
        self.radio, self.total, self.debug, self.ui = radio, total, debug, ui
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
        if self.ui is not None and ack:
            self.ui.pulse_activity()
        if self.debug:
            console("TX link ACK observed={}".format(bool(ack)))
        return ack

    def receive(self, timeout):
        frame = self.radio.receive(timeout)
        if frame is None:
            if self.debug:
                console("TX reply wait expired; no matching receipt yet")
        elif self.pending is not None and frame == expected_reply(self.pending):
            if self.ui is not None:
                self.ui.pulse_activity()
            kind, session, sequence, body = unpack_packet(self.pending)
            if kind == DATA:
                self.accepted_bytes = min(self.total, sequence * 16)
                percent = (100 * self.accepted_bytes // self.total) if self.total else 100
                if self.debug or percent >= self.next_progress:
                    console("TX peer accepted data {}/{} bytes ({}%); NOT saved yet".format(
                        self.accepted_bytes, self.total, percent))
                    self.next_progress = (percent // 5 + 1) * 5
            elif kind == FINISH:
                console("TX matching remote SAVED receipt observed; awaiting normal cleanup")
            elif self.debug:
                console("TX matching START ACK observed")
        elif self.debug:
            console("TX unrelated reply ignored by protocol")
        return frame


class LoggedRxRadio:
    def __init__(self, radio, debug=False, ui=None):
        self.radio = radio
        self.debug = debug
        self.ui = ui
        self.last_logged_pct = -1
        self.total_bytes = 0

    def send(self, frame):
        ack = self.radio.send(frame)
        if self.ui is not None:
            self.ui.pulse_activity()
        return ack

    def receive(self, timeout):
        frame = self.radio.receive(timeout)
        if frame is not None:
            if self.ui is not None:
                self.ui.pulse_activity()
            try:
                kind, session, sequence, body = unpack_packet(frame)
                if kind == START:
                    import struct
                    self.total_bytes = struct.unpack("!I", body[:4])[0]
                    console("RX START received! Incoming air payload: {} bytes".format(self.total_bytes))
                    self.last_logged_pct = -1
                elif kind == DATA and self.total_bytes > 0:
                    rx_bytes = min(self.total_bytes, sequence * 16)
                    pct = (100 * rx_bytes) // self.total_bytes
                    if self.debug or sequence == 1 or pct >= self.last_logged_pct + 5:
                        console("RX receiving DATA seq={} ({}/{} bytes, {}%)".format(
                            sequence, rx_bytes, self.total_bytes, pct))
                        self.last_logged_pct = (pct // 5) * 5
                elif kind == FINISH:
                    console("RX FINISH packet received! Verifying CRC32 and saving...")
            except Exception:
                pass
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


def load_tx_payload(path, batch=True, compress=True):
    """Load a single .txt file or automatically pack all .txt files in a directory."""
    if path.is_dir():
        txt_files = sorted(
            p for p in path.iterdir()
            if p.is_file() and p.suffix.lower() == ".txt" and not p.name.startswith(".")
        )
        if not txt_files:
            raise ValueError(f"no .txt files found in directory {path}")
        if len(txt_files) == 1 and not batch:
            chosen = txt_files[0]
            console(f"Auto-selected TXT from directory: {chosen.name}")
            raw = read_txt(chosen)
            return maybe_compress(raw, compress), [chosen.name], len(raw)

        parts = [BATCH_MAGIC, len(txt_files).to_bytes(4, "big")]
        names = []
        for f in txt_files:
            raw_name = f.name.encode("utf-8")
            if len(raw_name) > 255:
                raise ValueError(f"filename too long: {f.name}")
            file_bytes = read_txt(f)
            parts.append(len(raw_name).to_bytes(2, "big"))
            parts.append(raw_name)
            parts.append(len(file_bytes).to_bytes(4, "big"))
            parts.append(file_bytes)
            names.append(f.name)
        raw_payload = b"".join(parts)
        payload = maybe_compress(raw_payload, compress)
        if len(payload) > MAX_BYTES:
            raise ValueError("combined directory payload exceeds 1 MiB limit")
        return payload, names, len(raw_payload)
    else:
        raw = read_txt(path)
        return maybe_compress(raw, compress), [path.name], len(raw)


def unpack_batch_payload(data):
    if not data.startswith(BATCH_MAGIC):
        return None
    idx = len(BATCH_MAGIC)
    if len(data) < idx + 4:
        raise ValueError("corrupted batch header")
    count = int.from_bytes(data[idx:idx + 4], "big")
    idx += 4
    items = []
    for _ in range(count):
        if len(data) < idx + 2:
            raise ValueError("corrupted batch filename length")
        name_len = int.from_bytes(data[idx:idx + 2], "big")
        idx += 2
        name = data[idx:idx + name_len].decode("utf-8")
        idx += name_len
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError(f"unsafe filename in batch: {name}")
        if len(data) < idx + 4:
            raise ValueError("corrupted batch file size")
        size = int.from_bytes(data[idx:idx + 4], "big")
        idx += 4
        if len(data) < idx + size:
            raise ValueError("truncated batch file content")
        content = data[idx:idx + size]
        idx += size
        items.append((name, content))
    return items


class CompletedFile:
    def __init__(self, path):
        self.path = path
        self.stage = None
        self.saved_bytes = 0
        self.is_dir_target = path.exists() and path.is_dir() or path.suffix == ""
        if not self.is_dir_target:
            if path.suffix.lower() != ".txt":
                raise ValueError("output must be an explicit .txt path or a directory")
            if os.path.lexists(path):
                raise FileExistsError("output already exists; choose a new path")

    def __enter__(self):
        if self.is_dir_target:
            self.path.mkdir(parents=True, exist_ok=True)
            return self
        descriptor, name = tempfile.mkstemp(prefix=".pi-txt-", suffix=".partial",
                                            dir=self.path.parent)
        self.stage = Path(name)
        os.close(descriptor)
        return self

    def _atomic_write_single(self, target_path, content):
        if os.path.lexists(target_path):
            target_path.unlink()
        descriptor, name = tempfile.mkstemp(prefix=".pi-txt-", suffix=".partial",
                                            dir=target_path.parent)
        stage = Path(name)
        os.close(descriptor)
        try:
            with stage.open("wb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            os.link(stage, target_path)
            stage.unlink()
            directory = os.open(target_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            stage.unlink(missing_ok=True)

    def save(self, content):
        content = maybe_decompress(content)
        self.saved_bytes = len(content)
        batch_items = unpack_batch_payload(content)
        if batch_items is not None:
            out_dir = self.path if self.is_dir_target else self.path.parent
            saved_paths = []
            for name, file_bytes in batch_items:
                dest = out_dir / name
                self._atomic_write_single(dest, file_bytes)
                saved_paths.append(str(dest))
            console(f"RX batch unpacked: {len(saved_paths)} files saved in {out_dir}")
            return
        if self.is_dir_target:
            dest = self.path / "ricevuto.txt"
            self._atomic_write_single(dest, content)
            self.path = dest
            return
        with self.stage.open("wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
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
def hardware_deadline(seconds=EXTENDED_WALL_TIMEOUT):
    def expired(signum, frame):
        raise TimeoutError("hardware operation deadline; remote save may be unconfirmed")

    old_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


@contextmanager
def exclusive_radio():
    descriptor = os.open("/tmp/pi-astra-radio.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


class ButtonLed:
    PINS = (17, 27, 22)
    BUTTON = 23
    DEBOUNCE = 0.02
    BLINK_INTERVAL = 0.25
    ACTIVITY_MIN_INTERVAL = 0.04

    def __init__(self, role="tx"):
        self.role = role.upper()
        self.pi = None
        self.io = None
        self.outputs = []
        self._last_activity_toggle = 0.0
        self._blue_state = False

    def _call(self, action, *args):
        with self.io.budget(1.0):
            result = self.io.call(action, *args)
        if isinstance(result, int) and result < 0:
            raise OSError("button/LED GPIO command failed")
        return result

    def __enter__(self):
        with hardware_deadline(15):
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

    def _set_raw_rgb(self, value):
        for pin, active in zip(self.PINS, value):
            self._call(self.pi.write, pin, 1 if active else 0)

    def color(self, value, log=True):
        self._set_raw_rgb((0, 0, 0))
        self._set_raw_rgb(value)
        self._blue_state = (value == (0, 0, 1))
        if log:
            console("LED state: {}".format(
                {(0, 0, 0): "off",
                 (1, 1, 1): "white / waiting for button press ({})".format(self.role),
                 (0, 0, 1): "blue / PC-activity mode ({})".format(self.role),
                 (0, 1, 0): "green / {} completed and saved".format(self.role),
                 (1, 0, 0): "red / {} error or unconfirmed".format(self.role)}.get(value, "custom")))

    def pulse_activity(self):
        now = time.monotonic()
        if now - self._last_activity_toggle >= self.ACTIVITY_MIN_INTERVAL:
            self._blue_state = not self._blue_state
            try:
                self._call(self.pi.write, 22, 1 if self._blue_state else 0)
            except Exception:
                pass
            self._last_activity_toggle = now

    def _wait_button_cycle(self, blink_rgb=None):
        last_toggle = time.monotonic()
        led_on = True
        if blink_rgb is not None:
            self._set_raw_rgb(blink_rgb)

        for target_level, message in ((1, "Button armed: released"),
                                     (0, "Button press stable; waiting for release"),
                                     (1, "Button release stable")):
            since = None
            while True:
                now = time.monotonic()
                if blink_rgb is not None:
                    if target_level == 1 and message == "Button release stable":
                        if not led_on:
                            self._set_raw_rgb(blink_rgb)
                            led_on = True
                    elif now - last_toggle >= self.BLINK_INTERVAL:
                        led_on = not led_on
                        self._set_raw_rgb(blink_rgb if led_on else (0, 0, 0))
                        last_toggle = now

                value = self._call(self.pi.read, self.BUTTON)
                if value not in (0, 1):
                    raise OSError("invalid button GPIO level")
                if value == target_level:
                    if since is None:
                        since = now
                    elif now - since >= self.DEBOUNCE:
                        break
                else:
                    since = None
                time.sleep(0.005)
            console(message)

    def wait(self):
        console("LED state: blinking white / waiting for button press to start {}".format(self.role))
        print("Blinking WHITE: press and release button to start {}; Ctrl-C cancels.".format(self.role),
              file=sys.stderr, flush=True)
        self._wait_button_cycle(blink_rgb=(1, 1, 1))

    def result(self, saved):
        try:
            if saved:
                console("LED state: blinking green / transfer complete! Press button to exit.")
                print("Blinking GREEN: transfer complete! Press and release button to finish.",
                      file=sys.stderr, flush=True)
                self._wait_button_cycle(blink_rgb=(0, 1, 0))
                self.color((0, 0, 0), log=False)
            else:
                self.color((1, 0, 0), log=True)
                time.sleep(3)
        except Exception as error:
            print("LED indication unavailable: {}".format(error), file=sys.stderr)

    def close(self):
        if self.pi is None:
            return
        try:
            if self.io is not None and self.pi.connected:
                if not self.io.broken:
                    with self.io.budget(0.2):
                        for pin in self.outputs:
                            self.io.call(self.pi.write, pin, 0)
        except Exception:
            pass
        finally:
            if self.io is not None:
                self.io.disconnect()
            else:
                self.pi.stop()
            self.pi = None

    def __exit__(self, kind, value, traceback):
        self.close()


def radio_main(argv):
    parser = argparse.ArgumentParser(description="PT/1 single-file or directory radio demo")
    parser.add_argument("role", choices=("tx", "rx"))
    parser.add_argument("file", type=Path, help="SD input file/dir (TX) or output file/dir (RX)")
    parser.add_argument("--rate", choices=("250k", "2m"), required=True)
    parser.add_argument("--hardware-checked", action="store_true", required=True)
    parser.add_argument("--batch", action="store_true", default=True)
    parser.add_argument("--compress", dest="compress", action="store_true", default=True)
    parser.add_argument("--no-compress", dest="compress", action="store_false")
    parser.add_argument("--button-led", dest="button_led", action="store_true", default=True)
    parser.add_argument("--no-button-led", dest="button_led", action="store_false")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.role == "tx":
            content, names, raw_len = load_tx_payload(args.file, args.batch, args.compress)
            output = None
            console("TXT loaded: {} ({} original bytes, {} air bytes; files={})".format(
                args.file, raw_len, len(content), ", ".join(names)))
        else:
            content = None
            output = CompletedFile(args.file)
            console("RX destination checked: {}".format(args.file))
        from radio_adapter import open_radio

        with exclusive_radio(), (ButtonLed(args.role) if args.button_led else nullcontext()) as ui:
            if ui is not None:
                ui.wait()
            try:
                with hardware_deadline(EXTENDED_WALL_TIMEOUT):
                    report = perform_transfer(args, content, output, open_radio, ui)
            except BaseException:
                if ui is not None:
                    ui.result(False)
                raise
            report.update({"channel": 74, "rate": args.rate, "pa": "MIN"})
            console("{} complete: {}".format(args.role.upper(),
                    "remote SAVED confirmed" if args.role == "tx" else "local file saved"))
            if ui is not None:
                ui.result(True)
        print(json.dumps(report))
        return 0
    except KeyboardInterrupt:
        print("Interrupted; transfer not confirmed.", file=sys.stderr)
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
            observed = LoggedTxRadio(radio, len(content), args.debug, ui=ui)
            frames = send_v1(content, session, args.rate, observed, time.monotonic)
        report = {"mode": "tx", "protocol": "PT/1", "bytes": len(content),
                  "frames": frames, "remote_saved_receipt": True,
                  "crc32": "{:08x}".format(crc32(content))}
    else:
        with output:
            with open_radio(args.role, args.rate, args.hardware_checked) as radio:
                console("RX radio ready; waiting for START (180s); RX also transmits replies")
                observed_rx = LoggedRxRadio(radio, args.debug, ui=ui)
                def save_and_log(data):
                    output.save(data)
                    console("RX TXT saved atomically: {} bytes; sending SAVED reply".format(output.saved_bytes))
                count = receive_v1(args.rate, observed_rx, save_and_log, time.monotonic)
        report = {"mode": "rx", "protocol": "PT/1", "bytes": output.saved_bytes or count,
                  "local_file_saved": True, "output": str(output.path)}
    return report


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("tx", "rx"):
        return radio_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="explicit local .txt input")
    parser.add_argument("--dry-run", action="store_true", required=True)
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