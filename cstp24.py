"""CSTP-24 chunked session transfer core: struct, zlib and typing only.

Implements the Chunked Session Transfer Protocol (CSTP-24) wire format:
5-byte little-endian header (kind, session, sequence) plus up to 27 bytes of
body per 32-byte nRF24L01 ESB frame. Files move in independent 4 KB chunks,
each compressed with an incompressible-data guard, CRC-32 verified, durably
committed by the receiver before CHUNK_ACK, with selective-repeat recovery
through a 152-bit missing-packet bitmap and .part/.state crash resume.

This module is pure framing and state machines. All filesystem and radio I/O
is injected by the caller (quick_mode.py), keeping the core host-testable.
Frames leave here at their exact spec length (<= 32 bytes); the radio glue
zero-pads to the adapter's fixed 32-byte payload and receivers parse by kind,
so padding is transparent (see PR notes on DPL).
"""
import struct
import zlib
from typing import Callable, Optional, Protocol

# Wire constants (spec sections 2-4).
HEADER = struct.Struct("<BHH")  # kind, session, sequence
BODY_BYTES = 27
FRAME_MAX = 32
CHUNK_BYTES = 4096
MAX_PACKETS = 152  # ceil(4096 / 27)
BITMAP_BYTES = 19  # 152 bits
VERSION = 1

# Packet kinds (spec section 4).
SESSION_INIT = 0x01
SESSION_ACK = 0x02
CHUNK_START = 0x03
CHUNK_DATA = 0x04
CHUNK_ACK = 0x05
CHUNK_NACK = 0x06
SESSION_END = 0x07
SESSION_END_ACK = 0x08
SESSION_ABORT = 0xFF

# SESSION_ACK status codes.
OK_NEW = 0x00
BUSY = 0x01
BAD_ALGO = 0x02
NO_SPACE = 0x03
OK_RESUME = 0x04

# CHUNK_NACK reason codes.
MISSING_PKTS = 0x01
CRC_ERR = 0x02

# Compression algorithms (SESSION_INIT algo field).
ALGO_NONE = 0
ALGO_ZLIB = 1
ALGO_LZ4 = 2  # Declared by the spec; not implemented (no new dependency).

# Abort error codes.
ABORT_TIMEOUT = 1
ABORT_SEQ_ERR = 2
ABORT_IO_ERR = 3
ABORT_CANCEL = 4

# Timing (spec sections 5, 6 and 8), overridable by the caller for field use.
T_ACK_WAIT = 0.015      # reverse-channel wait after a burst
T_GAP = 0.005           # spec idle-gap arming the proactive-NACK watchdog
T_SESSION_DEAD = 5.0    # inactivity that returns the receiver to IDLE
# Host-chosen margin over the spec's T_GAP before the receiver actually
# transmits a gap NACK: radio_adapter serializes a 5 ms pre-send settle plus
# airtime and ESB turnaround per frame, so healthy in-burst spacing already
# exceeds 5 ms on the wire and NACKing at T_GAP would collide with the rest
# of the burst. Needs on-air validation with the protocol team (PR notes).
GAP_NACK_IDLE = 0.025
# After a successful finalize the receiver keeps answering duplicate
# SESSION_END frames for the sender's whole retry window (5 attempts x 1s),
# so a lost END_ACK never leaves the sender reporting failure on a saved file.
END_LINGER = 5.1
HANDSHAKE_TIMEOUT = 1.0 # per-attempt wait for SESSION_ACK / SESSION_END_ACK
START_TIMEOUT = 30.0    # receiver wait for the first SESSION_INIT
TOTAL_TIMEOUT = 300.0   # whole-transfer bound
SESSION_ATTEMPTS = 5    # handshake / end-of-session attempts
CHUNK_ATTEMPTS = 25     # full-chunk retries before abort

# Receiver .state record: file_size, file_crc32, next_chunk_index, state_crc32.
STATE = struct.Struct("<QIII")

_BODY = {
    SESSION_INIT: struct.Struct("<BBHQII"),   # ver, algo, chunk_size, file_size, total_chunks, file_crc32
    SESSION_ACK: struct.Struct("<BHI"),       # status, rx_turnaround_us, resume_chunk_index
    CHUNK_START: struct.Struct("<IHHIB"),     # chunk_index, raw_len, wire_len, chunk_crc32, flags
    CHUNK_ACK: struct.Struct("<IB"),          # chunk_index, status
    CHUNK_NACK: struct.Struct("<IB19s"),      # chunk_index, reason, pkt_bitmap
    SESSION_END: struct.Struct("<II"),        # total_chunks, file_crc32
    SESSION_END_ACK: struct.Struct("<B"),     # status
    SESSION_ABORT: struct.Struct("<B"),       # error_code
}


