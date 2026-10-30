# Raspberry Pi local SD TXT radio demo

Original PT/1 software for two Raspberry Pi Zero W V1.1 units running Raspberry
Pi OS Lite 32-bit. Install the **same program on both**; select `tx` or `rx` per
invocation, then swap roles on a later invocation. This is one controlled
transfer at a time. No simultaneous duplex, discovery, relay, USB scanning,
HTTP download, LED control or button action is implemented.

No physical hardware was available. Host tests do not establish ARM compatibility,
radio delivery, electrical safety or regulatory compliance. Read
[hardware.md](hardware.md) and [reference.md](reference.md) before bring-up.

## Host use (Python 3.9+)

```sh
python3 quick_mode.py /explicit/local/file-on-sd.txt --dry-run
python3 harness.py check
git diff --check
```

The legacy dry-run retains its little-endian signed count and 32-byte chunks,
with the original tests unchanged. It never opens hardware. It does **not**
simulate PT/1 or establish interoperability with either reference. New host tests
exercise PT/1, fake radio/SPI, filesystem and CLI behavior.

Implementation validation on CPython 3.10.12: 43 host tests passed, including
all 12 unchanged legacy tests. The actual nrf24 2.0.0 wheel also passed 12
fake-SPI role/rate/status combinations. Whitespace checks passed. These are
author checks, not independent review or Pi/radio measurements.

## Controlled hardware use, only after review

The intended radio is the catalogue SopwMap Sop-155 nRF24L01+PA+LNA SMA pair;
actual modules, 3.3 V supply, wiring and antennas still require inspection.
`--hardware-checked` explicitly attests to the operator's completion of the
checks in hardware.md. It is not hardware detection or certification. Do not
pass it merely to bypass an error. RX also transmits hardware ACKs and replies.

On each reviewed Pi, use Python 3.9+ and the pinned Python dependencies:

```sh
python3 -m pip install nrf24==2.0.0 pigpio==1.78
```

Installation is not performed by this program. Confirm the OS release, suitable
Python environment and compatible local pigpiod daemon separately. The wheels
are platform-independent; this does not validate the native daemon or ARMv6.
The adapter connects only to `127.0.0.1:8888`, ignoring pigpio host/port environment
defaults, and never starts a daemon or configures the OS. Configure pigpiod for
local-only access. There is no remote host, alternate driver or GPIO fallback.

Start RX first and TX within 30 seconds. These examples explicitly select 250 kbps;
replace **both** rate arguments with `2m` for 2 Mbps. There is no implicit rate,
and the arguments do not assign meanings to MRM/NM/SRI.

```sh
# Receiving Pi: existing local directory, new destination filename
python3 quick_mode.py rx /explicit/sd/folder/received.txt --rate 250k --hardware-checked
# Sending Pi: existing regular .txt file on its SD card
python3 quick_mode.py tx /explicit/sd/folder/source.txt --rate 250k --hardware-checked
```

Both sides use channel 74, fixed 32-byte payloads, 5-byte addresses, 2-byte hardware
CRC and chip PA `MIN`. These are proposed software settings except for the
owner-selected channel and explicit rate choice. No MAX or carrier option exists.
SPI0 CE0/BCM8, CE BCM25, 1 MHz SPI clock. Register/address readback must pass
before CE can rise. A process lock prevents concurrent instances from using the
local radio; it does not coordinate unrelated radio software.

Files are binary bytes regardless of encoding: empty files, embedded/trailing
zeros and arbitrary TXT bytes are preserved. The source is never deleted.
The 1 MiB ceiling is a resource cap, not a promised size or throughput; start
with a small file (for example 1 KiB). Stop-and-wait overhead and the five-minute
deadline can prevent large files from completing on a Pi Zero.

TX succeeds only for a matching application `SAVED` receipt. RX reports local
saving after validation and publication. Hardware ACK alone proves neither.
If all saved-file receipts are lost, TX fails as **unconfirmed** even though RX
may have a complete file. Inspect it before retrying with a new output path.
Re-running RX against an existing destination fails; nothing is overwritten.

## PT/1 wire contract

This is an original, explicitly different protocol, incompatible with both
unlicensed references and legacy host framing. All multi-byte integers are
unsigned, big-endian. Every packet is exactly 32 bytes:

