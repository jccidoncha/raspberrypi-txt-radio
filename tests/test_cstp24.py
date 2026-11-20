"""CSTP-24 host tests: wire vectors, chunk pipeline, loopback sessions, resume."""
import os
import queue
import struct
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import cstp24 as c
import quick_mode


def init_frame(session=7, algo=c.ALGO_ZLIB, size=6, chunks=1, crc=0x12345678):
    return c.init_frame(session, algo, size, chunks, crc)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ScriptedRadio:
    """Deterministic half-duplex fake: preloaded inbound frames, clock on receive."""

    def __init__(self, incoming, clock):
        self.incoming = deque(incoming)
        self.clock = clock
        self.sent = []

    def send(self, frame):
        self.sent.append(frame)
        return True

    def receive(self, timeout):
        self.clock.t += timeout
        return self.incoming.popleft() if self.incoming else None


class WireEnd:
    """Thread-safe loopback endpoint; drop/corrupt hooks model the air link."""

    def __init__(self, transform=None):
        self.q = queue.Queue()
        self.peer = None
        self.transform = transform or (lambda frame: frame)

    def send(self, frame):
        forwarded = self.transform(frame)
        if forwarded is not None:
            self.peer.q.put(forwarded)
        return True

    def receive(self, timeout):
        try:
            return self.q.get(timeout=max(0.001, timeout))
        except queue.Empty:
            return None


def wire_pair(tx_transform=None, rx_transform=None):
    a, b = WireEnd(tx_transform), WireEnd(rx_transform)
    a.peer, b.peer = b, a
    return a, b


def run_session(content, rx_transform=None, tx_transform=None, resume_store=None):
    """Drive one sender and one receiver over a loopback; return (tx, rx, box)."""
    tx_radio, rx_radio = wire_pair(tx_transform, rx_transform)
    box = {"committed": {}, "final": None, "error": None, "store": resume_store
           if resume_store is not None else {}}

    def lookup(file_size, file_crc, total_chunks):
        k = box["store"].get((file_size, file_crc), 0)
        for i in range(k):  # chunks already durable in .part from the prior run
            box["committed"][i] = content[i * c.CHUNK_BYTES:(i + 1) * c.CHUNK_BYTES]
        return k

    def commit(index, raw):
        box.setdefault("commit_calls", []).append(index)
        box["committed"][index] = raw

    def finalize(file_crc):
        data = b"".join(box["committed"][i] for i in sorted(box["committed"]))
        if c.crc32(data) != file_crc:
            return False
        box["final"] = data
        return True

    def receive():
        try:
            box["rx_stats"] = c.receive_session(rx_radio, __import__("time").monotonic,
                                                lookup, commit, finalize)
        except Exception as error:  # surfaced in the assertion, never swallowed
            box["error"] = error

    with mock.patch.object(c, "T_ACK_WAIT", 0.1), \
            mock.patch.object(c, "T_GAP", 0.05), \
            mock.patch.object(c, "START_TIMEOUT", 5.0):
        worker = threading.Thread(target=receive)
        worker.start()
        total = (len(content) + c.CHUNK_BYTES - 1) // c.CHUNK_BYTES
        stats = c.send_session(
            lambda i: content[i * c.CHUNK_BYTES:(i + 1) * c.CHUNK_BYTES],
            total, len(content), c.crc32(content), 7, tx_radio,
            __import__("time").monotonic)
        worker.join(timeout=20)
    return stats, box


