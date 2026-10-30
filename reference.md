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
  Readback masks only bit 0 (`0xfe`), which Nordic's register map marks obsolete
  and don't-care; both values are accepted. Rate, MIN power, carrier, PLL and
  reserved bits remain checked. get_crc_bytes() has inverted enable logic;
  CONFIG is checked directly.
- Constructor/setters toggle CE. A pigpio facade holds CE low through configuration
  and readback, clears stale carrier settings and tracks SPI handles even if
  construction fails. It writes CE low before construction and before forwarding
  output-mode selection. In [pigpio v78 source](https://raw.githubusercontent.com/joan2937/pigpio/v78/pigpio.c),
  `gpioSetMode` retains the latch; `gpioWrite` sets the level before selecting
  output. Suppressing explicit high writes alone did not cover a retained-high
  input latch. No constant-carrier entry point is exposed.
- Explicit loopback/port bypass pigpio environment defaults. Command socket timeout
  and CLI watchdog bound ordinary stalled-daemon failures. The 100 ms send budget
  now starts before settling/preparation, and receive checks expiry after readiness
  and payload calls. The socket facade passes the remaining budget to every
  send/sendall/recv, including pigpio's repeated `_rxbuf` reads. A partial or
  timed-out command stream is invalidated rather than reused for cleanup.
- RX recovery and close each have a shared 200 ms allowance. Recovery failure
  disconnects without a second cleanup attempt. pigpio 1.78 `stop()` normally
  calls `_notify.stop()`, which sends on a separate, unbounded socket. With no
  callbacks used here, the adapter stops notifications by local shutdown/close
  before `pi.stop()` closes the command socket. No thread join or daemon reply
  is awaited. This is pinned-release handling, not a general pigpio facade.

## Repair cycle 1 host evidence (2026-10-30)

Independent review identified CE latch exposure, incomplete I/O deadlines and
the obsolete-bit readback requirement. This targeted repair addresses all three;
no protocol or legacy behavior is changed. The original 43 tests are unchanged.
`python3 harness.py check` passes 60 tests, including 17 new regressions in
`tests/test_radio_repair.py`. `git diff --check` passes. No additional independent
audit is claimed or requested by this repair.

`python3 tests/probe_radio_release.py /downloads/driver-deps` passes 21 tests using
the exact wheels above, imported directly without installation. Original fake
GPIO/SPI and command sockets are used; no daemon constructor or physical access
is invoked. Coverage includes:

- 64 nrf24 combinations: two roles, two rates, two bit-0 values, and four statuses
  (TX_DS, MAX_RT, both, neither). MAX_RT takes precedence. A further 48 combinations
  run both released libraries together over fake daemon/SPI replies.
- Retained-high input latch plus failed readback, both rates: first GPIO action
  is write-low; no modeled CE-high transition occurs. Failed initial write never
  reaches the driver constructor. Partial initialization still closes its handle.
- Both bit-0 readback values pass. Flipping any other RF_SETUP bit fails readback
  before enabling CE, at both rates.
- The review's 20 ms per GPIO/SPI-call model (which deliberately ignores socket
  timeouts) now returns `False` for send after 245 ms including RX recovery and
  `None` for a 10 ms receive after its first 20 ms call. Post-call checks reject
  late results; this deliberately uninterruptible fake can overshoot a budget.
- With both released libraries and timeout-respecting fake sockets at 20 ms per
  socket phase, send stops with an error at 100 ms and receive at 10 ms, at both
  rates. Neither accepts late success. A stalled close stops at 200 ms total.
  Partial replies invalidate the stream and disconnect without more commands.
- Trickled SPI payload: 20 ms per socket phase completes four phases in 80 ms
  under a 100 ms budget; a 70 ms budget interrupts the fourth phase at 70 ms.
  Expiry between command and reply also invalidates the stream. Notification
  cleanup issues no socket send, even with a modeled stalled notification socket.

These are injected-clock host results, not wall-clock real-time guarantees,
independent review, ARM execution, electrical checks or RF measurements. The
candidate policy still excludes these scoped paths and is unchanged; owner review
and parent publication remain separate. No further audit round is performed.

## Chip documentation versus owner RF proposal

[Nordic nRF24L01+ Product Specification v1.0](https://docs.nordicsemi.com/r/bundle/pdf_ps_nrf24l01p/)
is also available as the [Nordic-authored PDF hosted by SparkFun](https://www.sparkfun.com/datasheets/Components/SMD/nRF24L01Pluss_Preliminary_Product_Specification_v1_0.pdf).
Sections 5.2/5.3, 6.2/6.3/6.5 and register map 9 support chip-level rate, bandwidth,
power-step and channel facts in hardware.md. They do not identify the actual
amplified module or certify emissions. Owner table values and undefined context
names are preserved separately there.
