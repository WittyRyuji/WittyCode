"""``core.importers.zstd``: the pure-Python zstd decoder Codex's compressed rollouts are read with.

The frames are real zstd made by another implementation (``importer_support.ZSTD``, from Node's zlib): every block type,
both literal stream layouts, the table modes and repeated offsets turn up in them, so a decoder that gets any of it wrong
gives the wrong bytes."""

from __future__ import annotations

import hashlib
import io
import struct

import pytest

from desktop.core.importers import zstd
from desktop.core.importers.base import lines
from tests.importer_support import ZSTD, skippable, zstd_frame, zstd_sample


def decode(data: bytes) -> bytes:
    return b"".join(zstd.chunks(io.BytesIO(data)))


@pytest.mark.parametrize("name", sorted(ZSTD))
def test_every_frame_made_by_another_zstd_comes_back_byte_for_byte(name):
    digest, _ = ZSTD[name]
    assert hashlib.sha1(zstd_sample(name)).hexdigest() == digest, "the sample changed: run importer_support.regenerate_zstd"
    assert decode(zstd_frame(name)) == zstd_sample(name)


def test_the_output_comes_a_block_at_a_time_so_a_reader_can_stop_early():
    pieces = list(zstd.chunks(io.BytesIO(zstd_frame("words19"))))
    assert len(pieces) >= 2
    assert max(len(piece) for piece in pieces) <= zstd.MAX_BLOCK
    stream = zstd.chunks(io.BytesIO(zstd_frame("words19")))
    assert next(stream) == zstd_sample("words19")[: len(pieces[0])]


def test_frames_follow_each_other_and_a_skippable_frame_is_skipped():
    assert decode(zstd_frame("multi")) == zstd_sample("multi")
    assert decode(skippable(b"x" * 10) + zstd_frame("tiny")) == zstd_sample("tiny")


def test_the_checksum_at_the_end_of_a_frame_is_stepped_over():
    framed = zstd_frame("checksum")
    assert framed[4] & 0x04  # (the frame says it has one)
    assert decode(framed + zstd_frame("tiny")) == zstd_sample("checksum") + zstd_sample("tiny")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not zstd at all",
        b"\x28\xb5\x2f",
        struct.pack("<I", 0xFD2FB528),
    ],
)
def test_what_is_not_a_whole_zstd_frame_is_a_zstd_error(data):
    with pytest.raises(zstd.ZstdError):
        decode(data)