class FrameTests(unittest.TestCase):
    def test_init_wire_vector(self):
        raw = init_frame(session=0x0102, algo=c.ALGO_ZLIB, size=0x0102030405060708,
                         chunks=3, crc=0xA1B2C3D4)
        self.assertEqual(len(raw), 25)
        self.assertEqual(raw[0], c.SESSION_INIT)
        self.assertEqual(struct.unpack("<H", raw[1:3])[0], 0x0102)
        self.assertEqual(struct.unpack("<H", raw[3:5])[0], 0)
        body = raw[5:]
        self.assertEqual(body[0], c.VERSION)
        self.assertEqual(body[1], c.ALGO_ZLIB)
        self.assertEqual(struct.unpack("<H", body[2:4])[0], 4096)
        self.assertEqual(struct.unpack("<Q", body[4:12])[0], 0x0102030405060708)
        self.assertEqual(struct.unpack("<I", body[12:16])[0], 3)
        self.assertEqual(struct.unpack("<I", body[16:20])[0], 0xA1B2C3D4)

    def test_frame_bounds_and_parse(self):
        self.assertEqual(len(c.frame(c.CHUNK_NACK, 1, 0, b"\0" * 24)), 29)
        self.assertEqual(len(c.frame(c.CHUNK_DATA, 1, 0, b"\0" * 27)), 32)
        with self.assertRaises(ValueError):
            c.frame(c.CHUNK_DATA, 0, 0)
        with self.assertRaises(ValueError):
            c.frame(c.CHUNK_DATA, 1, 0, b"\0" * 28)
        padded = c.frame(c.SESSION_END_ACK, 9, 0, b"\0").ljust(32, b"\0")
        kind, session, _, body = c.parse(padded)
        self.assertEqual((kind, session), (c.SESSION_END_ACK, 9))
        self.assertIsNone(c.parse(b"\0" * 32))
        self.assertIsNone(c.parse(b"\x99" + b"\0" * 31))
        self.assertIsNone(c.parse(b"\x01\x00\x00\x00\x00" + b"\0" * 20))

    def test_compress_guard_and_roundtrip(self):
        random_block = os.urandom(c.CHUNK_BYTES)
        wire, flag = c.compress_chunk(random_block)
        self.assertEqual((wire, flag), (random_block, 0))
        text_block = b"the same line again\n" * 200
        wire, flag = c.compress_chunk(text_block)
        self.assertEqual(flag, 1)
        self.assertLess(len(wire), len(text_block))
        self.assertEqual(c.decompress_chunk(wire, flag, len(text_block)), text_block)
        with self.assertRaises(c.SessionError):
            c.decompress_chunk(wire, flag, len(text_block) + 1)
        with self.assertRaises(ValueError):
            c.compress_chunk(b"x" * (c.CHUNK_BYTES + 1))

    def test_state_record_roundtrip_and_corruption(self):
        record = c.state_record(1234, 0xDEADBEEF, 5)
        self.assertEqual(len(record), 20)
        self.assertEqual(c.parse_state(record), (1234, 0xDEADBEEF, 5))
        broken = bytearray(record)
        broken[3] ^= 0xFF
        self.assertIsNone(c.parse_state(bytes(broken)))
        self.assertIsNone(c.parse_state(record[:19]))


class LoopbackTests(unittest.TestCase):
    def test_small_file_single_chunk(self):
        stats, box = run_session(b"hello\n")
        self.assertIsNone(box["error"])
        self.assertEqual(box["final"], b"hello\n")
        self.assertEqual(stats["chunks"], 1)
        self.assertEqual(stats["resumed_from"], 0)

    def test_multichunk_compressed(self):
        content = (b"line %04d of the report\n" % 1) * 500
        stats, box = run_session(content)
        self.assertIsNone(box["error"])
        self.assertEqual(box["final"], content)
        self.assertEqual(stats["chunks"], 3)

    def test_selective_nack_retransmits_only_missing(self):
        dropped = {"done": False}
        seen = []

        def transform(frame):
            parsed = c.parse(frame)
            if parsed and parsed[0] == c.CHUNK_DATA:
                seen.append(parsed[2])
                if parsed[2] == 3 and not dropped["done"]:
                    dropped["done"] = True
                    return None
            return frame

        content = os.urandom(3000)  # one chunk: every seq seen here belongs to it
        stats, box = run_session(content, tx_transform=transform)
        self.assertIsNone(box["error"])
        self.assertEqual(box["final"], content)
        self.assertEqual(seen.count(3), 2)  # exactly one selective retransmit
        self.assertGreaterEqual(stats["nacks"], 1)

    def test_crc_corruption_triggers_full_chunk_retry(self):
        corrupted = {"done": False}

        def transform(frame):
            parsed = c.parse(frame)
            if parsed and parsed[0] == c.CHUNK_DATA and parsed[2] == 0 \
                    and not corrupted["done"]:
                corrupted["done"] = True
                mutated = bytearray(frame)
                mutated[10] ^= 0xFF
                return bytes(mutated)
            return frame

        content = os.urandom(5000)
        stats, box = run_session(content, tx_transform=transform)
        self.assertIsNone(box["error"])
        self.assertEqual(box["final"], content)
        self.assertGreaterEqual(stats["chunk_retries"], 1)

    def test_resume_skips_committed_chunks(self):
        content = b"resumable content\n" * 400  # 7200 bytes = 2 chunks
        store = {(len(content), c.crc32(content)): 1}
        stats, box = run_session(content, resume_store=store)
        self.assertIsNone(box["error"])
        self.assertEqual(stats["resumed_from"], 1)
        self.assertEqual(box["commit_calls"], [1])  # chunk 0 never re-committed
        self.assertEqual(box["final"], content)


