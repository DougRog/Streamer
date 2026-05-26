"""
SCTE-35 splice_insert injection over multicast UDP.

Sends 188-byte MPEG-TS packets containing a SCTE-35 splice_insert section
directly to the channel's multicast address.  splice_immediate_flag is always
set (no PTS reference needed) so the cue fires at the downstream device's next
opportunity.

Packet delivery is best-effort: three copies are sent 33 ms apart for
redundancy.  The caller is responsible for ensuring the channel's multicast
stream is already flowing before calling inject_splice_insert().
"""
import socket
import struct
import logging
import time

from config import MULTICAST_INTERFACE, SCTE35_PID

logger = logging.getLogger(__name__)

# Per-channel continuity counters keyed by channel_id.
_cc: dict = {}


# ------------------------------------------------------------------ #
#  CRC-32 (MPEG-2 polynomial 0x04C11DB7, MSB first)                 #
# ------------------------------------------------------------------ #

def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


# ------------------------------------------------------------------ #
#  SCTE-35 section builder                                           #
# ------------------------------------------------------------------ #

def _build_splice_insert(
    event_id: int,
    out_of_network: bool,
    duration_90k: int = 0,
) -> bytes:
    """Return a binary SCTE-35 splice_info_section (no TS wrapping)."""

    oon = 1 if out_of_network else 0
    duration_flag = 1 if duration_90k > 0 else 0

    # Flags byte: out_of_network | program_splice_flag=1 | duration_flag | splice_immediate_flag=1 | reserved=0xF
    flags_byte = (oon << 7) | (1 << 6) | (duration_flag << 5) | (1 << 4) | 0x0F

    # splice_insert body
    cmd = bytearray()
    cmd += struct.pack('>I', event_id & 0xFFFFFFFF)   # splice_event_id (32 bits)
    cmd += bytes([0x7F])                               # cancel_indicator=0, reserved=0x7F
    cmd += bytes([flags_byte])

    if duration_flag:
        # break_duration: auto_return=1 (1b) + reserved=0 (6b) + duration (33b) → 5 bytes
        hi = (duration_90k >> 32) & 0x01
        lo = duration_90k & 0xFFFFFFFF
        cmd += struct.pack('>BI', 0x80 | hi, lo)

    cmd += struct.pack('>HBB', 0, 0, 0)   # unique_program_id, avail_num, avails_expected

    cmd_bytes = bytes(cmd)
    cmd_len   = len(cmd_bytes)

    # Assemble body (everything after the 3-byte section header, minus CRC)
    body = bytearray()
    body += bytes([0x00])                               # protocol_version
    # encrypted_packet=0 (1b), encryption_algorithm=0 (6b), pts_adjustment bit32=0 (1b)
    body += bytes([0x00])
    body += struct.pack('>I', 0)                        # pts_adjustment bits 31-0
    body += bytes([0xFF])                               # cw_index
    # tier (12b = 0xFFF) + splice_command_length (12b) packed into 3 bytes
    body += bytes([0xFF, 0xF0 | (cmd_len >> 8), cmd_len & 0xFF])
    body += bytes([0x05])                               # splice_command_type = splice_insert
    body += cmd_bytes
    body += struct.pack('>H', 0)                        # descriptor_loop_length

    # section_length counts from the byte after section_length to end including CRC32
    section_length = len(body) + 4   # +4 for CRC32

    header = bytearray()
    header += bytes([0xFC])                             # table_id
    # section_syntax_indicator=0, private=0, reserved=11, section_length (12 bits)
    header += bytes([0x30 | (section_length >> 8), section_length & 0xFF])

    section_no_crc = bytes(header) + bytes(body)
    crc = _crc32_mpeg(section_no_crc)
    return section_no_crc + struct.pack('>I', crc)


# ------------------------------------------------------------------ #
#  MPEG-TS packet builder                                            #
# ------------------------------------------------------------------ #

def _wrap_in_mpegts(section: bytes, pid: int, cc: int) -> bytes:
    """Wrap a SCTE-35 section in a single 188-byte MPEG-TS packet."""
    assert len(section) <= 182, f'SCTE-35 section too large ({len(section)} bytes)'

    pkt = bytearray(188)
    pkt[0] = 0x47                          # sync byte
    pkt[1] = 0x40 | ((pid >> 8) & 0x1F)   # PUSI=1, transport_error=0, priority=0, pid[12:8]
    pkt[2] = pid & 0xFF                    # pid[7:0]
    pkt[3] = 0x10 | (cc & 0x0F)           # payload_only, continuity_counter

    pkt[4] = 0x00                          # pointer_field (section starts at next byte)
    pkt[5:5 + len(section)] = section
    for i in range(5 + len(section), 188):
        pkt[i] = 0xFF                      # stuffing
    return bytes(pkt)


# ------------------------------------------------------------------ #
#  Public injection function                                         #
# ------------------------------------------------------------------ #

def inject_splice_insert(
    channel,
    event_id: int,
    out_of_network: bool,
    duration_secs: float = 0.0,
    repeat: int = 3,
):
    """
    Build and multicast a SCTE-35 splice_insert for channel.

    Sends `repeat` copies spaced one video frame (~33 ms) apart for redundancy.
    channel must expose .id, .multicast_addr, and .multicast_port.
    """
    pid = SCTE35_PID
    duration_90k = int(duration_secs * 90000) if duration_secs > 0 else 0

    try:
        section = _build_splice_insert(event_id, out_of_network, duration_90k)
    except Exception:
        logger.exception('SCTE-35: failed to build splice_insert section')
        return

    dest = (channel.multicast_addr, int(channel.multicast_port))

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        if MULTICAST_INTERFACE:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(MULTICAST_INTERFACE),
            )
    except OSError:
        logger.exception('SCTE-35: failed to open UDP socket')
        return

    try:
        for _ in range(repeat):
            cc = _cc.get(channel.id, 0)
            pkt = _wrap_in_mpegts(section, pid, cc)
            _cc[channel.id] = (cc + 1) & 0x0F
            sock.sendto(pkt, dest)
            if repeat > 1:
                time.sleep(0.033)   # ~1 frame at 30 fps
        logger.info(
            'SCTE-35 splice_insert injected: ch=%d event_id=%d oon=%s dur=%.1fs → %s:%d',
            channel.id, event_id, out_of_network, duration_secs,
            channel.multicast_addr, channel.multicast_port,
        )
    except OSError:
        logger.exception('SCTE-35: sendto failed for %s:%d', *dest)
    finally:
        sock.close()
