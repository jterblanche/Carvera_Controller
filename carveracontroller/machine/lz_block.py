"""Checks one QuickLZ block before the native decompressor is given it.

Compressed files (``.lz``, uploads to and downloads from the machine) are
made of blocks, each compressed separately by QuickLZ. The controller
decompresses them with pyquicklz, whose QuickLZ is built without
QLZ_MEMORY_SAFE, so it trusts every length and offset in the block: a
damaged block makes it read and write outside its buffers, and that can
crash the whole app. The machine's firmware is built with QLZ_MEMORY_SAFE.

check_block applies the same checks as QLZ_MEMORY_SAFE, in Python, by
walking the block exactly as QuickLZ 1.4.1 level 3 decompresses it (the
version and level pyquicklz 1.4.1 builds), without writing anything. A
block that passes is one the native decompressor only reads and writes
inside its own buffers. Garbage that passes is still caught afterwards by
the file's checksum.
"""

from __future__ import annotations

# From quicklz.c 1.4.1.
_MINOFFSET = 2
_UNCONDITIONAL_MATCHLEN = 6
_UNCOMPRESSED_END = 4
_CWORD_LEN = 4
_BITLUT = (4, 0, 1, 0, 2, 0, 1, 0, 3, 0, 1, 0, 2, 0, 1, 0)

# The first byte of a block is 01SSLLHC: S streaming buffer (0 here), L the
# compression level (3), H a 9-byte rather than 3-byte header, C compressed.
_FLAGS_MASK = 0xFC
_FLAGS_LEVEL_3 = 0x4C

# qlz_size_compressed and qlz_size_decompressed read 9 bytes whatever the
# header length, and QuickLZ never makes a shorter compressed block.
_MIN_BLOCK = 9


class DamagedBlock(ValueError):
    """The block is not one QuickLZ could have made."""


def check_block(block: bytes, max_size: int) -> None:
    """Raise DamagedBlock unless `block` is a whole QuickLZ level 3 block
    that decompresses to at most `max_size` bytes using only the bytes it
    holds and the output it has already produced."""
    n = len(block)
    if n < _MIN_BLOCK:
        raise DamagedBlock("block of %d bytes is shorter than a QuickLZ header" % n)
    flags = block[0]
    if flags & _FLAGS_MASK != _FLAGS_LEVEL_3:
        raise DamagedBlock("block header 0x%02x is not QuickLZ level 3" % flags)
    if flags & 2:
        header = 9
        compressed_size = int.from_bytes(block[1:5], "little")
        size = int.from_bytes(block[5:9], "little")
    else:
        header = 3
        compressed_size = block[1]
        size = block[2]
    if compressed_size != n:
        raise DamagedBlock("block header says %d bytes, block has %d" % (compressed_size, n))
    if size == 0 or size > max_size:
        raise DamagedBlock("block header says it decompresses to %d bytes" % size)
    if not flags & 1:
        # Stored, not compressed: the native call copies `size` bytes.
        if header + size != n:
            raise DamagedBlock("stored block of %d bytes holds %d" % (size, n - header))
        return
    _walk(block, header, size)


def _walk(block: bytes, src: int, size: int) -> None:
    """qlz_decompress_core for level 3, with QLZ_MEMORY_SAFE's checks, moving
    the read and write positions without copying anything. `src` is where
    the compressed data starts, `size` the decompressed size."""
    last_source_byte = len(block) - 1
    last_destination_byte = size - 1
    last_matchstart = last_destination_byte - _UNCONDITIONAL_MATCHLEN - _UNCOMPRESSED_END
    dst = 0
    cword_val = 1
    while True:
        if cword_val == 1:
            if src + _CWORD_LEN - 1 > last_source_byte:
                raise DamagedBlock("control word runs past the end of the block")
            cword_val = int.from_bytes(block[src : src + _CWORD_LEN], "little")
            src += _CWORD_LEN
        # Every step reads 4 bytes here, even one that uses fewer.
        if src + 3 > last_source_byte:
            raise DamagedBlock("data runs past the end of the block")
        fetch = int.from_bytes(block[src : src + 4], "little")
        if cword_val & 1:
            cword_val >>= 1
            if fetch & 3 == 0:
                offset = (fetch & 0xFF) >> 2
                matchlen = 3
                src += 1
            elif fetch & 2 == 0:
                offset = (fetch & 0xFFFF) >> 2
                matchlen = 3
                src += 2
            elif fetch & 1 == 0:
                offset = (fetch & 0xFFFF) >> 6
                matchlen = ((fetch >> 2) & 15) + 3
                src += 2
            elif fetch & 127 != 3:
                offset = (fetch >> 7) & 0x1FFFF
                matchlen = ((fetch >> 2) & 0x1F) + 2
                src += 3
            else:
                offset = fetch >> 15
                matchlen = ((fetch >> 7) & 255) + 3
                src += 4
            # The copy starts `offset` bytes back in the output: inside what
            # has been written, and far enough back for the native copy,
            # which moves 4 bytes at a time in steps of 3.
            if offset > dst or offset < _MINOFFSET + 1:
                raise DamagedBlock("match at %d refers %d bytes back" % (dst, offset))
            # No wraparound here, unlike the C check's unsigned cast.
            if matchlen > last_destination_byte - dst - _UNCOMPRESSED_END + 1:
                raise DamagedBlock("match of %d bytes at %d runs past the end of %d" % (matchlen, dst, size))
            dst += matchlen
        elif dst < last_matchstart:
            # Copies 4 bytes and keeps the literal ones among them.
            step = _BITLUT[cword_val & 0xF]
            dst += step
            src += step
            cword_val >>= step
        else:
            while dst <= last_destination_byte:
                if cword_val == 1:
                    src += _CWORD_LEN
                    cword_val = 1 << 31
                if src > last_source_byte:
                    raise DamagedBlock("data runs past the end of the block")
                dst += 1
                src += 1
                cword_val >>= 1
            return
