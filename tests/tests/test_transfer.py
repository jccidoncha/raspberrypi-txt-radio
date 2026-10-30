import unittest
from transfer import MAX_BYTES, TransferError, decode, encode, transmit


class FakeLed:
    def __init__(self):
        self.states = []

    def set_active(self, active):
        self.states.append(active)


class FakeRadio:
    def __init__(self, result=True):
        self.result = result
        self.frames = []

    def send(self, frame):
        self.frames.append(frame)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class TransferTests(unittest.TestCase):
    def test_roundtrip_boundaries(self):
        for length in (0, 1, 31, 32, 33, 64, 65, MAX_BYTES):
            with self.subTest(length=length):
                content = bytes(i % 256 for i in range(length))
                self.assertEqual(decode(encode(content)), content)

    def test_header_vector(self):
        self.assertEqual(encode(b"a" * 33)[0], b"\x02\x00\x00\x00")

    def test_oversize(self):
        with self.assertRaises(ValueError):
            encode(b"a" * (MAX_BYTES + 1))

    def test_bad_frames(self):
        bad = [[], [b""], [b"\xff" * 4], [b"\x01\x00\x00\x00"],
               [b"\x01\x00\x00\x00", b"x" * 33],
               [b"\x02\x00\x00\x00", b"x", b"y"],
               [b"\x00" * 4, b"extra"]]
        for frames in bad:
            with self.subTest(frames=frames), self.assertRaises(TransferError):
                decode(frames)

    def test_link_success_and_led(self):
        radio, led = FakeRadio(), FakeLed()
        self.assertEqual(transmit(b"hello", radio, led), 2)
        self.assertEqual(led.states, [True, False])
        self.assertEqual(decode(radio.frames), b"hello")

    def test_retry_budget_and_header_failure(self):
        radio, led = FakeRadio(False), FakeLed()
        with self.assertRaises(TransferError):
            transmit(b"hello", radio, led, 2)
        self.assertEqual(len(radio.frames), 2)
        self.assertEqual(led.states, [True, False])

    def test_exception_clears_led(self):
        led = FakeLed()
        with self.assertRaises(OSError):
            transmit(b"hello", FakeRadio(OSError("offline")), led)
        self.assertEqual(led.states, [True, False])

    def test_invalid_budget_no_effect(self):
        radio, led = FakeRadio(), FakeLed()
        with self.assertRaises(ValueError):
            transmit(b"hello", radio, led, 0)
        self.assertEqual(led.states, [])
        self.assertEqual(radio.frames, [])


if __name__ == "__main__":
    unittest.main()
