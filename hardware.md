# Hardware inventory and bring-up blockers

## Confirmed by owner
Raspberry Pi Zero W V1.1 (message, 2026-10-30), two units with TX and RX roles. Project includes a transceiver
and a GPIO LED, but their model and wiring have not been supplied.

## Listed in supplied parts PDF
- AMS1117 fixed 3.3 V step-down module: 2.
- MT3608 adjustable boost module: 2.
- Mini USB charger for 3.7 V 18650/21700/22650 cells: 1.
- Lishen INR18650-30E 3000 mAh 6 A Li-ion cells (TerraE label): 2.
These are listing descriptions, not verified electrical ratings or an approved
power circuit. The PDF does not identify the Pi or transceiver.

## Must resolve before hardware adapters
Installed OS: Raspberry Pi OS Lite, 32-bit, as reported by owner. Release and
Python version not supplied; host CI is not a 32-bit board test.

Radio exact model and driver/license; SPI device and CE/CSN; LED BCM pin,
polarity and resistor; power schematic, ground, battery topology/protection,
charger behavior and regulator ratings; OS release and Python version.
Do not wire either cells or converters from this list alone.
No driver is installed automatically. CI runs on a host and does not prove this
board can run a selected GPIO library. Pi/OS bring-up and paired radio testing
remain mandatory. No radio or physical LED has been tested.

## Proposed LED semantics
On during a transfer attempt, off on completion or error. Not a delivery receipt.
Pending owner/wiring confirmation; only a fake indicator exists in tests.
