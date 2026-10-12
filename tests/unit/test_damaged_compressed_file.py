"""A damaged compressed (.lz) file must never crash the controller.

The native QuickLZ decompressor (pyquicklz) trusts every length and offset in
a block, so a damaged block makes it read or write outside its buffers. The
damaged file below crashed the controller once in 11 previews: its first
block asks for bytes 56,542 bytes before the start of its output, wherever
that lands in memory. Changing only the size its header claims, or crafting a
block that writes past its output, makes the same call crash every time, so
those are what the tests feed it. Anything that might crash runs in a child
process, so the test runner survives it.
"""

from __future__ import annotations

import os
import random
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import quicklz

import carveracontroller.main as main_module
from carveracontroller.machine.lz_block import DamagedBlock, check_block
from carveracontroller.main import Makera

REPO_ROOT = Path(__file__).resolve().parents[2]
REPEATS = 20

# The damaged file from the 12 Oct 2026 test session (corrupt-test.lz, 393
# bytes): one block of 387 bytes that decompresses to 488, then the checksum.
DAMAGED_FILE = bytes.fromhex(
    "000001834f83010000e80100000000007fc4dfbe968dd28c61666520746573742066696c6520666f7220636f6e6e6563"
    "0000008074696f6e2d666f6c6c6f77732d6d652070726f63656475726573202853552d40402080352e372c204d060231"
    "292e0a3b205321012d5a206d6f76846f6e6c792c206e6f2000001092746f6f6c2072657175697265642c207370696e64"
    "990173746179986666860e426501000080d50165206c6f6164696e673a207365742074686520776f726b2058595a207a"
    "658011b8a1726f2077697468ca0516116e6f7390656c6c206162d5012501860762c6190e316669726d0a0563c00100c0"
    "757272656e744e14d50185012773205830205930205a313020697320696e7369642104a01c01c0726176656cca3365a6"
    "3c2e6d5901563dc648686f7771032067656e657261746520612073a104000000802d66657463682076617269616e742e"
    "0a4732310a4739300a473137204739343ce001800a470a1cc6014a1f60205a3820463530ce022d021a03839f0b003020"
    "5a31300a4d320a9278"
)
DAMAGED_BLOCK = DAMAGED_FILE[4:-2]


def _claiming_size(block, size):
    """`block` with its header's decompressed size changed to `size`."""
    return block[:5] + struct.pack("<I", size) + block[9:]


def _overflowing_block(size):
    """A level 3 block for `size` bytes: four literal bytes, then 27 matches
    of 258 bytes each, far more than `size` holds."""
    cword = (0x7FFFFFF << 4) | (1 << 31)  # 4 literals, then matches
    match = struct.pack("<I", (3 << 15) | ((258 - 3) << 7) | 3)  # offset 3, length 258
    body = struct.pack("<I", cword) + b"G1 X" + match * 27
    return bytes([0x4F]) + struct.pack("<I", 9 + len(body)) + struct.pack("<I", size) + body


# Blocks the native decompressor crashes on every time (SIGSEGV), in a fresh
# process on Linux: the damaged stream reading far before an output buffer
# of its own mapping, and a block writing past its output.
CRASHING_BLOCKS = {
    "damaged block claiming 1 MB": _claiming_size(DAMAGED_BLOCK, 1 << 20),
    "block writing past its 1 kB output": _overflowing_block(1024),
}


def _lz_file(block):
    """One block in the controller's file format, with a checksum."""
    return struct.pack(">I", len(block)) + block + b"\x00\x00"


DAMAGED_FILES = {
    "damaged file as found": DAMAGED_FILE,
    **{name: _lz_file(block) for name, block in CRASHING_BLOCKS.items()},
}


def _run_child(script, *args):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=300,
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="what crashes depends on the C library")
@pytest.mark.parametrize("name", sorted(CRASHING_BLOCKS))
def test_the_native_decompressor_crashes_on_a_damaged_block(tmp_path, name):
    """Why the check exists: given these blocks, quicklz.decompress takes the
    whole process down, every time."""
    path = tmp_path / "block.bin"
    path.write_bytes(CRASHING_BLOCKS[name])

    result = _run_child(
        """
        import sys
        import quicklz
        quicklz.decompress(open(sys.argv[1], "rb").read())
        print("survived")
        """,
        str(path),
    )

    # Killed by a signal: at once, or on the way out, from the memory the
    # call wrote over.
    assert result.returncode < 0


