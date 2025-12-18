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
        back_offset = 0x8000     # Backside label
        front_offset = 0x18000   # Frontside label
        cartridge_size = (128, 128)
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
