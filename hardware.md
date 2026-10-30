# Hardware inventory and bring-up blockers

## Confirmed by owner
Raspberry Pi Zero W V1.1 (message, 2026-10-30), two units with TX and RX roles. Project includes a transceiver
and a GPIO LED. A later schematic supplies intended radio/SPI wiring below;
LED wiring is still absent.

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

Radio exact module variant and driver/license; actual continuity to the
schematic SPI0/CE/CSN connections; LED BCM pin,
polarity and resistor; power schematic, ground, battery topology/protection,
charger behavior and regulator ratings; OS release and Python version.
Do not wire either cells or converters from this list alone.
No driver is installed automatically. CI runs on a host and does not prove this
board can run a selected GPIO library. Pi/OS bring-up and paired radio testing
remain mandatory. No radio or physical LED has been tested.

## Proposed LED semantics
On during a transfer attempt, off on completion or error. Not a delivery receipt.
Pending owner/wiring confirmation; only a fake indicator exists in tests.

## User-supplied schematic, 2026-10-30
The diagram is evidence of intended connections, not continuity, a tested build,
or electrical approval. Radio box reads `NFR24L01`, apparently a spelling error;
treat as an nRF24L01-family target, exact chip/module variant still unverified.
Do not infer nRF24L01+, PA/LNA, pin order or current from this block diagram.

| Diagram signal | Pi physical pin | BCM / destination |
| --- | --- | --- |
| AMS1117 VIN+ | 2 | 5 V supply rail |
| AMS1117 GND | 6 | Common ground, also radio GND |
| Radio VCC | via regulator | AMS1117 fixed 3.3 V VOUT+ |
| Radio CE | 22 | GPIO25 (not BCM22) |
| Radio CSN (drawn CNS) | 24 | GPIO8 / SPI0 CE0 |
| Radio SCK | 23 | GPIO11 / SPI0 SCLK |
| Radio MOSI | 19 | GPIO10 / SPI0 MOSI |
| Radio MISO | 21 | GPIO9 / SPI0 MISO |
| Radio IRQ | none shown | Unconnected in diagram |
| LED | absent | Pin, polarity, resistor and wiring unresolved |

The long blue SPI traces were followed individually; crossings without dots
are not junctions. Verify continuity against the actual module's labelled pins
before power. The reference script's CE=22 is incompatible with diagram BCM25
if interpreted as BCM numbering. No hardware adapter is configured yet.

Official Raspberry Pi SPI0 mapping matches the four SPI rows:
https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/raspberry-pi/spi-bus-on-raspberry-pi.adoc
Nordic nRF24L01 chip specification states VDD 1.9-3.6 V. Do not connect radio
VCC to the Pi 5 V rail; 5 V is only the depicted regulator input.
https://devzone.nordicsemi.com/cfs-file/__key/communityserver-discussions-components-files/4/content.pdf
AMS1117 family datasheet lists fixed 3.3 V and adjustable variants. Verify the
actual module is fixed 3.3 V, input/output labels, capacitors, output voltage
under load, heat and supply stability before connecting the radio.
http://www.ams-semitech.com/attachments/File/AMS1117_20120314.pdf
These chip sources do not certify the unidentified radio/regulator modules.
The drawing omits battery, boost and charger connections, so those parts remain
outside any reviewed power design. No instruction to power the build is given.

## Owner-selected radio channel (2026-10-30)
Owner specifies the 2.4 GHz band and channel 74 for both TX and RX. This
supersedes reference quick_mode.py channel 76; do not carry 76 into a driver.
For the Nordic nRF24L01 RF_CH mapping, F0 = 2400 + RF_CH MHz, so channel 74
corresponds to a 2474 MHz centre frequency. This is a calculated chip setting,
not a measured emission or a claim of tested RF performance/regulatory approval.
Source: Nordic product specification linked above, RF channel frequency section.
Data rate, PA level, addresses and exact module variant are not selected by
this channel clarification. Both nodes must eventually use the same profile.
No radio driver is enabled and no physical transmission has been attempted.
