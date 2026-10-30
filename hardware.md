# Hardware evidence and bring-up blockers

## Owner confirmations and catalogue identification (2026-10-30)

Two Raspberry Pi Zero W V1.1 units; either selects TX or RX using the same program.
Target OS is Raspberry Pi OS Lite **32-bit**; release, installed Python and native
pigpiod version remain unknown. TXT source bytes come from an explicit local SD
path. RX likewise requires an explicit new local `.txt` destination.

Owner identifies catalogue ASIN **B0DRYRC5BX** as a SopwMap **Sop-155
nRF24L01+PA+LNA SMA pair**. This identifies a listing, not inspected hardware,
silicon authenticity, pin order, current requirements or measured RF output.
Actual amplified modules and antennas have not been inspected or powered here.

Previously listed parts: two AMS1117 fixed 3.3 V modules, two MT3608 adjustable
boost modules, one mini USB charger for 3.7 V 18650/21700/22650 cells, and two
Lishen INR18650-30E 3000 mAh 6 A cells (TerraE label). These are listing descriptions,
not verified ratings or a reviewed power circuit. Do not energize from this list.

## Intended radio wiring, not continuity-tested

| Signal | Pi physical pin | BCM / destination |
| --- | --- | --- |
| Radio CE | 22 | BCM25 |
| Radio CSN (drawn CNS) | 24 | BCM8 / SPI0 CE0 |
| Radio SCK | 23 | BCM11 / SPI0 SCLK |
| Radio MOSI | 19 | BCM10 / SPI0 MOSI |
| Radio MISO | 21 | BCM9 / SPI0 MISO |
| Radio IRQ | none shown | Unconnected; polling proposed |
| AMS1117 VIN+ | 2 | Diagram's 5 V rail input |
| AMS1117 GND | 6 | Common ground, also radio GND |
| Radio VCC | via regulator | Diagram's fixed 3.3 V VOUT+ |

