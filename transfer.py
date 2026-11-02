"""Original byte protocols and use cases; all effects are injected ports.

encode/decode/transmit remain the legacy host-only count/chunk proposal.
The explicitly separate PT/1 protocol is used for the controlled radio demo.
"""
import struct
from typing import Callable, Iterable, Optional, Protocol

CHUNK_BYTES = 32
MAX_BYTES = 1024 * 1024


class TransferError(RuntimeError):
    """The transfer failed; delivery must not be reported as successful."""


class Radio(Protocol):
    def send(self, frame: bytes) -> bool:
        """Return a link ACK, not an application delivery receipt."""


class Indicator(Protocol):
    def set_active(self, active: bool) -> None:
        """Show transfer activity; pin configuration belongs to an adapter."""


def encode(content: bytes) -> tuple[bytes, ...]:
    if len(content) > MAX_BYTES:
        raise ValueError("file exceeds 1 MiB host-proposal limit")
    chunks = tuple(content[i:i + CHUNK_BYTES]
                   for i in range(0, len(content), CHUNK_BYTES))
    return (struct.pack("<i", len(chunks)),) + chunks


def decode(frames: Iterable[bytes]) -> bytes:
    iterator = iter(frames)
    header = next(iterator, b"")
    if len(header) != 4:
        raise TransferError("expected exactly four header bytes")
    count, = struct.unpack("<i", header)
    if count < 0 or count > (MAX_BYTES + 31) // 32:
        raise TransferError("invalid chunk count")
    chunks = []
    for index in range(count):
        chunk = next(iterator, b"")
        if not 1 <= len(chunk) <= CHUNK_BYTES:
            raise TransferError("missing or oversized chunk")
        if index < count - 1 and len(chunk) != CHUNK_BYTES:
            raise TransferError("short non-final chunk")
        chunks.append(chunk)
    if next(iterator, None) is not None:
        raise TransferError("unexpected extra frames")
    return b"".join(chunks)