def test_a_frame_cut_short_is_an_error_not_a_hang():
    framed = zstd_frame("words19")
    for size in (5, 20, len(framed) // 2, len(framed) - 1):
        with pytest.raises(zstd.ZstdError):
            decode(framed[:size])


def test_damaged_frames_raise_zstd_errors_and_nothing_else():
    framed = bytearray(zstd_frame("words1"))
    failures = 0
    for position in range(8, len(framed), max(1, len(framed) // 120)):
        damaged = bytearray(framed)
        damaged[position] ^= 0x5A
        try:
            decode(bytes(damaged))
        except zstd.ZstdError:
            failures += 1
    assert failures > 10


def test_a_frame_that_needs_a_dictionary_is_refused():
    descriptor = 0x01  # (a one-byte dictionary id)
    framed = struct.pack("<I", 0xFD2FB528) + bytes([descriptor, 0x58, 7]) + b"\x01\x00\x00"
    with pytest.raises(zstd.ZstdError, match="dictionary"):
        decode(framed)


def test_a_reserved_bit_or_block_type_is_refused():
    with pytest.raises(zstd.ZstdError):
        decode(struct.pack("<I", 0xFD2FB528) + bytes([0x08, 0x58]) + b"\x01\x00\x00")
    with pytest.raises(zstd.ZstdError):
        decode(struct.pack("<I", 0xFD2FB528) + bytes([0x00, 0x58]) + (7 | (1 << 3)).to_bytes(3, "little"))


def test_raw_and_rle_blocks_written_by_hand_are_read():
    raw_block = ((5 << 3) | (0 << 1) | 0).to_bytes(3, "little") + b"hello"
    rle_block = ((4 << 3) | (1 << 1) | 1).to_bytes(3, "little") + b"!"
    framed = struct.pack("<I", 0xFD2FB528) + bytes([0x00, 0x00]) + raw_block + rle_block
    assert decode(framed) == b"hello!!!!"


def hand_frame(literals: bytes, weights: list[int]) -> bytes:
    """A zstd frame written by hand: one block of Huffman literals whose tree description is direct (the weights
    themselves, packed two a byte, high nibble first), no sequences. ``weights``: the stored weights; the last
    symbol's is implied. (Node's zstd FSE-compresses the weights of every fixture above, so this path is built here.)"""
    total = sum(1 << (weight - 1) for weight in weights if weight)
    max_bits = total.bit_length()
    full = [*weights, ((1 << max_bits) - total).bit_length()]
    codes: dict[int, tuple[int, int]] = {}
    position = 0
    for weight in range(1, max_bits + 1):
        for symbol, own in enumerate(full):
            if own == weight:
                codes[symbol] = (position >> (weight - 1), max_bits + 1 - weight)
                position += 1 << (weight - 1)
    bits = "".join(f"{codes[symbol][0]:0{codes[symbol][1]}b}" for symbol in literals)
    stream = bytearray(len(bits) // 8 + 1)
    for index, bit in enumerate(bits):
        at = len(bits) - 1 - index  # (the stream is read from its end: the first symbol's bits sit highest)
        if bit == "1":
            stream[at // 8] |= 1 << (at % 8)
    stream[len(bits) // 8] |= 1 << (len(bits) % 8)  # (the end mark)
    tree = bytes([127 + len(weights)]) + bytes(
        (weights[i] << 4) | (weights[i + 1] if i + 1 < len(weights) else 0) for i in range(0, len(weights), 2)
    )
    packed = tree + bytes(stream)
    head = (2 | (len(literals) << 4) | (len(packed) << 14)).to_bytes(3, "little")
    block = head + packed + b"\x00"  # (a sequences section that says: none)
    return struct.pack("<I", 0xFD2FB528) + bytes([0x20, len(literals)]) + ((len(block) << 3) | (2 << 1) | 1).to_bytes(3, "little") + block


def test_a_direct_tree_description_packs_two_weights_a_byte_high_first():
    literals = bytes([3, 0, 3, 1, 2, 0, 2, 3, 3, 1, 0, 2, 3, 3, 3, 0, 1, 0, 2, 3] * 2)
    framed = hand_frame(literals, [2, 1, 1])
    assert framed[12] == 127 + 3  # (three stored weights, written directly)
    assert decode(framed) == literals


def test_a_huffman_stream_off_by_a_bit_is_damaged_not_wrong():
    framed = bytearray(zstd_frame("words1"))
    framed[224] ^= 0x80  # (inside the first Huffman literals stream: now its bits no longer add up exactly)
    with pytest.raises(zstd.ZstdError, match="damaged Huffman stream"):
        decode(bytes(framed))


def test_the_history_is_trimmed_to_the_window_only_when_far_past_it():
    frame = zstd._Frame(1024)
    frame.history = bytearray(1024 + zstd.MAX_BLOCK * 8 + 10)
    frame.trim()
    assert len(frame.history) == 1024, "what is kept is the window: a match may still reach that far back"
    frame = zstd._Frame(1024)
    frame.history = bytearray(1024 + zstd.MAX_BLOCK * 8)
    frame.trim()
    assert len(frame.history) == 1024 + zstd.MAX_BLOCK * 8


def test_lines_of_a_compressed_file_are_the_lines_of_the_plain_one(tmp_path):
    plain = tmp_path / "rollout.jsonl"
    plain.write_bytes(zstd_sample("rollout"))
    packed = tmp_path / "rollout.jsonl.zst"
    packed.write_bytes(zstd_frame("rollout"))
    assert list(lines(packed)) == list(lines(plain))
    assert len(list(lines(packed))) == zstd_sample("rollout").count(b"\n")
    head = list(lines(packed, limit=300))
    assert head == list(lines(plain, limit=300)) and 0 < len(head) < len(list(lines(plain)))


def test_a_last_line_without_a_newline_is_still_a_line(tmp_path):
    packed = tmp_path / "x.jsonl.zst"
    packed.write_bytes(zstd_frame("tiny"))
    assert list(lines(packed)) == [b"hello hello hello hello"]
