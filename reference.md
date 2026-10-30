# Reference comparison and dependency evidence (2026-10-30)

## Untrusted source observations

Only the supplied Python files in `material/` were read as text. They were never
executed, imported, copied into implementation, or licensed for redistribution.
No LICENSE accompanies either reference. Do not commit/publish `material/`, stale
preliminary notes, or extracted source. The owner describes PDF diagrams with
`S mod 256` and undefined `END`. No PDF is present in this checkout, so this is
owner-reported evidence, not a fresh PDF inspection.

| Aspect | lxkalv quick mode | Owner ZIP Transmitter/Receiver |
| --- | --- | --- |
| Source | [quick_mode.py](https://github.com/lxkalv/nrf24.mtp/blob/2c7430227915ee9072f7db8747fe624203b0dd63/quick_mode.py), owner-identified commit | Supplied Transmitter.py / Receiver.py, no upstream URL/license |
| Driver | nrf24 + pigpio | pyrf24 |
| Roles | One interactive program | Separate TX/RX programs |
| Framing | Native signed count header, then <=32-byte chunks | One sequence byte modulo 256 plus 31 data bytes, zero padded |
| Finish | Count only, no integrity or saved-file receipt | FF EOF marker; TX ignores its ACK |
| Retry/loss | Unbounded data retry; unreliable header completion | Failed data skipped while sequence fails to advance |
| RX risks | No sequence check; infinite header wait; may save partial content | No timeout; strips real zeros from each chunk; EOF/data collision possible |
| Files | USB discovery and local fallback | USB placeholders and copy/delete flow |
| Hardware | Import-time connection; CE22, CH76, 2 Mbps, MIN | Import-time devices; CE22, 1 Mbps, LOW, no explicit channel |

Useful principles: selectable roles, declared data amount, bounded payloads and
ordered reception. Neither provides coherent end-to-end completion. Commented
LED construction in the ZIP also leaves active undefined LED references; none
of its color behavior is adopted.

## Original implementation decision

The owner delegated the protocol choice. PT/1 combines those principles with an
explicit discriminator/version, exact byte length, CRC32, random session,
32-bit sequencing, bounded stop-and-wait, atomic no-overwrite save and application
receipt. A fixed 16-byte data body trades throughput for a simple single-packet
header and testable behavior. No windowing, resume, multi-node routing, encryption
or new hardware UI is introduced for this first small TXT demo. README.md gives
the normative wire layout, empty-file behavior and deadlines.

PT/1 is **not interoperable** with either reference. Legacy encode/decode/transmit
and dry-run tests retain their original bytes and meaning. The legacy LED port
is a fake host-test port, not an RGB color or button specification. No new
project-wide software license is selected.

## Selected driver: actual release, not just master

Selected [bjarne-hansen/py-nrf24](https://github.com/bjarne-hansen/py-nrf24), MIT,
using [PyPI nrf24 2.0.0](https://pypi.org/project/nrf24/) with pigpio 1.78
(Unlicense). Local wheel metadata and source were inspected directly:

| File | SHA-256 |
| --- | --- |
| nrf24-2.0.0-py3-none-any.whl | `1c72094ccd5dee887b1f01ac444a4efa0ddd10139df9a8a542565dd878adfbc3` |
| pigpio-1.78-py2.py3-none-any.whl | `81e46f640c4e6342881fa9bbe290dbcd4fc179619dc6591e57a9d4a084dc49fa` |

The nrf24 digest matches PyPI's published wheel digest. Metadata requires Python
>=3.6 and pigpio; this project needs Python >=3.9. These pure Python wheels avoid
a C++ extension build for the ARMv6 32-bit Pi Zero. Upstream reports Pi Zero
Wireless testing; that is not testing of this project, SopwMap module or installed
OS. Native pigpiod compatibility and permissions remain board checks. Local master
nrf24.py was byte-identical to the release, but the release is the adapter's basis.
No driver source is vendored.

Release API verified: NRF24 constructor arguments pi/ce/spi_channel/spi_speed/
data_rate/channel/payload_size/address_bytes/crc_bytes/pad/pa_level; rates
RATE_250KBPS and RATE_2MBPS; PA MIN; retransmission settings, writing/reading pipes,
status/payload methods, power transitions and SPI cleanup. BCM25 and SPI0 CE0
are explicit; CE22, CH76 and default MAX are never inherited.

Release-specific findings and handling:

- send() returns no ACK boolean. wait_until_sent()/is_sending() transition to RX
  and clear TX status, treating MAX_RT as completion. The adapter polls TX_DS/MAX_RT
  before restoring RX, with a bounded wait and FIFO cleanup.
- get_data_rate() combines disjoint bits with AND and cannot report the selected
  rate correctly. RF_SETUP is read directly to check rate/PA/carrier bits.
  get_crc_bytes() has inverted enable logic; CONFIG is checked directly.
- Constructor/setters toggle CE. A pigpio facade holds CE low through configuration
  and readback, clears stale carrier settings and tracks SPI handles even if
  construction fails. No constant-carrier entry point is exposed.
- Explicit loopback/port bypass pigpio environment defaults. Command socket timeout
  and CLI watchdog bound ordinary stalled-daemon failures.

Host evidence: the released wheel was loaded with an original fake SPI/pigpio
device, without a daemon or hardware. Twelve combinations (two roles, two rates,
ACK/MAX_RT/timeout) passed initialization, status, receive and cleanup checks.
The regular harness uses the original fake driver and SPI model in tests/test_radio.py.
This establishes API behavior under simulation only.

## Chip documentation versus owner RF proposal

[Nordic nRF24L01+ Product Specification v1.0](https://docs.nordicsemi.com/r/bundle/pdf_ps_nrf24l01p/)
is also available as the [Nordic-authored PDF hosted by SparkFun](https://www.sparkfun.com/datasheets/Components/SMD/nRF24L01Pluss_Preliminary_Product_Specification_v1_0.pdf).
Sections 5.2/5.3, 6.2/6.3/6.5 and register map 9 support chip-level rate, bandwidth,
power-step and channel facts in hardware.md. They do not identify the actual
amplified module or certify emissions. Owner table values and undefined context
names are preserved separately there.
