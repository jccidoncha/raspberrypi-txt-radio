# Repository guide

University project: two Raspberry Pi Zero W V1.1 units, one TX and one RX,
with transceivers and GPIO LEDs.
Keep this project separate from emergency-net. All artifacts are in English.

## Map and dependencies
- `transfer.py`: pure byte framing and transfer use cases; standard library only.
- `quick_mode.py`: command-line adapter, currently local-file dry-run only.
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
an explicitly scoped task and owner review. This bootstrap task creates those files.
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
Owner confirmed Raspberry Pi Zero W V1.1 on 2026-10-30. Radio model, LED GPIO,
polarity/resistor, SPI/CE/CSN wiring, OS/Python and intended TXT source remain
unknown. See hardware.md. Keep real hardware adapters disabled until resolved.

## First feature boundary
Reference quick mode sends a count header then <=32-byte chunks. It is not an
HTTP download mode. Current code is an original host-testable proposal, not a
claim of wire compatibility. Do not silently replace the protocol with another
format. Before hardware release, agree endianness, empty files, retry bounds,
timeouts, partial-file policy and what the LED means. Never report ACK from the
radio as proof that a remote file was saved. Retry budget: two repair cycles,
then stop and report unresolved failures. Do not weaken checks.