class SessionError(ValueError):
    """Protocol, integrity or peer-rejection error."""


class DuplexRadio(Protocol):
    def send(self, frame: bytes) -> bool:
        """Bounded send of one <=32-byte frame; return link ACK only."""

    def receive(self, timeout: float) -> Optional[bytes]:
        """Return one frame or None within timeout seconds."""


def crc32(content: bytes) -> int:
    """IEEE 802.3 CRC-32 as required for chunk and file checksums."""
    return zlib.crc32(content) & 0xFFFFFFFF


def frame(kind: int, session: int, sequence: int, body: bytes = b"") -> bytes:
    if not 0 < session < 2**16:
        raise ValueError("session must be a non-zero 16-bit id")
    if not 0 <= sequence < 2**16:
        raise ValueError("sequence must fit 16 bits")
    if len(body) > BODY_BYTES:
        raise ValueError("CSTP-24 body exceeds 27 bytes")
    return HEADER.pack(kind, session, sequence) + body


def parse(raw: bytes):
    """Split a frame into (kind, session, sequence, body); None when malformed.

    Accepts zero-padded fixed 32-byte frames: body sizes are fixed per kind
    except CHUNK_DATA, whose meaningful length the receiver derives from the
    active chunk's wire_len, so trailing pad bytes are ignored here.
    """
    if len(raw) < HEADER.size or len(raw) > FRAME_MAX:
        return None
    kind, session, sequence = HEADER.unpack(raw[:HEADER.size])
    if session == 0 or kind not in _BODY and kind != CHUNK_DATA:
        return None
    body = raw[HEADER.size:]
    layout = _BODY.get(kind)
    if layout is not None and len(body) < layout.size:
        return None
    return kind, session, sequence, body


def compress_chunk(raw: bytes, compress: bool = True) -> tuple[bytes, int]:
    """Apply the portioned compression guard: never expand on the wire.

    Returns (wire_payload, compressed_flag). zlib level 6 per spec section 3.
    """
    if len(raw) > CHUNK_BYTES:
        raise ValueError("chunk exceeds 4096 bytes")
    if compress and raw:
        packed = zlib.compress(raw, 6)
        if len(packed) < len(raw):
            return packed, 1
    return raw, 0


def decompress_chunk(wire: bytes, flag: int, raw_len: int) -> bytes:
    raw = zlib.decompress(wire) if flag else bytes(wire)
    if len(raw) != raw_len:
        raise SessionError("decompressed length mismatch")
    return raw


def packets_for(wire_len: int) -> int:
    return (wire_len + BODY_BYTES - 1) // BODY_BYTES


def init_frame(session: int, algo: int, file_size: int, total_chunks: int,
               file_crc: int) -> bytes:
    return frame(SESSION_INIT, session, 0,
                 _BODY[SESSION_INIT].pack(VERSION, algo, CHUNK_BYTES,
                                          file_size, total_chunks, file_crc))