Never substitute reference CE=BCM22 for physical pin 22/BCM25. The earlier diagram
label NFR24L01 and trace crossings do not establish actual module pinout. Verify
against labelled hardware. [Raspberry Pi hardware/SPI documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#serial-peripheral-interface-spi)
supports the SPI0 mapping; it does not verify this wiring.

Radio supply is intended to be **3.3 V**, never the Pi 5 V rail directly. Amplified
module current, decoupling, regulator stability under load and antenna connection
require review. The [AMS1117 family datasheet](http://www.ams-semitech.com/attachments/File/AMS1117_20120314.pdf)
does not establish the installed board's variant, thermal margin, capacitors or
load capacity. Battery, charger, boost and protection wiring are absent from the
reviewed evidence; the diagram is not an instruction to power the build.

## New owner RGB/button image: intended connections only

| Item | Pi physical pin | Intended connection |
| --- | --- | --- |
| RGB red | 11 | BCM17 through its own 330 ohm resistor |
| RGB green | 13 | BCM27 through its own 330 ohm resistor |
| RGB blue | 15 | BCM22 through its own 330 ohm resistor |
| RGB common | 9 | GND **only if common-cathode**, still unconfirmed |
| Button | 16 | BCM23, other terminal to GND physical 14 |

Confirm RGB common-cathode versus common-anode, actual leads, resistor suitability
and GPIO limits before connecting. The image supplies no agreed color meanings,
button action, pull resistor configuration or debounce behavior. This feature
never accesses these GPIOs and assigns no physical LED semantics. The old host
fake indicator is not a physical LED specification.

## Latest owner radio table, preserved as a proposal

| Owner table item | Value / unresolved interpretation |
| --- | --- |
| Modulation | GFSK, BT 0.5 |
| Deviation | +/-160 kHz; +/-320 kHz at 2 Mbps |
| Named contexts | MRM 250 kbps, NM 250 kbps, SRI 2 Mbps; meanings undefined |
| Bandwidth | 0.8 / 1 / 2 MHz for 250 kbps / 1 Mbps / 2 Mbps |
| Own channel | CH74 for SRI/MRM; 2474 MHz centre |
| Table interval | 2473.5-2474.5 MHz |
| NM common channel | To agree; no automatic NM selection |
| Reserve channel | CH68 = 2468 MHz; not enabled by this feature |
| Power/antenna claim | PA_LEVEL_MAX 18 dBm, antenna 2 dBi, EIRP 19.4 dBm <=20 |

MRM/NM/SRI stay undefined; no mode meaning is inferred. CLI requires `--rate 250k`
or `--rate 2m`, identically on both peers, and fixes CH74 unless a future explicit
owner selection changes it. It never silently chooses a mode.

Chip facts from [Nordic's specification](https://www.sparkfun.com/datasheets/Components/SMD/nRF24L01Pluss_Preliminary_Product_Specification_v1_0.pdf):
nRF24L01+ supports 250 kbps, 1 Mbps and 2 Mbps. RF_CH uses 2400 + channel MHz,
so 74 yields 2474 MHz, not a Wi-Fi channel number. Typical deviations are
160/160/320 kHz; maximum 20 dB bandwidth figures are 0.8/1/2 MHz respectively.
The table interval is only 1 MHz wide and must not be claimed to contain the
full 2 Mbps emission. Nordic describes at least 2 MHz channel spacing at 2 Mbps.
GFSK/deviation are chip characteristics, not separate knobs in this API; BT 0.5
remains an owner-table assertion, not a measured module result.

PA_LEVEL_MAX is a chip/driver selection, **not measured 18 dBm** from the SopwMap
amplifier. Nordic's bare-chip power steps are -18/-12/-6/0 dBm; the amplifier,
antenna and feed loss determine assembled output and EIRP. 18 + 2 alone gives 20,
not 19.4; the implied 0.6 dB loss is unestablished. Actual conducted output, antenna
gain, feed loss and regulatory requirements remain unresolved. No EIRP, compliance
or certification claim is made. Software fixes PA to **MIN**, with no MAX or
carrier option. Even MIN does not certify amplified-module radiated power.

## Proposed software profile, pending physical validation

Pinned nrf24==2.0.0 and pigpio==1.78, local-only pigpiod at 127.0.0.1:8888.
SPI0 CE0 at 1 MHz, CE BCM25, channel 74, explicit rate, chip PA MIN, two-byte
hardware CRC, fixed 32-byte payloads. Addresses PTX01 (requests) / PRX01 (replies)
depend on role. Two hardware retransmissions at 1500 us spacing; bounded
application repairs/deadlines are in README.md. Addresses, SPI speed and retry
profile are implementation proposals, not owner hardware decisions or measured
results. The inspected release and nRF24L01+ specification support both rates;
neither has been tested on these units.

## Required before passing --hardware-checked

1. Obtain owner/electrical review of actual module identity/pinout and continuity,
   supply voltage/polarity/current ratings, common ground and decoupling. Review
   the complete battery protection/charging/boost/regulator design if batteries
   are used. Do not substitute a parts list for this review.
2. Verify the amplified module's 3.3 V supply under load and correct connected
   antenna, connector type and RF suitability. Resolve actual power/gain/loss and
   operating permission; the table is not authorization to use MAX.
3. Confirm OS release, Python >=3.9, ARMv6/32-bit native pigpiod support, local-only
   daemon setup, SPI0 availability and permissions. Stop unrelated radio programs.
4. Confirm matching rates. Start with a small file, compare exact received bytes,
   test loss/timeouts/interruption/receipt loss, then swap roles. Record measured
   results separately from this software proposal.

Unresolved RGB polarity/function and button behavior remain excluded from radio
bring-up; leave them inactive. Imports and host tests open no GPIO or radio.
Device access is explicitly gated, but opt-in proves none of these checks.
No measured hardware results exist yet.
