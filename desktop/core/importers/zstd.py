"""A zstd decoder (RFC 8878) in pure Python: enough to read the compressed rollout files Codex leaves behind
(``rollout-*.jsonl.zst``; on the machine this was written on, 147 of 162 Codex sessions were compressed).

The standard library has no zstd before Python 3.14 and the application ships no compiled modules, so this decodes the slow
way. It streams: ``chunks`` yields the output block by block (128 KiB at most), so a reader that needs only the start of a
file stops early, and what is kept in memory is the window (the output a match may still refer back to) plus one block.

Not supported (``ZstdError``): dictionaries. Checksums are skipped, not verified.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

MAGIC = 0xFD2FB528
SKIPPABLE = 0x184D2A50
"""The magic numbers 0x184D2A50 to 0x184D2A5F start a frame to skip."""
MAX_BLOCK = 128 * 1024
MAX_WINDOW = 1 << 31

LL_BASE = tuple(range(16)) + (16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
LL_BITS = (0,) * 16 + (1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
ML_BASE = tuple(range(3, 35)) + (35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051, 4099, 8195, 16387, 32771, 65539)
ML_BITS = (0,) * 32 + (1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16)
LL_DEFAULT = (4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1, -1, -1, -1, -1)
ML_DEFAULT = (1, 4, 3) + (2,) * 6 + (1,) * 37 + (-1,) * 7
OF_DEFAULT = (1, 1, 1, 1, 1, 1, 2, 2, 2) + (1,) * 15 + (-1,) * 5


class ZstdError(ValueError):
    """The data is not zstd, or is damaged, or uses what this decoder does not do."""


# ---------------------------------------------------------------------------------------------- bits


class _Forward:
    """Bits read from the start of ``data`` up, the lowest bit of each byte first (FSE table descriptions)."""

    def __init__(self, data: bytes, start: int = 0) -> None:
        self.data = data
        self.bit = start * 8

    def peek(self, count: int) -> int:
        index = self.bit >> 3
        chunk = int.from_bytes(self.data[index : index + 5], "little")
        return (chunk >> (self.bit & 7)) & ((1 << count) - 1)

    def take(self, count: int) -> int:
        value = self.peek(count)
        self.bit += count
        return value

    def end(self) -> int:
        """The index of the first byte after the bits read."""
        return (self.bit + 7) >> 3


class _Backward:
    """Bits read from the end of ``data`` down, after the highest set bit of the last byte (which marks where they begin):
    how Huffman streams and the sequences of a block are written. Reading past the start gives zeros; ``left`` is then
    negative, which is how the decoders below know the stream is done."""

    def __init__(self, data: bytes) -> None:
        if not data or data[-1] == 0:
            raise ZstdError("a bit stream without its end mark")
        self.data = data
        self.left = len(data) * 8 - (9 - data[-1].bit_length())
        """Bits not yet read."""

    def take(self, count: int) -> int:
        if count == 0:
            return 0
        self.left -= count
        start = self.left
        if start < 0:
            # (past the start: the missing low bits are zeros)
            if start + count <= 0:
                return 0
            return (int.from_bytes(self.data[0:8], "little") & ((1 << (start + count)) - 1)) << -start
        index = start >> 3
        return (int.from_bytes(self.data[index : index + 8], "little") >> (start & 7)) & ((1 << count) - 1)


# ---------------------------------------------------------------------------------------------- FSE


def _read_counts(data: bytes, start: int, max_symbol: int, max_log: int) -> tuple[list[int], int, int]:
    """An FSE table description: (normalized counts, accuracy log, index of the byte after it)."""
    bits = _Forward(data, start)
    log = bits.take(4) + 5
    if log > max_log:
        raise ZstdError("an FSE table too large")
    remaining = (1 << log) + 1
    threshold = 1 << log
    width = log + 1
    counts: list[int] = []
    zero_before = False
    while remaining > 1 and len(counts) <= max_symbol:
        if zero_before:
            run = 0
            while bits.peek(2) == 3:
                run += 3
                bits.take(2)
            run += bits.take(2)
            counts.extend([0] * run)
            if len(counts) > max_symbol:
                break
        biggest = 2 * threshold - 1 - remaining
        low = bits.peek(width - 1)
        if low < biggest:
            count = low
            bits.take(width - 1)
        else:
            count = bits.peek(width)
            if count >= threshold:
                count -= biggest
            bits.take(width)
        count -= 1
        remaining -= -count if count < 0 else count
        counts.append(count)
        zero_before = count == 0
        while remaining < threshold:
            width -= 1
            threshold >>= 1
    if remaining != 1 or len(counts) > max_symbol + 1:
        raise ZstdError("a damaged FSE table")
    return counts, log, bits.end()


def _fse_table(counts: tuple[int, ...] | list[int], log: int) -> tuple[list[int], list[int], list[int]]:
    """(symbol, bits to read, baseline) of every state."""
    size = 1 << log
    symbols = [0] * size
    high = size - 1
    following = [0] * len(counts)
    for symbol, count in enumerate(counts):
        if count == -1:
            symbols[high] = symbol
            high -= 1
            following[symbol] = 1
        else:
            following[symbol] = count
    step = (size >> 1) + (size >> 3) + 3
    mask = size - 1
    position = 0
    for symbol, count in enumerate(counts):
        for _ in range(count):
            symbols[position] = symbol
            position = (position + step) & mask
            while position > high:
                position = (position + step) & mask
    if position != 0:
        raise ZstdError("a damaged FSE table")
    widths = [0] * size
    bases = [0] * size
    for state in range(size):
        symbol = symbols[state]
        next_state = following[symbol]
        following[symbol] += 1
        width = log - (next_state.bit_length() - 1)
        widths[state] = width
        bases[state] = (next_state << width) - size
    return symbols, widths, bases


_LL_TABLE = (_fse_table(LL_DEFAULT, 6), 6)
_ML_TABLE = (_fse_table(ML_DEFAULT, 6), 6)
_OF_TABLE = (_fse_table(OF_DEFAULT, 5), 5)


# ---------------------------------------------------------------------------------------------- Huffman


def _huffman_weights(data: bytes, start: int) -> tuple[list[int], int]:
    """The weights of a Huffman tree description: (weights of the symbols before the last, index after it)."""
    if start >= len(data):
        raise ZstdError("a Huffman tree past the end of the block")
    head = data[start]
    if head >= 128:
        count = head - 127
        size = (count + 1) // 2
        packed = data[start + 1 : start + 1 + size]
        if len(packed) < size:
            raise ZstdError("a Huffman tree past the end of the block")
        weights = []
        for byte in packed:
            weights.append(byte >> 4)
            weights.append(byte & 15)
        return weights[:count], start + 1 + size
    end = start + 1 + head
    if end > len(data) or head == 0:
        raise ZstdError("a damaged Huffman tree")
    block = data[start + 1 : end]
    counts, log, used = _read_counts(block, 0, 255, 6)
    symbols, widths, bases = _fse_table(counts, log)
    bits = _Backward(block[used:])
    one, two = bits.take(log), bits.take(log)
    weights = []
    while True:
        weights.append(symbols[one])
        one = bases[one] + bits.take(widths[one])
        if bits.left < 0:
            weights.append(symbols[two])
            break
        weights.append(symbols[two])
        two = bases[two] + bits.take(widths[two])
        if bits.left < 0:
            weights.append(symbols[one])
            break
        if len(weights) > 255:
            raise ZstdError("a damaged Huffman tree")
    return weights, end


def _huffman_table(weights: list[int]) -> tuple[list[int], int]:
    """A decoding table indexed by the next ``max_bits`` bits: entries are ``symbol | bits << 8``."""
    total = sum(1 << (w - 1) for w in weights if w > 0)
    if total == 0 or any(w > 11 for w in weights):
        raise ZstdError("a damaged Huffman tree")
    max_bits = total.bit_length()
    left = (1 << max_bits) - total
    if left & (left - 1):
        raise ZstdError("a damaged Huffman tree")
    weights = weights + [left.bit_length()]
    if max_bits > 11:
        raise ZstdError("a Huffman tree too deep")
    ranks = [0] * (max_bits + 2)
    for weight in weights:
        if weight:
            ranks[weight] += 1
    starts = [0] * (max_bits + 2)
    position = 0
    for weight in range(1, max_bits + 1):
        starts[weight] = position
        position += ranks[weight] << (weight - 1)
    table = [0] * (1 << max_bits)
    for symbol, weight in enumerate(weights):
        if not weight:
            continue
        length = 1 << (weight - 1)
        entry = symbol | ((max_bits + 1 - weight) << 8)
        begin = starts[weight]
        table[begin : begin + length] = [entry] * length
        starts[weight] += length
    return table, max_bits


def _huffman_stream(data: bytes, count: int, table: list[int], max_bits: int) -> bytes:
    if not data or data[-1] == 0:
        raise ZstdError("a Huffman stream without its end mark")
    # (read backwards: with the bytes reversed, the stream is an ordinary high-bit-first one after the end mark; four
    # symbols at a time, as four never take more than 44 bits)
    reverse = data[::-1] + bytes(8)
    have = reverse[0].bit_length() - 1
    acc = reverse[0] & ((1 << have) - 1)
    position = 1
    mask = (1 << max_bits) - 1
    out = bytearray(count)
    quads = count >> 2
    index = 0
    for _ in range(quads):
        if have < 44:
            acc = ((acc & ((1 << have) - 1)) << 48) | int.from_bytes(reverse[position : position + 6], "big")
            position += 6
            have += 48
        entry = table[(acc >> (have - max_bits)) & mask]
        out[index] = entry & 255
        have -= entry >> 8
        entry = table[(acc >> (have - max_bits)) & mask]
        out[index + 1] = entry & 255
        have -= entry >> 8
        entry = table[(acc >> (have - max_bits)) & mask]
        out[index + 2] = entry & 255
        have -= entry >> 8
        entry = table[(acc >> (have - max_bits)) & mask]
        out[index + 3] = entry & 255
        have -= entry >> 8
        index += 4
    for index in range(quads * 4, count):
        if have < max_bits:
            acc = ((acc & ((1 << have) - 1)) << 48) | int.from_bytes(reverse[position : position + 6], "big")
            position += 6
            have += 48
        entry = table[(acc >> (have - max_bits)) & mask]
        out[index] = entry & 255
        have -= entry >> 8
    # (every bit of the stream is used, no more: 8 bits a byte were loaded after the first, partial, one)
    if (position - 1) * 8 - have != (len(data) - 1) * 8:
        raise ZstdError("a damaged Huffman stream")
    return bytes(out)


# ---------------------------------------------------------------------------------------------- a frame


class _Frame:
    def __init__(self, window: int) -> None:
        self.window = window
        self.history = bytearray()
        self.huffman: tuple[list[int], int] | None = None
        self.tables: list[tuple[tuple[list[int], list[int], list[int]], int] | None] = [None, None, None]
        self.offsets = [1, 4, 8]

    def literals(self, block: bytes) -> tuple[bytes, int]:
        head = block[0]
        kind = head & 3
        form = (head >> 2) & 3
        if kind < 2:
            if form in (0, 2):
                size, used = head >> 3, 1
            elif form == 1:
                size, used = (head >> 4) | (block[1] << 4), 2
            else:
                size, used = (head >> 4) | (block[1] << 4) | (block[2] << 12), 3
            if kind == 0:
                data = block[used : used + size]
                if len(data) < size:
                    raise ZstdError("literals past the end of the block")
                return data, used + size
            if used >= len(block):
                raise ZstdError("literals past the end of the block")
            return bytes([block[used]]) * size, used + 1
        if form < 2:
            value = int.from_bytes(block[0:3], "little")
            size, compressed, used, streams = (value >> 4) & 0x3FF, (value >> 14) & 0x3FF, 3, 1 if form == 0 else 4
        elif form == 2:
            value = int.from_bytes(block[0:4], "little")
            size, compressed, used, streams = (value >> 4) & 0x3FFF, (value >> 18) & 0x3FFF, 4, 4
        else:
            value = int.from_bytes(block[0:5], "little")
            size, compressed, used, streams = (value >> 4) & 0x3FFFF, (value >> 22) & 0x3FFFF, 5, 4
        end = used + compressed
        if end > len(block):
            raise ZstdError("literals past the end of the block")
        position = used
        if kind == 2:
            weights, position = _huffman_weights(block, used)
            self.huffman = _huffman_table(weights)
        elif self.huffman is None:
            raise ZstdError("literals that reuse a Huffman tree there is none of")
        table, max_bits = self.huffman
        if streams == 1:
            return _huffman_stream(block[position:end], size, table, max_bits), end
        jumps = block[position : position + 6]
        if len(jumps) < 6:
            raise ZstdError("literals past the end of the block")
        one, two, three = (int.from_bytes(jumps[i : i + 2], "little") for i in (0, 2, 4))
        position += 6
        part = (size + 3) // 4
        bounds = [position, position + one, position + one + two, position + one + two + three, end]
        if bounds[3] > end:
            raise ZstdError("damaged literals")
        sizes = [part, part, part, size - 3 * part]
        if sizes[3] < 0:
            raise ZstdError("damaged literals")
        out = b"".join(_huffman_stream(block[bounds[i] : bounds[i + 1]], sizes[i], table, max_bits) for i in range(4))
        return out, end

    def _table(self, mode: int, which: int, block: bytes, position: int, default: tuple[tuple[list[int], list[int], list[int]], int], max_symbol: int, max_log: int) -> int:
        if mode == 0:
            self.tables[which] = default
        elif mode == 1:
            if position >= len(block) or block[position] > max_symbol:
                raise ZstdError("a damaged sequence table")
            self.tables[which] = (([block[position]], [0], [0]), 0)
            position += 1
        elif mode == 2:
            counts, log, position = _read_counts(block, position, max_symbol, max_log)
            self.tables[which] = (_fse_table(counts, log), log)
        elif self.tables[which] is None:
            raise ZstdError("sequences that reuse a table there is none of")
        return position

    def block(self, block: bytes) -> bytes:
        literals, position = self.literals(block)
        if position >= len(block):
            raise ZstdError("a block without its sequences")
        head = block[position]
        if head == 0:
            count, position = 0, position + 1
        elif head < 128:
            count, position = head, position + 1
        elif head < 255:
            count, position = ((head - 128) << 8) + block[position + 1], position + 2
        else:
            count, position = block[position + 1] + (block[position + 2] << 8) + 0x7F00, position + 3
        history = self.history
        start = len(history)
        if count == 0:
            history += literals
            return bytes(history[start:])
        modes = block[position]
        position += 1
        position = self._table(modes >> 6, 0, block, position, _LL_TABLE, 35, 9)
        position = self._table((modes >> 4) & 3, 1, block, position, _OF_TABLE, 31, 8)
        position = self._table((modes >> 2) & 3, 2, block, position, _ML_TABLE, 52, 9)
        (ll_symbols, ll_widths, ll_bases), ll_log = self.tables[0]  # type: ignore[misc]
        (of_symbols, of_widths, of_bases), of_log = self.tables[1]  # type: ignore[misc]
        (ml_symbols, ml_widths, ml_bases), ml_log = self.tables[2]  # type: ignore[misc]
        stream = block[position:]
        if not stream or stream[-1] == 0:
            raise ZstdError("a bit stream without its end mark")
        # (the stream is read from its end down; reversed, it is an ordinary high-bit-first stream after the end mark, read
        # here through an accumulator because this loop runs once per match)
        reverse = stream[::-1]
        size = len(reverse)
        have = reverse[0].bit_length() - 1
        acc = reverse[0] & ((1 << have) - 1)
        index = 1
        left = (size - 1) * 8 + have
        """Bits of the stream not read yet (negative: read past its start, which gives zeros)."""
        wanted = ll_log + of_log + ml_log
        if have < wanted:
            chunk = reverse[index : index + 7]
            index += len(chunk)
            acc = ((acc & ((1 << have) - 1)) << (8 * len(chunk))) | int.from_bytes(chunk, "big")
            have += 8 * len(chunk)
            if have < wanted:
                acc <<= wanted - have
                have = wanted
        have -= wanted
        left -= wanted
        states = acc >> have
        ml_state = states & ((1 << ml_log) - 1)
        of_state = (states >> ml_log) & ((1 << of_log) - 1)
        ll_state = (states >> (ml_log + of_log)) & ((1 << ll_log) - 1)
        offsets = self.offsets
        literal_count = len(literals)
        used = 0
        last = count - 1
        for number in range(count):
            of_code = of_symbols[of_state]
            ml_code = ml_symbols[ml_state]
            ll_code = ll_symbols[ll_state]
            ml_bits = ML_BITS[ml_code]
            ll_bits = LL_BITS[ll_code]
            wanted = of_code + ml_bits + ll_bits
            if have < wanted:
                chunk = reverse[index : index + 7]
                index += len(chunk)
                acc = ((acc & ((1 << have) - 1)) << (8 * len(chunk))) | int.from_bytes(chunk, "big")
                have += 8 * len(chunk)
                if have < wanted:
                    acc <<= wanted - have
                    have = wanted
            have -= wanted
            left -= wanted
            extra = (acc >> have) & ((1 << wanted) - 1)
            length = LL_BASE[ll_code] + (extra & ((1 << ll_bits) - 1))
            extra >>= ll_bits
            match = ML_BASE[ml_code] + (extra & ((1 << ml_bits) - 1))
            offset = (1 << of_code) + (extra >> ml_bits)
            if offset > 3:
                offset -= 3
                offsets[2] = offsets[1]
                offsets[1] = offsets[0]
                offsets[0] = offset
            else:
                which = offset - 1 if length else offset
                if which == 0:
                    offset = offsets[0]
                else:
                    offset = offsets[0] - 1 if which == 3 else offsets[which]
                    if offset <= 0:
                        raise ZstdError("a damaged sequence")
                    if which > 1:
                        offsets[2] = offsets[1]
                    offsets[1] = offsets[0]
                    offsets[0] = offset
            if length:
                if used + length > literal_count:
                    raise ZstdError("a sequence past the end of the literals")
                history += literals[used : used + length]
                used += length
            end = len(history)
            if offset > end:
                raise ZstdError("a match before the start of the data")
            begin = end - offset
            if match <= offset:
                history += history[begin : begin + match]
            else:
                pattern = bytes(history[begin:])
                history += (pattern * (match // offset + 1))[:match]
            if number != last:
                ll_width = ll_widths[ll_state]
                ml_width = ml_widths[ml_state]
                of_width = of_widths[of_state]
                wanted = ll_width + ml_width + of_width
                if have < wanted:
                    chunk = reverse[index : index + 7]
                    index += len(chunk)
                    acc = ((acc & ((1 << have) - 1)) << (8 * len(chunk))) | int.from_bytes(chunk, "big")
                    have += 8 * len(chunk)
                    if have < wanted:
                        acc <<= wanted - have
                        have = wanted
                have -= wanted
                left -= wanted
                extra = (acc >> have) & ((1 << wanted) - 1)
                of_state = of_bases[of_state] + (extra & ((1 << of_width) - 1))
                extra >>= of_width
                ml_state = ml_bases[ml_state] + (extra & ((1 << ml_width) - 1))
                ll_state = ll_bases[ll_state] + (extra >> ml_width)
        if left != 0:
            raise ZstdError("a damaged sequence stream")
        history += literals[used:]
        return bytes(history[start:])

    def trim(self) -> None:
        excess = len(self.history) - self.window
        if excess > MAX_BLOCK * 8:
            del self.history[:excess]


def _read(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ZstdError("the data ends in the middle of a frame")
    return data


def chunks(stream: BinaryIO) -> Iterator[bytes]:
    """The decompressed data of every frame in ``stream``, a block at a time (the first frame must be there)."""
    first = True
    while True:
        magic_bytes = stream.read(4)
        if not magic_bytes and not first:
            return
        if len(magic_bytes) != 4:
            raise ZstdError("not zstd data")
        magic = int.from_bytes(magic_bytes, "little")
        if magic & 0xFFFFFFF0 == SKIPPABLE:
            size = int.from_bytes(_read(stream, 4), "little")
            _read(stream, size)
            first = False
            continue
        if magic != MAGIC:
            raise ZstdError("not zstd data")
        first = False
        yield from _frame(stream)


def _frame(stream: BinaryIO) -> Iterator[bytes]:
    descriptor = _read(stream, 1)[0]
    size_flag = descriptor >> 6
    single = bool(descriptor & 0x20)
    checksum = bool(descriptor & 0x04)
    dictionary = descriptor & 3
    if descriptor & 0x08:
        raise ZstdError("a frame with a reserved bit set")
    window = 0
    if not single:
        byte = _read(stream, 1)[0]
        base = 1 << (10 + (byte >> 3))
        window = base + (base // 8) * (byte & 7)
    if dictionary:
        if any(_read(stream, (0, 1, 2, 4)[dictionary])):
            raise ZstdError("a frame that needs a dictionary")
    size_bytes = (1 if single else 0, 2, 4, 8)[size_flag]
    content = 0
    if size_bytes:
        content = int.from_bytes(_read(stream, size_bytes), "little") + (256 if size_bytes == 2 else 0)
    if single:
        window = content
    if window > MAX_WINDOW:
        raise ZstdError("a frame with a window too large")
    frame = _Frame(max(window, 1))
    while True:
        header = int.from_bytes(_read(stream, 3), "little")
        last = header & 1
        kind = (header >> 1) & 3
        size = header >> 3
        if kind == 0:
            data = _read(stream, size)
            frame.history += data
        elif kind == 1:
            data = _read(stream, 1) * size
            frame.history += data
        elif kind == 2:
            if size > MAX_BLOCK:
                raise ZstdError("a block too large")
            data = frame.block(_read(stream, size))
        else:
            raise ZstdError("a reserved block type")
        if data:
            yield data
        frame.trim()
        if last:
            break
    if checksum:
        _read(stream, 4)