class ScriptedReceiverTests(unittest.TestCase):
    def receive(self, frames):
        clock = FakeClock()
        radio = ScriptedRadio(frames, clock)
        box = {}
        with self.assertRaises(c.SessionError):
            c.receive_session(radio, clock,
                              lambda s, crc, total: box.setdefault("lookup", (s, crc, total)) and 0,
                              lambda i, raw: None, lambda crc: True)
        return radio

    def test_bad_algo_rejected(self):
        radio = self.receive([init_frame(algo=c.ALGO_LZ4)])
        kind, session, _, body = c.parse(radio.sent[0])
        self.assertEqual((kind, session), (c.SESSION_ACK, 7))
        status, _, _ = c._BODY[c.SESSION_ACK].unpack(body[:7])
        self.assertEqual(status, c.BAD_ALGO)

    def test_abort_after_init_raises(self):
        radio = self.receive([
            init_frame(),
            c.frame(c.SESSION_ABORT, 7, 0, bytes([c.ABORT_CANCEL])),
        ])
        kind, _, _, body = c.parse(radio.sent[0])
        self.assertEqual(kind, c.SESSION_ACK)
        status, _, resume = c._BODY[c.SESSION_ACK].unpack(body[:7])
        self.assertEqual((status, resume), (c.OK_NEW, 0))

    def test_duplicate_start_reacks_without_duplicate_commit(self):
        clock = FakeClock()
        content = b"x" * 100
        wire, flag = c.compress_chunk(content)
        count = c.packets_for(len(wire))
        start = c.frame(c.CHUNK_START, 7, count,
                        c._BODY[c.CHUNK_START].pack(0, len(content), len(wire),
                                                    c.crc32(wire), flag))
        data = [c.frame(c.CHUNK_DATA, 7, s,
                        wire[s * 27:(s + 1) * 27]) for s in range(count)]
        abort = c.frame(c.SESSION_ABORT, 7, 0, bytes([c.ABORT_CANCEL]))
        radio = ScriptedRadio([init_frame(), start] + data + [start, abort], clock)
        commits = []
        with self.assertRaises(c.SessionError):
            c.receive_session(radio, clock, lambda s, crc, total: 0,
                              lambda i, raw: commits.append((i, raw)),
                              lambda crc: True)
        self.assertEqual(commits, [(0, content)])  # committed exactly once
        acks = [f for f in radio.sent
                if c.parse(f) and c.parse(f)[0] == c.CHUNK_ACK]
        self.assertEqual(len(acks), 2)  # duplicate START re-acked, no rewrite

    def test_data_without_start_draws_crc_nack(self):
        clock = FakeClock()
        stray = c.frame(c.CHUNK_DATA, 7, 0, b"\0" * 27)
        radio = ScriptedRadio([init_frame(), stray,
                               c.frame(c.SESSION_ABORT, 7, 0, b"\x04")], clock)
        with self.assertRaises(c.SessionError):
            c.receive_session(radio, clock, lambda s, crc, total: 0,
                              lambda i, raw: None, lambda crc: True)
        nacks = [f for f in radio.sent
                 if c.parse(f) and c.parse(f)[0] == c.CHUNK_NACK]
        self.assertEqual(len(nacks), 1)
        _, _, _, body = c.parse(nacks[0])
        _, reason, bitmap = c._BODY[c.CHUNK_NACK].unpack(body[:24])
        self.assertEqual(reason, c.CRC_ERR)
        self.assertEqual(bitmap, b"\xff" * 19)


class ReceiverFileTests(unittest.TestCase):
    def test_checkpoint_files_resume_and_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out.txt"
            with quick_mode.CompletedFile(target) as output:
                files = quick_mode.CstpReceiverFiles(output)
                content = b"checkpoint me\n" * 400  # 3 chunks
                total = 3
                crc = c.crc32(content)
                self.assertEqual(files.lookup(len(content), crc, total), 0)
                files.commit(0, content[:c.CHUNK_BYTES])
                # Simulate a crash: new instance, same paths, mid-transfer.
                again = quick_mode.CstpReceiverFiles(output)
                self.assertEqual(again.lookup(len(content), crc, total), 1)
                # A changed source file invalidates the checkpoint.
                self.assertEqual(again.lookup(len(content), crc ^ 1, total), 0)
                self.assertFalse(files.part.exists())
                # Full commit then finalize publishes and cleans up.
                files.commit(0, content[:c.CHUNK_BYTES])
                files.commit(1, content[c.CHUNK_BYTES:2 * c.CHUNK_BYTES])
                files.commit(2, content[2 * c.CHUNK_BYTES:])
                self.assertTrue(files.finalize(crc))
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_cli_rx_cstp24_timeout_leaves_no_files(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "out.txt"
            manager = unittest.mock.MagicMock()
            manager.__enter__.return_value = WireEnd()
            with mock.patch("radio_adapter.open_radio", return_value=manager), \
                    mock.patch.object(c, "START_TIMEOUT", 0.2), \
                    mock.patch("sys.stderr", new_callable=__import__("io").StringIO), \
                    mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
                self.assertEqual(quick_mode.main(
                    ["rx", str(target), "--rate", "250k",
                     "--hardware-checked"]), 1)
            self.assertEqual(list(Path(folder).iterdir()), [])
            manager.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
