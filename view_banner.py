#!/usr/bin/env python3
"""
Banner Viewer - Extract and display textures from .bnr files
"""

import struct
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: PIL/Pillow required. Install with: pip install Pillow")
    sys.exit(1)


def decompress_lz11(data, offset=0):
    """Decompress LZ11 data."""
    if data[offset] != 0x11:
        raise ValueError(f'Not LZ11 compressed (got 0x{data[offset]:02x})')

    size = data[offset+1] | (data[offset+2] << 8) | (data[offset+3] << 16)
    result = bytearray(size)
    src = offset + 4
    dst = 0

    while dst < size and src < len(data):
        flags = data[src]
        src += 1

        for bit in range(8):
            if dst >= size or src >= len(data):
                break

            if flags & (0x80 >> bit):
                byte1 = data[src]
                src += 1

                if (byte1 >> 4) == 0:
                    if src + 1 >= len(data): break
                    byte2 = data[src]
                    src += 1
                    length = ((byte1 << 4) | (byte2 >> 4)) + 0x11
                    disp = ((byte2 & 0x0F) << 8) | data[src]
                    src += 1
                elif (byte1 >> 4) == 1:
                    if src + 2 >= len(data): break
                    byte2, byte3, byte4 = data[src], data[src+1], data[src+2]
                    src += 3
                    length = (((byte1 & 0x0F) << 12) | (byte2 << 4) | (byte3 >> 4)) + 0x111
                    disp = ((byte3 & 0x0F) << 8) | byte4
                else:
                    length = (byte1 >> 4) + 1
                    if src >= len(data): break
                    byte2 = data[src]
                    src += 1
                    disp = ((byte1 & 0x0F) << 8) | byte2

                disp += 1
                for i in range(length):
                    if dst >= size: break
                    result[dst] = result[dst - disp]
                    dst += 1
            else:
                result[dst] = data[src]
                src += 1
                dst += 1

    return bytes(result)


def decode_rgba8_texture(data, offset, width, height):
    """Decode RGBA8 Morton-tiled texture to PIL Image."""
    img = Image.new('RGBA', (width, height))
    pixels = img.load()

    pos = offset
    tiles_x = width // 8
    tiles_y = height // 8

    for ty in range(tiles_y):
        for tx in range(tiles_x):
            for t in range(64):
                # Morton decode
                px = (t & 1) | ((t >> 1) & 2) | ((t >> 2) & 4)
                py = ((t >> 1) & 1) | ((t >> 2) & 2) | ((t >> 3) & 4)

                x = tx * 8 + px
                y = ty * 8 + py

                if pos + 4 <= len(data):
                    # ABGR format
                    a, b, g, r = data[pos:pos+4]
                    if x < width and y < height:
                        pixels[x, y] = (r, g, b, a)
                pos += 4

    return img


def decode_etc1_solid_color(block: bytes) -> tuple[int, int, int]:
    """
    Decode a single ETC1 block assuming it's a solid fill (as used for Universal VC shell tint).
    The block layout is little-endian in CGFX; selectors are ignored and the first table/selector
    is used to derive the effective color.
    """
    if len(block) != 8:
        raise ValueError("ETC1 block must be 8 bytes")

    # Stored little-endian: [low32 selectors][hi32 control bits]
    low32 = int.from_bytes(block[0:4], "little")
    hi32 = int.from_bytes(block[4:8], "little")

    diff = (hi32 >> 1) & 1
    flip = hi32 & 1  # not used for uniform fill

    table1 = (hi32 >> 5) & 0x7
    table2 = (hi32 >> 2) & 0x7

    r5_1 = (hi32 >> 27) & 0x1F
    g5_1 = (hi32 >> 22) & 0x1F
    b5_1 = (hi32 >> 17) & 0x1F

    if diff:
        dr = (hi32 >> 14) & 0x7
        dg = (hi32 >> 11) & 0x7
        db = (hi32 >> 8) & 0x7
        if dr >= 4:
            dr -= 8
        if dg >= 4:
            dg -= 8
        if db >= 4:
            db -= 8
        r5_2 = (r5_1 + dr) & 0x1F
        g5_2 = (g5_1 + dg) & 0x1F
        b5_2 = (b5_1 + db) & 0x1F
    else:
        # Individual mode; colors stored separately (fallback)
        r5_2 = (hi32 >> 24) & 0x1F
        g5_2 = (hi32 >> 16) & 0xFF  # actually 4 bits, but unused here
        b5_2 = (hi32 >> 8) & 0xFF

    def expand5(v: int) -> int:
        return (v << 3) | (v >> 2)

    c1 = [expand5(r5_1), expand5(g5_1), expand5(b5_1)]
    c2 = [expand5(r5_2), expand5(g5_2), expand5(b5_2)]

    # ETC1 modifier tables
    tables = [
        (-8, -2, 2, 8),
        (-17, -5, 5, 17),
        (-29, -9, 9, 29),
        (-42, -13, 13, 42),
        (-60, -18, 18, 60),
        (-80, -24, 24, 80),
        (-106, -33, 33, 106),
        (-183, -47, 47, 183),
    ]

    # Selectors for the first pixel (LSBs of low32). If all selectors are 0 (as in solid fills),
    # this still produces the correct uniform color.
    selector_bits = low32 & 0xFFFF_FFFF
    sel0 = selector_bits & 0x3
    mod_table1 = tables[table1][sel0]
    mod_table2 = tables[table2][sel0]

    # Use subblock1 if flip=0, subblock2 if flip=1 for the representative color; difference is nil
    # for uniform fills since c1==c2 in our usage.
    base = c1 if not flip else c2
    mod = mod_table1 if not flip else mod_table2

    def clamp(v: int) -> int:
        return 0 if v < 0 else 255 if v > 255 else v

    return tuple(clamp(base[i] + mod) for i in range(3))