| Offset | Bytes | Meaning |
| --- | --- | --- |
| 0 | 2 | ASCII `PT` discriminator |
| 2 | 1 | Version `1` |
| 3 | 1 | START=1, DATA=2, FINISH=3, ACK=4, SAVED=5 |
| 4 | 8 | Nonzero random session ID, selected anew by TX |
| 12 | 4 | Sequence number; never wraps within a transfer |
| 16 | 16 | Type-specific body, zero padded |

START has sequence 0 and body `length:u32, crc32:u32, rate:u8, zero[7]`.
Rate code 1 means 250 kbps; code 2 means 2 Mbps. Length is 0 through 1,048,576.
CRC is CRC-32/ISO-HDLC (reflected polynomial `0xedb88320`, initial/final XOR
`0xffffffff`; `123456789` gives `cbf43926`). It detects accidental corruption,
not malicious changes; there is no authentication or encryption.

DATA sequences are 1 through `ceil(length/16)`, with up to 16 original bytes
per packet. Declared length determines final padding, which must be zero;
actual zero content is preserved. FINISH sequence is data count + 1, with a body
exactly repeating START metadata. There is no in-band EOF marker. ACK repeats
the request session/sequence, with the acknowledged type byte and 15 zeros.
SAVED repeats FINISH's session, sequence and body.

RX accepts one session, in sequence. Foreign versions/sessions, wrong declared
rate, gaps and malformed packets are ignored; invalid final padding/integrity
aborts. An identical duplicate of the last accepted request repeats its cached
reply without appending or saving again. Ignored traffic and duplicates cannot
extend progress deadlines. All bytes and FINISH metadata must pass length/CRC
checks before the save port runs; only successful publication permits SAVED.

Addresses are `PTX01` for requests and `PRX01` for replies, assigned by role,
not board identity. They are demonstration addresses, not unique or secure
identities. Start only one pair in the test area.

## Bounds and completed-file behavior

Each request gets one initial attempt and at most two application repairs. Each
radio send has two hardware retransmissions at 1500 us spacing, with a 100 ms
adapter deadline. TX waits up to one second for an application reply per attempt.
RX waits 30 seconds for START, then 10 seconds without **new progress**. Each
protocol invocation has a 300-second total deadline. RX remains for four seconds
after saving to answer duplicate FINISH requests; duplicates do not extend this.
Driver command socket I/O has a one-second timeout. A Linux CLI watchdog covers
hardware startup/transfer/save for 319 seconds. Deadlines are best-effort under
OS scheduling; uninterruptible OS I/O cannot be guaranteed.

Incomplete content stays in bounded memory. An empty hidden `.pi-txt-*.partial`
file is reserved in the destination directory before opening hardware. On FINISH,
validated content is written and fsynced there, then published by a same-directory
hard link that fails if the final name exists (including a symlink). The staging
name is removed and the directory fsynced before returning the saved receipt.
Use a local POSIX filesystem supporting hard links and directory fsync (such as
ext4); unsupported filesystems fail with no overwrite fallback. The output
directory must exist and be trusted.

Timeouts, ordinary exceptions and Ctrl-C remove staging files. Crash, power loss
or SIGKILL can leave a hidden staging file for manual inspection/removal. There
is no resume or automatic stale-file deletion. A failure after publication can
leave a **complete** final file with no receipt. A completed file is never deleted
to hide that uncertainty. Fsync requests OS persistence; it cannot prove safety
against faulty SD media or power loss. Physical LEDs/buttons remain disabled.

## Structure and review

`transfer.py` contains pure framing/use cases with injected ports, importing only
`struct` and `typing`. `quick_mode.py` owns files, clock/session generation and CLI.
`radio_adapter.py` owns the pinned driver and device lifecycle, with no import-time
hardware effects.

The owner scoped tests and hardware/reference/agent documentation for this feature.
`policy.json` still only allows `transfer.py` and `quick_mode.py` as candidates,
so `harness.py candidate BASE_SHA` rejects this broader diff. Policy/harness/CI
are unchanged. Eight files are changed, but protected paths and the scope gap
still need owner review. Candidate checks omit untracked files; review the two
new files explicitly. Passing host tests is not independent review. No reference
material or preliminary notes belong in a commit. No push or merge.
