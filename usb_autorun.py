"""Unattended USB flow around quick_mode.py: USB TXT -> SD -> radio -> SD -> USB.

Runs as a service on each Pi. The USB stick decides the role:
  * exactly one root-level .txt on the stick -> TX that file;
  * no root-level .txt -> RX, saved on the SD first, then copied to <stick>/received/.
The radio code is not touched: this wrapper only stages files and starts
quick_mode.py. Radio saves go to the SD because os.link (atomic no-overwrite save)
does not work on FAT32/exFAT. Standard library only; no hardware access at import.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent
MOUNT = Path(os.environ.get("PT_MOUNT", "/mnt/pt-usb"))
STATE = Path(os.environ.get("PT_STATE", "/var/lib/pt-radio"))
MAX_BYTES = 1024 * 1024
ALLOWED_FS = ("vfat", "exfat", "ext2", "ext3", "ext4")
RECEIVED_DIR = "received"
LED_HOLD = 10.0


def log(message):
    print("[usb-autorun] {}".format(message), file=sys.stderr, flush=True)


class FlowError(Exception):
    pass


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


# ---- USB device discovery -------------------------------------------------

def usb_candidates(lsblk):
    """Mountable filesystems on USB disks from `lsblk -J`; SD (mmcblk) is never listed."""
    found = []
    for disk in lsblk.get("blockdevices", []):
        if disk.get("type") != "disk" or disk.get("tran") != "usb":
            continue
        parts = [c for c in disk.get("children") or [] if c.get("type") == "part"]
        for node in (parts or [disk]):
            if node.get("fstype"):
                found.append({"path": node["path"], "fstype": node["fstype"],
                              "mountpoint": node.get("mountpoint"),
                              "label": node.get("label")})
    return found


def find_usb():
    out = run("lsblk", "-J", "--tree", "-o", "PATH,TYPE,TRAN,FSTYPE,MOUNTPOINT,LABEL").stdout
    found = usb_candidates(json.loads(out))
    if not found:
        return None
    if len(found) > 1:
        raise FlowError("more than one USB filesystem present; use one stick with one partition")
    if found[0]["fstype"] not in ALLOWED_FS:
        raise FlowError("unsupported USB filesystem: {}".format(found[0]["fstype"]))
    return found[0]


def mount_usb(device, writable=False):
    if device["mountpoint"] not in (None, "", str(MOUNT)):
        raise FlowError("USB already mounted at {}; unmount it first".format(device["mountpoint"]))
    MOUNT.mkdir(parents=True, exist_ok=True)
    if device["mountpoint"] != str(MOUNT):
        run("mount", "-t", device["fstype"], "-o", "ro,nosuid,nodev,noexec",
            device["path"], str(MOUNT))
    if writable:
        remount_rw()


def remount_rw():
    run("mount", "-o", "remount,rw,sync", str(MOUNT))


def unmount_usb():
    run("sync", check=False)
    result = run("umount", str(MOUNT), check=False)
    if result.returncode != 0:
        raise FlowError("umount failed: {}".format(result.stderr.strip()))


# ---- file helpers ----------------------------------------------------------

def root_txt_files(directory):
    return sorted(p for p in Path(directory).iterdir()
                  if p.is_file() and p.suffix.lower() == ".txt"
                  and not p.name.startswith("."))


def same_bytes(a, b):
    a, b = Path(a), Path(b)
    return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def fsync_dir(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_new_verified(source, destination):
    """Copy to a hidden temp name, fsync, compare, then rename. Never overwrites."""
    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(str(destination))
    temp = destination.with_name(".pt-" + destination.name + ".partial")
    try:
        with source.open("rb") as src, temp.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        if not same_bytes(source, temp):
            raise FlowError("copy verification failed (bytes differ)")
        if os.path.lexists(destination):
            raise FileExistsError(str(destination))
        os.rename(temp, destination)
        fsync_dir(destination.parent)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)
    if not same_bytes(source, destination):
        raise FlowError("final verification failed (bytes differ)")


def stage_tx(usb_file, stage_dir):
    """Copy the stick's TXT to the SD, verified. The stage dir is cleared first."""
    stage_dir = Path(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)
    for old in stage_dir.iterdir():
        if old.is_file():
            old.unlink()
    if usb_file.stat().st_size > MAX_BYTES:
        raise FlowError("TXT exceeds 1 MiB limit")
    target = stage_dir / "tx.txt"
    copy_new_verified(usb_file, target)
    return target


def next_rx_path(rx_dir):
    rx_dir = Path(rx_dir)
    rx_dir.mkdir(parents=True, exist_ok=True)
    number = 1
    while any((rx_dir / "rx_{:04d}{}".format(number, ext)).exists()
              for ext in (".txt", ".delivered")):
        number += 1
    return rx_dir / "rx_{:04d}.txt".format(number)