def send_session(read_chunk: Callable[[int], bytes], total_chunks: int,
                 file_size: int, file_crc: int, session: int,
                 radio: DuplexRadio, now: Callable[[], float],
                 algo: int = ALGO_ZLIB) -> dict:
    """Send one file from resume point to verified END_ACK; raise otherwise."""
    if algo not in (ALGO_NONE, ALGO_ZLIB):
        raise ValueError("only algo 0 (none) and 1 (zlib) are implemented")
    deadline = now() + TOTAL_TIMEOUT
    stats = {"chunks": total_chunks, "resumed_from": 0, "data_frames": 0,
             "nacks": 0, "chunk_retries": 0}

    def collect(until):
        while now() < until:
            raw = radio.receive(max(0.0, until - now()))
            if raw is None:
                continue
            parsed = parse(raw)
            if parsed is not None and parsed[1] == session:
                return parsed
        return None

    # Handshake with resume negotiation (spec section 5 step 1).
    resume = None
    init = init_frame(session, algo, file_size, total_chunks, file_crc)
    for _ in range(SESSION_ATTEMPTS):
        if now() >= deadline:
            break
        radio.send(init)
        reply = collect(min(deadline, now() + HANDSHAKE_TIMEOUT))
        if reply is None:
            continue
        kind, _, _, body = reply
        if kind == SESSION_ABORT:
            raise SessionError("peer aborted during handshake")
        if kind != SESSION_ACK:
            continue
        status, _, resume_index = _BODY[SESSION_ACK].unpack(body[:7])
        if status in (OK_NEW, OK_RESUME):
            resume = resume_index if status == OK_RESUME else 0
        elif status == BUSY:
            raise SessionError("receiver busy")
        elif status == BAD_ALGO:
            raise SessionError("receiver rejected compression algorithm")
        elif status == NO_SPACE:
            raise SessionError("receiver out of space")
        else:
            continue
        break
    if resume is None:
        _abort(radio, session, ABORT_TIMEOUT)
        raise SessionError("no SESSION_ACK within attempts")
    if not 0 <= resume <= total_chunks:
        _abort(radio, session, ABORT_SEQ_ERR)
        raise SessionError("invalid resume index")
    stats["resumed_from"] = resume

    # Stop-and-wait chunk loop with selective recovery (spec steps 2-6).
    for index in range(resume, total_chunks):
        raw = read_chunk(index)
        if len(raw) > CHUNK_BYTES:
            _abort(radio, session, ABORT_IO_ERR)
            raise SessionError("read_chunk returned too many bytes")
        wire, flag = compress_chunk(raw, compress=(algo == ALGO_ZLIB))
        chunk_crc = crc32(wire)
        count = packets_for(len(wire))
        start = frame(CHUNK_START, session, count,
                      _BODY[CHUNK_START].pack(index, len(raw), len(wire),
                                              chunk_crc, flag))
        data = [frame(CHUNK_DATA, session, seq,
                      wire[seq * BODY_BYTES:(seq + 1) * BODY_BYTES])
                for seq in range(count)]
        acknowledged = False
        for _ in range(CHUNK_ATTEMPTS):
            if now() >= deadline:
                break
            radio.send(start)
            for packet_frame in data:
                radio.send(packet_frame)
            stats["data_frames"] += len(data)
            while now() < deadline:
                reply = collect(min(deadline, now() + T_ACK_WAIT))
                if reply is None:
                    break  # T_ACK_WAIT silence: resend whole chunk.
                kind, _, sequence, body = reply
                if kind == CHUNK_ACK:
                    ack_index, status = _BODY[CHUNK_ACK].unpack(body[:5])
                    if ack_index == index and status == 0:
                        acknowledged = True
                elif kind == CHUNK_NACK:
                    nack_index, reason, bitmap = _BODY[CHUNK_NACK].unpack(body[:24])
                    if nack_index != index:
                        continue
                    stats["nacks"] += 1
                    if reason == MISSING_PKTS:
                        wanted = [s for s in range(count)
                                  if bitmap[s // 8] >> (s % 8) & 1]
                        for seq in wanted:
                            radio.send(data[seq])
                        stats["data_frames"] += len(wanted)
                        continue  # wait again for the verdict
                    break  # CRC_ERR: full clean retransmission
                elif kind == SESSION_ABORT:
                    _abort(radio, session, ABORT_CANCEL)
                    raise SessionError("peer aborted mid-chunk")
                break
            if acknowledged:
                break
            stats["chunk_retries"] += 1
        if not acknowledged:
            _abort(radio, session, ABORT_TIMEOUT)
            raise SessionError("chunk {} not acknowledged within budget".format(index))

    # Close: SESSION_END until END_ACK (spec step 6).
    end = frame(SESSION_END, session, 0,
                _BODY[SESSION_END].pack(total_chunks, file_crc))
    for _ in range(SESSION_ATTEMPTS):
        if now() >= deadline:
            break
        radio.send(end)
        reply = collect(min(deadline, now() + HANDSHAKE_TIMEOUT))
        if reply is None:
            continue
        kind, _, _, body = reply
        if kind == SESSION_END_ACK:
            status, = _BODY[SESSION_END_ACK].unpack(body[:1])
            if status == 0:
                return stats
            _abort(radio, session, ABORT_IO_ERR)
            raise SessionError("receiver reports whole-file CRC mismatch")
        if kind == SESSION_ABORT:
            raise SessionError("peer aborted at close")
    _abort(radio, session, ABORT_TIMEOUT)
    raise SessionError("no SESSION_END_ACK within attempts")


def receive_session(radio: DuplexRadio, now: Callable[[], float],
                    lookup: Callable[[int, int, int], int],
                    commit: Callable[[int, bytes], None],
                    finalize: Callable[[int], bool]) -> dict:
    """Receive one session; raise SessionError on abort or timeout.

    lookup(file_size, file_crc32, total_chunks) -> next_chunk_index (0 = new).
    commit(chunk_index, raw_bytes) must durably write .part and .state.
    finalize(file_crc32) -> True only when the whole file verified and was
    published; False leaves .part/.state for inspection and reports CRC error.
    """
    deadline = now() + TOTAL_TIMEOUT
    wait_deadline = now() + START_TIMEOUT
    session = None
    algo = ALGO_ZLIB
    expect = 0
    total_chunks = 0
    file_crc = 0
    active = None       # dict with the in-flight chunk assembly
    last_activity = now()
    last_data = now()
    last_gap_nack = 0.0
    last_committed = None  # (chunk_index, packet_count) for duplicate START
    stats = {"chunks": 0, "data_frames": 0, "nacks": 0, "resumed_from": 0}

    def nack(index: int, reason: int, missing: int, bitmap: bytes):
        radio.send(frame(CHUNK_NACK, session, missing,
                         _BODY[CHUNK_NACK].pack(index, reason, bitmap)))

    while now() < deadline:
        if session is None:
            if now() >= wait_deadline:
                raise SessionError("no SESSION_INIT within start timeout")
            timeout = min(deadline, wait_deadline) - now()
        else:
            timeout = min(deadline, now() + T_GAP) - now()
        raw = radio.receive(max(0.0, timeout))
        moment = now()
        if raw is None:
            if session is not None:
                if moment - last_activity > T_SESSION_DEAD:
                    session = None  # keep .part/.state for a later resume
                    active = None
                    wait_deadline = moment + START_TIMEOUT
                    continue
                if (active is not None and active["missing"] > 0
                        and moment - last_data > GAP_NACK_IDLE
                        and moment - last_gap_nack > GAP_NACK_IDLE):
                    bitmap = bytearray(BITMAP_BYTES)
                    for seq in range(active["count"]):
                        if not active["seen"][seq]:
                            bitmap[seq // 8] |= 1 << (seq % 8)
                    nack(active["index"], MISSING_PKTS, active["missing"],
                         bytes(bitmap))
                    stats["nacks"] += 1
                    last_gap_nack = moment
            continue
        parsed = parse(raw)
        if parsed is None:
            continue
        kind, frame_session, sequence, body = parsed

        if kind == SESSION_INIT:
            ver, init_algo, chunk_size, init_size, init_total, init_crc = \
                _BODY[SESSION_INIT].unpack(body[:20])
            if ver != VERSION or chunk_size != CHUNK_BYTES or init_size < 0:
                continue
            if init_total != (init_size + CHUNK_BYTES - 1) // CHUNK_BYTES:
                continue
            if init_algo not in (ALGO_NONE, ALGO_ZLIB):
                radio.send(frame(SESSION_ACK, frame_session, 0,
                                 _BODY[SESSION_ACK].pack(BAD_ALGO, 0, 0)))
                continue
            if session is not None and (
                    frame_session != session
                    or (init_size, init_crc, init_total, init_algo)
                    != (file_size, file_crc, total_chunks, algo)):
                # A competing session must never discard the active transfer
                # or its durable checkpoint; an identical INIT is the
                # idempotent duplicate handled below.
                radio.send(frame(SESSION_ACK, frame_session, 0,
                                 _BODY[SESSION_ACK].pack(BUSY, 0, 0)))
                continue
            session = frame_session
            algo = init_algo
            total_chunks = init_total
            file_size = init_size
            file_crc = init_crc
            expect = lookup(file_size, file_crc, total_chunks)
            if not 0 <= expect <= total_chunks:
                expect = 0
            active = None
            last_committed = None
            last_activity = moment
            status = OK_RESUME if expect else OK_NEW
            stats["resumed_from"] = expect
            radio.send(frame(SESSION_ACK, session, 0,
                             _BODY[SESSION_ACK].pack(status, 0, expect)))
            continue

        if session is None or frame_session != session:
            continue
        last_activity = moment

        if kind == SESSION_ABORT:
            raise SessionError("peer aborted the session")

        if kind == CHUNK_START:
            index, raw_len, wire_len, chunk_crc, flags = \
                _BODY[CHUNK_START].unpack(body[:13])
            count = sequence  # header sequence carries total packet count N
            if index < expect:
                if last_committed is not None and index == last_committed[0]:
                    radio.send(frame(CHUNK_ACK, session, last_committed[1],
                                     _BODY[CHUNK_ACK].pack(index, 0)))
                continue
            expected_raw = min(CHUNK_BYTES, file_size - index * CHUNK_BYTES)
            if index > expect or count > MAX_PACKETS \
                    or count != packets_for(wire_len) \
                    or wire_len > CHUNK_BYTES or raw_len != expected_raw \
                    or (flags & 1 and algo != ALGO_ZLIB):
                continue
            active = {"index": index, "raw_len": raw_len, "wire_len": wire_len,
                      "crc": chunk_crc, "flags": flags, "count": count,
                      "buffer": bytearray(wire_len),
                      "seen": [False] * count, "missing": count}
            last_data = moment
            continue

        if kind == CHUNK_DATA:
            if active is None:
                nack(expect, CRC_ERR, MAX_PACKETS, b"\xff" * BITMAP_BYTES)
                stats["nacks"] += 1
                continue
            if sequence >= active["count"]:
                continue
            length = min(BODY_BYTES,
                         active["wire_len"] - sequence * BODY_BYTES)
            if length <= 0 or len(body) < length:
                # A short body written into a longer slice would shift the
                # bytes already assembled; wait for a well-formed copy.
                continue
            offset = sequence * BODY_BYTES
            if not active["seen"][sequence]:
                active["buffer"][offset:offset + length] = body[:length]
                active["seen"][sequence] = True
                active["missing"] -= 1
            last_data = moment
            if active["missing"]:
                continue
            wire = bytes(active["buffer"])
            index = active["index"]
            count = active["count"]
            if crc32(wire) != active["crc"]:
                active = None
                nack(index, CRC_ERR, MAX_PACKETS, b"\xff" * BITMAP_BYTES)
                stats["nacks"] += 1
                continue
            try:
                raw_chunk = decompress_chunk(wire, active["flags"] & 1,
                                             active["raw_len"])
                commit(index, raw_chunk)
            except (SessionError, zlib.error, OSError):
                active = None
                nack(index, CRC_ERR, MAX_PACKETS, b"\xff" * BITMAP_BYTES)
                stats["nacks"] += 1
                continue
            active = None
            stats["chunks"] += 1
            last_committed = (index, count)
            expect = index + 1
            radio.send(frame(CHUNK_ACK, session, count,
                             _BODY[CHUNK_ACK].pack(index, 0)))
            continue

        if kind == SESSION_END:
            end_total, end_crc = _BODY[SESSION_END].unpack(body[:8])
            if end_total != total_chunks or expect < total_chunks:
                continue
            if end_crc != file_crc:
                radio.send(frame(SESSION_END_ACK, session, 0,
                                 _BODY[SESSION_END_ACK].pack(1)))
                raise SessionError(
                    "SESSION_END metadata disagrees with SESSION_INIT")
            ok = finalize(end_crc)
            radio.send(frame(SESSION_END_ACK, session, 0,
                             _BODY[SESSION_END_ACK].pack(0 if ok else 1)))
            if not ok:
                raise SessionError("whole-file CRC mismatch at finalize")
            stats["file_crc32"] = "{:08x}".format(end_crc)
            # If the first END_ACK was lost the sender retries END; keep
            # answering duplicates for its whole retry window instead of
            # leaving it reporting failure on a file we already published.
            linger = min(deadline, moment + END_LINGER)
            while now() < linger:
                raw = radio.receive(max(0.0, linger - now()))
                if raw is None:
                    break
                parsed = parse(raw)
                if parsed is None:
                    continue
                kind, frame_session, _, body = parsed
                if kind == SESSION_END and frame_session == session:
                    end_total, end_crc = _BODY[SESSION_END].unpack(body[:8])
                    if end_total == total_chunks and end_crc == file_crc:
                        radio.send(frame(SESSION_END_ACK, session, 0,
                                         _BODY[SESSION_END_ACK].pack(0)))
            return stats

    raise SessionError("CSTP-24 receive timeout; session not completed")


def _abort(radio: DuplexRadio, session: int, code: int) -> None:
    """Best-effort abort notice; never raises."""
    try:
        radio.send(frame(SESSION_ABORT, session, 0,
                         _BODY[SESSION_ABORT].pack(code)))
    except Exception:
        pass


def state_record(file_size: int, file_crc: int, next_chunk: int) -> bytes:
    """Pack a 20-byte .state checkpoint (spec section 7)."""
    body = STATE.pack(file_size, file_crc, next_chunk, 0)[:16]
    return body + struct.pack("<I", crc32(body))


def parse_state(data: bytes):
    """Validate and unpack a .state record; None on any corruption."""
    if len(data) != STATE.size:
        return None
    if crc32(data[:16]) != struct.unpack("<I", data[16:])[0]:
        return None
    file_size, file_crc, next_chunk, _ = STATE.unpack(data)
    return file_size, file_crc, next_chunk
