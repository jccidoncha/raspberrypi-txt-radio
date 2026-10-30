# Raspberry Pi TXT radio

University project: two Raspberry Pi Zero W V1.1 units (TX and RX),
with transceivers and GPIO LEDs.
Owner-selected radio profile: 2.4 GHz band, channel 74 for TX and RX.
On the nRF24L01 RF_CH mapping this is 2474 MHz; no radio driver is enabled.
Separate from emergency-net. Bootstrap proposal, not hardware-ready firmware.

## Run locally (Python 3.9+ standard library)
```
python3 quick_mode.py example.txt --dry-run
python3 harness.py check
```
Host validation: 12 tests passed on CPython 3.9.25 and 3.10.12, x86_64.
This does not validate the Pi ARM/32-bit runtime.

The dry-run reads the explicitly named TXT as raw bytes, prepares a 4-byte count
header and 32-byte chunks, reconstructs locally and prints JSON. It never sends
radio packets, drives GPIO, downloads a URL or scans USB mounts.

`transfer.py` has injectable radio/indicator ports for bounded link-ACK attempts.
Tests exercise fake ports only. A successful result means local framing or fake
link ACKs, not delivery to another Raspberry Pi.

## Structure
Read AGENTS.md, hardware.md and reference.md first. CI checks compilation,
layering, unit/fault tests and optional candidate-diff policy. It does not launch
coding workers or enforce GitHub branch protection. Protected-file changes must
be owner-reviewed; CI does not authenticate human approval. No automatic merge.

## Pending decisions
- Exact nRF24L01-family module variant/driver, physical wiring verification
  and LED GPIO/resistor/polarity. Intended SPI0/CE wiring is in hardware.md.
- Does "download" mean URL to radio TX, USB/local TXT, or radio RX to file?
- OS release/Python version on installed Raspberry Pi OS Lite 32-bit; real device testing and interoperability contract.
- Protocol completion/integrity rules and actual LED semantics.

Only the repository README was initialized on main. Bootstrap is proposed in a
PR for owner review. Hardware adapters remain disabled. No upstream source was
copied, and no new software license is selected.