def decode_etc1_uniform_texture(data: bytes, offset: int, width: int, height: int) -> Image.Image:
    """
    Decode a texture made of repeated identical ETC1 blocks (as used for Universal VC shell tint).
    """
    block = data[offset : offset + 8]
    r, g, b = decode_etc1_solid_color(block)
    img = Image.new("RGBA", (width, height), (r, g, b, 255))
    return img


def view_banner(banner_path, output_dir=None):
    """Extract and display textures from a banner file."""
    banner_path = Path(banner_path)

    if output_dir is None:
        output_dir = banner_path.parent
    else:
        output_dir = Path(output_dir)

    print(f"Loading banner: {banner_path}")

    with open(banner_path, 'rb') as f:
        banner = f.read()

    # Check for CBMD header
    if banner[:4] != b'CBMD':
        print("Error: Not a valid banner file (no CBMD header)")
        return False

    # Parse header
    cgfx_offset = struct.unpack('<I', banner[0x08:0x0C])[0]
    cwav_offset = struct.unpack('<I', banner[0x84:0x88])[0]

    print(f"  CGFX offset: 0x{cgfx_offset:X}")
    print(f"  CWAV offset: 0x{cwav_offset:X}")

    # Decompress CGFX
    print("  Decompressing CGFX...")
    try:
        cgfx = decompress_lz11(banner, cgfx_offset)
        print(f"  Decompressed size: {len(cgfx)} bytes")
    except Exception as e:
        print(f"  Error decompressing: {e}")
        return False

    # Detect template type by size
    if len(cgfx) == 172416:
        template = "Universal VC"
        # COMMON1 front (128x128 RGBA8 tiled)
        front_offset = 0x5880
        back_offset = None
        cartridge_size = (128, 128)
        shell_offset = 0x23C70   # COMMON3 ETC1 solid fill
    elif len(cgfx) > 250000:
        template = "GBA VC"
        back_offset = None
        front_offset = 0x38F80
        cartridge_size = (128, 128)
    else:
        template = "Unknown"
        back_offset = None
        front_offset = 0x18000
        cartridge_size = (128, 128)

    print(f"  Template: {template}")

    # Extract cartridge texture(s)
    base_name = banner_path.stem

    # Extract frontside (main) texture
    print(f"  Extracting frontside texture at 0x{front_offset:X}...")
    front_img = decode_rgba8_texture(cgfx, front_offset, *cartridge_size)
    front_path = output_dir / f"{base_name}_cartridge.png"
    front_img.save(front_path)
    print(f"  Saved: {front_path}")

    # Extract backside texture for Universal VC
    if back_offset is not None:
        print(f"  Extracting backside texture at 0x{back_offset:X}...")
        back_img = decode_rgba8_texture(cgfx, back_offset, *cartridge_size)
        back_path = output_dir / f"{base_name}_cartridge_back.png"
        back_img.save(back_path)
        print(f"  Saved: {back_path}")

    # Extract shell tint (Universal VC ETC1 solid)
    if template == "Universal VC":
        try:
            shell_img = decode_etc1_uniform_texture(cgfx, shell_offset, 128, 128)
            shell_path = output_dir / f"{base_name}_shell.png"
            shell_img.save(shell_path)
            print(f"  Saved: {shell_path}")
        except Exception as e:
            print(f"  Warning: failed to decode shell tint: {e}")

    # Try to open the image
    try:
        import subprocess
        subprocess.Popen(['xdg-open', str(front_path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\n  Opened image viewer for cartridge texture")
    except:
        pass

    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python view_banner.py <banner.bnr> [output_dir]")
        print("\nExtracts textures from a .bnr banner file and opens them for viewing.")
        sys.exit(1)

    banner_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None

    view_banner(banner_path, output_dir)


if __name__ == '__main__':
    main()
