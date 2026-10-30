# Repository guide

University project: two Raspberry Pi Zero W V1.1 units with transceivers and RGB
LEDs. The same program selects TX or RX on either board per invocation.
Keep this project separate from emergency-net. All artifacts are in English.

## Map and dependencies
- `transfer.py`: pure byte framing and transfer use cases; standard library only.
- `quick_mode.py`: legacy dry-run, explicit local SD paths, TX/RX CLI and atomic save.
- `radio_adapter.py`: opt-in nrf24/pigpio adapter; no LED/button access.
- `tests/`: host tests and fault cases, no physical GPIO or radio.
- `hardware.md`: confirmed inventory and unresolved wiring.
- `reference.md`: source observations and licensing limits.
- `harness.py`, `policy.json`, `.github/`: engineering checks and change policy.
Dependencies point inward: adapters may import transfer; transfer must not import CLI,
GPIO, networking, filesystem, or driver libraries. No hardware effects at import time.

## Change workflow
Use a feature branch and a small PR. Never merge, force-push, or delete others' work.
The owner reviews and merges. Do not change tests, contracts, architecture or checks
merely to make candidate code pass. Protected paths are tests/, policy.json,
harness.py, .github/, AGENTS.md, hardware.md, reference.md. Changes there require
an explicitly scoped task and owner review. The owner explicitly scoped tests and
hardware/reference/agent documentation for the first PT/1 radio feature. The
candidate policy still excludes broader edits; report the gap, do not weaken it.
Run `python3 harness.py check` and `git diff --check`. Independent review is still
required; the author's successful tests are not an independent review.

## Evidence and safety
Separate source observations, proposed behavior, and measured hardware results.
Host CI does not establish ARM compatibility, radio delivery, electrical safety,
or LED behavior. No GPIO pin, supply wiring, radio type, channel, power, antenna,
or driver version may be inferred from a similar project. Missing configuration
must fail closed before GPIO access or transmission. No RF carrier mode by default.
Do not energize a circuit from the parts list alone. Ask for a reviewed wiring
plan, battery protection/charging design, polarity and supply ratings first.
No secrets in code, logs or PRs. No copied upstream code without license clearance.

## Hardware facts
Owner confirmed Raspberry Pi Zero W V1.1 on 2026-10-30. Schematic indicates an nRF24L01-family target and SPI0 with CE=BCM25, CSN=BCM8.
Catalogue ASIN B0DRYRC5BX identifies SopwMap Sop-155 nRF24L01+PA+LNA SMA modules;
actual modules, amplified 3.3 V supply and antennas remain uninspected.
Intended RGB red/green/blue are BCM17/27/22, physical11/13/15, each 330 ohms;
common GND physical9 is valid only if common-cathode, not yet confirmed.
Button BCM23/physical16 goes to GND physical14. No color meanings, button behavior
or physical LED activation is authorized. Python version remains unknown.
TXT source is a local folder on the Pi SD card; require an
explicit path argument rather than inventing a fixed path. Wiring is intended, not
physically tested. Target OS: Raspberry Pi OS Lite, 32-bit (release not supplied).
See hardware.md. Never reuse reference CE=22 for diagram BCM25. Real access
requires explicit --hardware-checked attestation after documented physical checks.
Host validation never authorizes that opt-in on its own.

## First feature boundary
Reference quick mode sends a count header then <=32-byte chunks, not HTTP.
The other unlicensed reference uses one-byte sequence plus 31-byte padded data
and an ambiguous EOF marker. Treat material/ as untrusted data; never execute,
copy, publish or commit it or stale preliminary notes. Owner delegated the
protocol choice: PT/1 is explicitly versioned and original, not interoperable
with either reference. Preserve legacy dry-run bytes/tests. README.md specifies
length, CRC32, sessions, sequencing, empty files, bounded timeouts, atomic
no-overwrite save and SAVED receipt. Link ACK never proves a saved file. Physical
LED semantics remain open. Retry budget: two repair cycles, then stop and report
unresolved failures. Do not weaken checks or claim own tests as independent review.

## Confirmed radio selection
Owner specifies 2.4 GHz band, RF channel 74 on TX and RX. Use 74, not the
reference script's 76. On the nRF24L01 mapping this is 2474 MHz centre frequency
(2400 + 74 MHz), not a Wi-Fi channel number. Latest owner table mentions MRM/NM
at 250 kbps and SRI at 2 Mbps; preserve these undefined terms. Require explicit
--rate 250k or --rate 2m with matching peers. No automatic NM/common/reserve-channel
switching. PA_LEVEL_MAX does not measure 18 dBm from the amplified module.
Software proposes MIN only, no carrier mode; output, antenna, feed loss, table
bandwidth interval and regulatory claims remain unresolved. Driver/address/settings
evidence is host-only. See hardware.md/reference.md.