def test_decompress_file_survives_damaged_files_again_and_again(tmp_path):
    paths = []
    for index, contents in enumerate(DAMAGED_FILES.values()):
        path = tmp_path / ("damaged-%d.lz" % index)
        path.write_bytes(contents)
        paths.append(str(path))

    result = _run_child(
        """
        import os
        import sys
        import carveracontroller.main as main_module
        from carveracontroller.main import Makera

        main_module.load_constants()
        root = Makera.__new__(Makera)
        outcomes = set()
        for path in sys.argv[2:]:
            for _ in range(int(sys.argv[1])):
                output = path + ".out"
                outcomes.add((root.decompress_file(path, output), os.path.exists(output)))
        print("outcomes", sorted(outcomes))
        """,
        str(REPEATS),
        *paths,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "outcomes [(False, False)]" in result.stdout


@pytest.mark.parametrize("name", sorted(DAMAGED_FILES))
def test_a_damaged_block_never_reaches_the_native_decompressor(monkeypatch, tmp_path, name):
    main_module.load_constants()
    passed_on = []
    monkeypatch.setattr(main_module.quicklz, "decompress", passed_on.append)
    path = tmp_path / "damaged.lz"
    path.write_bytes(DAMAGED_FILES[name])
    output = tmp_path / "out.nc"

    assert Makera.__new__(Makera).decompress_file(str(path), str(output)) is False

    assert passed_on == []
    assert not output.exists()


@pytest.mark.parametrize("contents", [b"\x00\x00\x00\x00\x00\x09", b"\x00\x00\x10\x00G1"])
def test_a_block_running_past_the_end_of_the_file_is_refused(monkeypatch, tmp_path, contents):
    main_module.load_constants()
    passed_on = []
    monkeypatch.setattr(main_module.quicklz, "decompress", passed_on.append)
    path = tmp_path / "short.lz"
    path.write_bytes(contents)

    assert Makera.__new__(Makera).decompress_file(str(path), str(tmp_path / "out.nc")) is False
    assert passed_on == []


def _samples():
    rng = random.Random(314)
    gcode = "".join(
        "G1 X%.3f Y%.3f F%d\n" % (rng.random() * 100, rng.random() * 100, rng.choice((500, 1000))) for _ in range(3000)
    ).encode()
    yield b"G"
    for size in (1, 2, 3, 8, 15, 16, 17, 100, 215, 216, 217, 1000):
        yield gcode[:size]
        yield bytes(rng.randrange(256) for _ in range(size))
        yield b"x" * size
    for _ in range(100):
        start = rng.randrange(len(gcode) - 4096)
        yield gcode[start : start + rng.randrange(1, 4097)]
    for _ in range(30):
        yield bytes(rng.randrange(256) for _ in range(rng.randrange(1, 4097)))
        yield bytes(rng.choice(b"ab") for _ in range(rng.randrange(1, 4097)))
    yield gcode[:4096]
    yield bytes(4096)


def test_every_block_the_compressor_makes_passes_the_check():
    count = 0
    for data in _samples():
        block = quicklz.compress(data)
        check_block(block, 4096)
        count += 1
    assert count > 150


def test_the_damaged_block_is_refused_by_the_check():
    with pytest.raises(DamagedBlock, match="56542 bytes back"):
        check_block(DAMAGED_BLOCK, 4096)


@pytest.mark.parametrize(
    "block, reason",
    [
        (b"", "shorter"),
        (quicklz.compress(b"G1 X1 Y1\n" * 10)[:8], "shorter"),
        (bytes([0x47]) + quicklz.compress(b"G1 X1 Y1\n" * 10)[1:], "not QuickLZ level 3"),
        (quicklz.compress(b"G1 X1 Y1\n" * 10)[:-1], "block has"),
        (_claiming_size(quicklz.compress(b"G1 X1 Y1\n" * 50), 4097), "decompresses to 4097"),
        (_claiming_size(quicklz.compress(b"G1 X1 Y1\n" * 50), 0), "decompresses to 0"),
        (_overflowing_block(1024), "runs past the end of 1024"),
    ],
)
def test_the_check_says_what_is_wrong(block, reason):
    with pytest.raises(DamagedBlock, match=reason):
        check_block(block, 4096)


def _reference_walk(block):
    """The walk of check_block as a line-by-line copy of
    qlz_decompress_core (QuickLZ 1.4.1, level 3, with QLZ_MEMORY_SAFE), one
    literal step at a time, for a compressed block with a 9-byte header.
    True if the block passes."""
    bitlut = (4, 0, 1, 0, 2, 0, 1, 0, 3, 0, 1, 0, 2, 0, 1, 0)
    src = 9
    size = int.from_bytes(block[5:9], "little")
    last_source_byte = len(block) - 1
    last_destination_byte = size - 1
    last_matchstart = last_destination_byte - 6 - 4
    dst = 0
    cword_val = 1
    while True:
        if cword_val == 1:
            if src + 4 - 1 > last_source_byte:
                return False
            cword_val = int.from_bytes(block[src : src + 4], "little")
            src += 4
        if src + 4 - 1 > last_source_byte:
            return False
        fetch = int.from_bytes(block[src : src + 4], "little")
        if cword_val & 1:
            cword_val >>= 1
            if fetch & 3 == 0:
                offset, matchlen, src = (fetch & 0xFF) >> 2, 3, src + 1
            elif fetch & 2 == 0:
                offset, matchlen, src = (fetch & 0xFFFF) >> 2, 3, src + 2
            elif fetch & 1 == 0:
                offset, matchlen, src = (fetch & 0xFFFF) >> 6, ((fetch >> 2) & 15) + 3, src + 2
            elif fetch & 127 != 3:
                offset, matchlen, src = (fetch >> 7) & 0x1FFFF, ((fetch >> 2) & 0x1F) + 2, src + 3
            else:
                offset, matchlen, src = fetch >> 15, ((fetch >> 7) & 255) + 3, src + 4
            if dst - offset < 0 or dst - offset > dst - 2 - 1:
                return False
            if matchlen > last_destination_byte - dst - 4 + 1:
                return False
            dst += matchlen
        elif dst < last_matchstart:
            step = bitlut[cword_val & 0xF]
            dst += step
            src += step
            cword_val >>= step
        else:
            while dst <= last_destination_byte:
                if cword_val == 1:
                    src += 4
                    cword_val = 1 << 31
                if src >= last_source_byte + 1:
                    return False
                dst += 1
                src += 1
                cword_val >>= 1
            return True


def _passes(block):
    try:
        check_block(block, 4096)
    except DamagedBlock:
        return False
    return True


def test_the_check_agrees_with_quicklz_own_checks():
    """check_block takes a run of literal bytes in one step where QuickLZ
    takes up to 4 at a time; on blocks damaged at random they must still
    pass and fail alike."""
    rng = random.Random(2026)
    gcode = "".join("G1 X%.3f Y%.3f F1000\n" % (rng.random() * 100, rng.random() * 100) for _ in range(3000)).encode()
    outcomes = set()
    for _ in range(3000):
        start = rng.randrange(len(gcode) - 4096)
        block = bytearray(quicklz.compress(gcode[start : start + rng.randrange(300, 4097)]))
        for _ in range(rng.randrange(1, 6)):
            block[rng.randrange(9, len(block))] = rng.randrange(256)
        if rng.random() < 0.1:
            block[9:13] = bytes(4)  # a control word of 0: all literals
        expected = _reference_walk(bytes(block))
        assert _passes(bytes(block)) is expected, bytes(block).hex()
        outcomes.add(expected)
    assert outcomes == {True, False}


def test_damaged_blocks_that_pass_the_check_decompress_without_crashing(tmp_path):
    """The check is sound: blocks made by damaging real ones at random, and
    that it lets through, are all given to the native decompressor in one
    child process, which must survive them."""
    rng = random.Random(1012)
    gcode = "".join("G1 X%.3f Y%.3f F1000\n" % (rng.random() * 100, rng.random() * 100) for _ in range(3000)).encode()
    passed = []
    for _ in range(3000):
        start = rng.randrange(len(gcode) - 4096)
        block = bytearray(quicklz.compress(gcode[start : start + 4096]))
        for _ in range(rng.randrange(1, 6)):
            block[rng.randrange(9, len(block))] = rng.randrange(256)
        try:
            check_block(bytes(block), 4096)
        except DamagedBlock:
            continue
        passed.append(bytes(block))
    assert len(passed) > 100
    path = tmp_path / "passed.bin"
    path.write_bytes(b"".join(_lz_file(block)[:-2] for block in passed))

    result = _run_child(
        """
        import struct
        import sys
        import quicklz
        data = open(sys.argv[1], "rb").read()
        position = count = 0
        while position < len(data):
            size = struct.unpack(">I", data[position : position + 4])[0]
            quicklz.decompress(data[position + 4 : position + 4 + size])
            position += 4 + size
            count += 1
        print("decompressed", count)
        """,
        str(path),
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "decompressed %d" % len(passed) in result.stdout