def save_to_usb(received, usb_root):
    """Copy a received file into <usb>/received/. Identical content already there
    counts as done; a different file never gets overwritten. Returns the USB path."""
    out = Path(usb_root) / RECEIVED_DIR
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise FlowError("'{}' on the stick is a symlink or not a directory".format(RECEIVED_DIR))
    out.mkdir(exist_ok=True)
    for existing in sorted(out.glob("*.txt")):
        if (not existing.name.startswith(".") and existing.is_file()
                and not existing.is_symlink() and same_bytes(received, existing)):
            return existing
    number = 1
    while os.path.lexists(out / "rx_{:04d}.txt".format(number)):
        number += 1
    target = out / "rx_{:04d}.txt".format(number)
    copy_new_verified(received, target)
    return target


# ---- radio + LED -----------------------------------------------------------

def config():
    rate = os.environ.get("PT_RATE", "")
    if rate not in ("250k", "2m"):
        raise FlowError("PT_RATE must be 250k or 2m (set in the env file)")
    if os.environ.get("PT_HARDWARE_CHECKED") != "yes":
        raise FlowError("PT_HARDWARE_CHECKED=yes is the owner's attestation; not set")
    extra = os.environ.get("PT_EXTRA_ARGS", "--batch").split()
    return rate, extra


def radio_command(role, path, rate, extra):
    python = os.environ.get("PT_PYTHON", sys.executable)
    return [python, str(REPO / "quick_mode.py"), role, str(path),
            "--rate", rate, "--hardware-checked", "--auto-finish", *extra]


def run_radio(role, path, rate, extra):
    command = radio_command(role, path, rate, extra)
    log("starting " + " ".join(command[2:]))
    return subprocess.run(command, cwd=REPO).returncode


def show(color, seconds=LED_HOLD):
    """Best-effort final LED state through the existing ButtonLed. Never raises."""
    try:
        import quick_mode
        with quick_mode.ButtonLed("usb", interactive_led=False) as led:
            led.color(color)
            time.sleep(seconds)
    except BaseException as error:
        log("LED unavailable: {}".format(error))


GREEN, RED = (0, 1, 0), (1, 0, 0)


# ---- one cycle -------------------------------------------------------------

def pending_files(rx_dir):
    """Received on the SD but not yet verified on a stick."""
    rx_dir = Path(rx_dir)
    return sorted(rx_dir.glob("rx_*.txt")) if rx_dir.is_dir() else []


def deliver_pending(device, rx_dir):
    """Copy every pending SD file to the stick. The stick must be an RX stick
    (no root .txt): a TX stick or a swapped-in TX stick never receives files."""
    mount_usb(device)
    if root_txt_files(MOUNT):
        unmount_usb()
        raise FlowError("stick has a root .txt; not delivering received files to a TX stick")
    remount_rw()
    for received in pending_files(rx_dir):
        target = save_to_usb(received, MOUNT)
        log("verified on USB: {}/{}".format(RECEIVED_DIR, target.name))
        received.rename(received.with_suffix(".delivered"))
    unmount_usb()


def cycle(device):
    """Returns True on a fully completed cycle. Raises FlowError on a failure."""
    rate, extra = config()
    rx_dir = STATE / "rx"
    mount_usb(device)
    files = root_txt_files(MOUNT)
    if len(files) > 1:
        raise FlowError("more than one .txt on the stick root: {}".format(
            ", ".join(f.name for f in files)))
    if files:
        log("role TX: {}".format(files[0].name))
        staged = stage_tx(files[0], STATE / "tx")
        unmount_usb()
        if run_radio("tx", staged, rate, extra) != 0:
            raise FlowError("TX not confirmed (quick_mode exit non-zero)")
        return True
    log("role RX: no .txt on the stick root")
    unmount_usb()
    if pending_files(rx_dir):
        log("delivering earlier received file(s) first; no radio this round")
        deliver_pending(device, rx_dir)
        return True
    received = next_rx_path(rx_dir)
    if run_radio("rx", received, rate, extra) != 0 or not received.exists():
        raise FlowError("RX failed; nothing saved")
    device = find_usb()
    if device is None:
        raise FlowError("USB removed before delivery; file kept on SD and delivered on next stick")
    deliver_pending(device, rx_dir)
    return True


def wait_removed():
    while find_usb() is not None:
        time.sleep(1)


def step():
    """One main-loop iteration. No GPIO (LED) is touched until config is valid."""
    try:
        config()
    except FlowError as error:
        log("CONFIG ERROR: {}".format(error))
        time.sleep(10)
        return
    try:
        device = find_usb()
    except FlowError as error:
        log("ERROR: {}".format(error))
        show(RED, 3)
        time.sleep(3)
        return
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        log("ERROR reading devices: {}".format(error))
        time.sleep(3)
        return
    if device is None:
        time.sleep(1)
        return
    time.sleep(1)  # let the stick settle
    try:
        cycle(device)
        show(GREEN)
        log("done; remove the stick to arm the next round")
        wait_removed()
    except (FlowError, OSError, subprocess.CalledProcessError) as error:
        log("ERROR: {}".format(error))
        try:
            unmount_usb()
        except Exception:
            pass
        show(RED)
        time.sleep(2)


def main():
    log("waiting for USB stick")
    while True:
        step()


if __name__ == "__main__":
    sys.exit(main())
