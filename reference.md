# Reference inspection (2026-10-30)

User-supplied source:
https://github.com/lxkalv/nrf24.mtp/blob/master/quick_mode.py
Inspected master commit: 2c7430227915ee9072f7db8747fe624203b0dd63.

Observed: imports nrf24 and pigpio, connects to pigpiod at import time, sets
CE=22, channel 76, 2 Mbps, minimum PA, 2-byte CRC, dynamic payloads, 4-byte
addresses TAN0/TAN1. These are reference values, NOT confirmed project wiring.
TX finds a .txt on USB under /media (fallback lorem.txt), sends native signed
int chunk count, then <=32-byte payloads; data retries have no application bound.
RX waits indefinitely for a header, times out on inactive data and can save a
partial received_file.txt. No sequence number, file hash or save receipt is
provided. Link ACK does not prove remote file persistence. No HTTP download.

No license file or license declaration was visible in the repository root at
inspection; README contains no licensing terms. Do not infer copying rights.
No upstream code was copied into this scaffold. Implementation here is original.
No open-source license has been chosen for this private project.

Host proposal differences: explicit local input, little-endian signed count,
bounded retries, no import-time hardware, strict complete reconstruction, empty
file support and 1 MiB bound. These require review; existing upstream receiver
behavior for empty files differs. No interoperability is claimed. The legacy
format cannot detect duplicate/reordered chunks or establish end-to-end integrity.
Adding sequence/hash framing would change the protocol and needs a separate
contract decision, not a silent fix.