def transmit(content: bytes, radio: Radio, led: Indicator,
             max_attempts: int = 3) -> int:
    """Bound attempts per frame, abort on failure, always clear activity."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    frames = encode(content)
    try:
        led.set_active(True)
        for frame in frames:
            for _ in range(max_attempts):
                if radio.send(frame):
                    break
            else:
                raise TransferError("link ACK budget exhausted")
    finally:
        led.set_active(False)
    return len(frames)  # link-level completion only


# PT/1: fixed 32-byte packets, network byte order, 16-byte header/body.
MAGIC = b"PT"
VERSION = 1
START, DATA, FINISH, ACK, SAVED = range(1, 6)
BODY_BYTES = 16
HEADER = struct.Struct("!2sBBQI")
RATE_CODES = {"250k": 1, "2m": 2}
ATTEMPTS = 3  # Initial attempt plus two repairs, per request.
REPLY_TIMEOUT = 1.0
START_TIMEOUT = 30.0
IDLE_TIMEOUT = 10.0
TOTAL_TIMEOUT = 300.0
RECEIPT_LINGER = 4.0


class DuplexRadio(Protocol):
    def send(self, frame: bytes) -> bool:
        """Bounded send; return link ACK only, never a saved-file receipt."""

    def receive(self, timeout: float) -> Optional[bytes]:
        """Return one packet or None within timeout seconds."""


def crc32(content: bytes) -> int:
    """CRC-32/ISO-HDLC, reflected polynomial; corruption check, not authentication.

    Pure implementation keeps the existing struct/typing-only core gate intact.
    """
    value = 0xffffffff
    for byte in content:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xedb88320 if value & 1 else 0)
    return value ^ 0xffffffff


def packet(kind: int, session: int, sequence: int, body: bytes = b"") -> bytes:
    if kind not in (START, DATA, FINISH, ACK, SAVED):
        raise ValueError("unknown PT/1 packet type")
    if not 0 < session < 2**64 or not 0 <= sequence < 2**32:
        raise ValueError("invalid session or sequence")
    if len(body) > BODY_BYTES:
        raise ValueError("PT/1 body exceeds 16 bytes")
    return HEADER.pack(MAGIC, VERSION, kind, session, sequence) + body.ljust(16, b"\0")


def unpack_packet(frame: bytes) -> tuple[int, int, int, bytes]:
    if len(frame) != 32:
        raise TransferError("PT/1 requires exactly 32 bytes")
    magic, version, kind, session, sequence = HEADER.unpack(frame[:16])
    if magic != MAGIC or version != VERSION or session == 0:
        raise TransferError("invalid PT/1 discriminator or session")
    if kind not in (START, DATA, FINISH, ACK, SAVED):
        raise TransferError("unknown PT/1 type")
    return kind, session, sequence, frame[16:]


def v1_frames(content: bytes, session: int, rate: str) -> Iterable[bytes]:
    if len(content) > MAX_BYTES or rate not in RATE_CODES:
        raise ValueError("PT/1 requires <=1 MiB and rate 250k or 2m")
    metadata = struct.pack("!IIB", len(content), crc32(content), RATE_CODES[rate])
    yield packet(START, session, 0, metadata)
    count = (len(content) + BODY_BYTES - 1) // BODY_BYTES
    for index in range(count):
        yield packet(DATA, session, index + 1,
                     content[index * BODY_BYTES:(index + 1) * BODY_BYTES])
    yield packet(FINISH, session, count + 1, metadata)


def expected_reply(request: bytes) -> bytes:
    kind, session, sequence, body = unpack_packet(request)
    if kind == FINISH:
        return packet(SAVED, session, sequence, body)
    if kind not in (START, DATA):
        raise ValueError("not a request")
    return packet(ACK, session, sequence, bytes([kind]))


class Receiver:
    """One bounded session, ordered acceptance and idempotent last-request reply."""

    def __init__(self, rate: str):
        if rate not in RATE_CODES:
            raise ValueError("rate must be 250k or 2m")
        self.rate = rate
        self.session = None
        self.metadata = b""
        self.length = 0
        self.checksum = 0
        self.content = bytearray()
        self.next_sequence = 1
        self.saved = False
        self.last_request = None
        self.last_reply = None

    def accept(self, frame: bytes, save: Callable[[bytes], None]) -> Optional[bytes]:
        try:
            kind, session, sequence, body = unpack_packet(frame)
        except TransferError:
            return None
        if frame == self.last_request:
            return self.last_reply
        if self.saved:
            return None
        if self.session is None:
            if kind != START or sequence != 0:
                return None
            length, checksum, rate = struct.unpack("!IIB", body[:9])
            if (length > MAX_BYTES or rate != RATE_CODES[self.rate]
                    or body[9:] != b"\0" * 7):
                return None
            self.session, self.metadata = session, body
            self.length, self.checksum = length, checksum
        elif session != self.session or sequence != self.next_sequence:
            return None
        elif kind == DATA:
            remaining = self.length - len(self.content)
            if remaining <= 0:
                return None
            count = min(BODY_BYTES, remaining)
            if body[count:] != b"\0" * (BODY_BYTES - count):
                raise TransferError("nonzero final padding")
            self.content.extend(body[:count])
            self.next_sequence += 1
        elif kind == FINISH:
            if (body != self.metadata or len(self.content) != self.length
                    or crc32(self.content) != self.checksum):
                raise TransferError("incomplete file or integrity mismatch")
            save(bytes(self.content))  # Must atomically publish before returning.
            self.saved = True
        else:
            return None
        self.last_request = frame
        self.last_reply = expected_reply(frame)
        return self.last_reply


def send_v1(content: bytes, session: int, rate: str, radio: DuplexRadio,
            now: Callable[[], float]) -> int:
    """Stop-and-wait; only a matching SAVED receipt establishes success."""
    deadline = now() + TOTAL_TIMEOUT
    sent = 0
    for request in v1_frames(content, session, rate):
        reply = expected_reply(request)
        accepted = False
        for _ in range(ATTEMPTS):
            if now() >= deadline:
                break
            radio.send(request)  # A lost link ACK can still leave a valid reply.
            until = min(deadline, now() + REPLY_TIMEOUT)
            while now() < until:
                received = radio.receive(max(0, until - now()))
                if now() >= until:
                    break
                if received == reply:
                    accepted = True
                    break
            if accepted:
                break
        if not accepted:
            raise TransferError("PT/1 retry/deadline exhausted; remote save unconfirmed")
        sent += 1
    return sent


def receive_v1(rate: str, radio: DuplexRadio, save: Callable[[bytes], None],
               now: Callable[[], float]) -> int:
    """Receive once; duplicates/noise cannot extend progress or total deadlines."""
    receiver = Receiver(rate)
    deadline = now() + TOTAL_TIMEOUT
    progress_deadline = min(deadline, now() + START_TIMEOUT)
    linger_deadline = None
    while True:
        until = linger_deadline if receiver.saved else min(deadline, progress_deadline)
        if now() >= until:
            if receiver.saved:
                return receiver.length
            raise TransferError("PT/1 receive timeout; no completed file")
        frame = radio.receive(max(0, until - now()))
        if frame is None or now() >= until:
            continue
        before = (receiver.session, receiver.next_sequence, receiver.saved)
        reply = receiver.accept(frame, save)
        after = (receiver.session, receiver.next_sequence, receiver.saved)
        if before != after:
            progress_deadline = now() + IDLE_TIMEOUT
        if receiver.saved and linger_deadline is None:
            linger_deadline = now() + RECEIPT_LINGER
        if reply is not None:
            radio.send(reply)
