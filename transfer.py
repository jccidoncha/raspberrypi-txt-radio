"""Original host-only proposal for count + 32-byte quick-mode framing."""
import struct
from typing import Iterable, Protocol

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
