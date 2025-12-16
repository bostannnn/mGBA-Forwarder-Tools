#!/usr/bin/env python3
"""
CGFX Texture Offset Analyzer

Parses a CGFX file to find TXOB (Texture Object) entries and their data offsets.
Use this to find the correct offsets for different VC templates (GBA, GBC, GB, NES, SNES).
"""

import sys
import struct
from pathlib import Path


def read_string(data: bytes, offset: int, max_len: int = 64) -> str:
    """Read null-terminated string from data."""
    end = data.find(b'\x00', offset, offset + max_len)
    if end == -1:
        end = offset + max_len
    return data[offset:end].decode('ascii', errors='replace')


def analyze_cgfx(cgfx_path: str):
    """Analyze CGFX file and extract texture information."""
    with open(cgfx_path, 'rb') as f:
        data = f.read()
    
    print(f"\n{'='*60}")
    print(f"CGFX Analysis: {Path(cgfx_path).name}")
    print(f"File size: {len(data):,} bytes")
    print(f"{'='*60}\n")
    
    # Check CGFX magic
    if data[:4] != b'CGFX':
        print("ERROR: Not a CGFX file!")
        return
    
    # Parse header
    bom = struct.unpack_from('<H', data, 4)[0]
    header_size = struct.unpack_from('<H', data, 6)[0]
    version = struct.unpack_from('<I', data, 8)[0]
    file_size = struct.unpack_from('<I', data, 12)[0]
    num_entries = struct.unpack_from('<I', data, 16)[0]
    
    print(f"Header:")
    print(f"  BOM: 0x{bom:04X} ({'Little Endian' if bom == 0xFEFF else 'Big Endian'})")
    print(f"  Header size: {header_size}")
    print(f"  Version: 0x{version:08X}")
    print(f"  File size (header): {file_size:,}")
    print(f"  Num entries: {num_entries}")
    print()
    
    # Find DATA section
    data_pos = data.find(b'DATA')
    if data_pos != -1:
        data_size = struct.unpack_from('<I', data, data_pos + 4)[0]
        print(f"DATA section at 0x{data_pos:X}, size: {data_size:,} bytes\n")
    
    # Find all TXOB (Texture Object) entries
    print("Searching for TXOB (Texture Object) entries...")
    print("-" * 60)
    
    textures = []
    pos = 0
    while True:
        pos = data.find(b'TXOB', pos)
        if pos == -1:
            break
        
        # Parse TXOB header (structure based on ctrulib/citro3d)
        try:
            # TXOB starts with "TXOB" magic
            # Followed by various fields including dimensions and format
            
            # Try to find the texture name (usually nearby)
            name = None
            for search_offset in range(-128, 256, 4):
                if pos + search_offset < 0 or pos + search_offset >= len(data) - 32:
                    continue
                # Look for "COMMON" strings
                test_pos = pos + search_offset
                if data[test_pos:test_pos+6] == b'COMMON':
                    name = read_string(data, test_pos)
                    break
            
            # Parse texture dimensions and info from TXOB
            # Offset 0x08: sometimes has size info
            # Offset 0x0C-0x10: often has dimensions
            
            # Try multiple potential dimension locations
            width = height = fmt = 0
            data_offset = 0
            
            # Parse size field (at offset 4 from TXOB)
            size_field = struct.unpack_from('<I', data, pos + 4)[0]
            
            # Look for dimensions
            for dim_off in [0x10, 0x14, 0x18, 0x1C, 0x20]:
                if pos + dim_off + 4 > len(data):
                    continue
                w = struct.unpack_from('<H', data, pos + dim_off)[0]
                h = struct.unpack_from('<H', data, pos + dim_off + 2)[0]
                # Valid dimensions are power of 2, typical values
                if w in [32, 64, 128, 256, 512] and h in [32, 64, 128, 256, 512]:
                    width, height = w, h
                    break
            
            # Try to find format field
            for fmt_off in [0x08, 0x0C, 0x24, 0x28]:
                if pos + fmt_off + 4 > len(data):
                    continue
                f = struct.unpack_from('<I', data, pos + fmt_off)[0]
                # Known formats: RGBA8=0, RGB8=1, RGBA5551=2, RGB565=3, RGBA4=4, 
                # LA8=5, HILO8=6, L8=7, A8=8, LA4=9, L4=10, A4=11, ETC1=12, ETC1A4=13
                if f in range(0, 14):
                    fmt = f
                    break
            
            # Calculate expected data offset based on texture size
            format_bpp = {
                0: 4,   # RGBA8
                1: 3,   # RGB8
                2: 2,   # RGBA5551
                3: 2,   # RGB565
                4: 2,   # RGBA4
                5: 2,   # LA8
                6: 2,   # HILO8
                7: 1,   # L8
                8: 1,   # A8
                9: 1,   # LA4
                10: 0.5, # L4
                11: 0.5, # A4
                12: 0.5, # ETC1 (4bpp)
                13: 1,   # ETC1A4 (8bpp)
            }
            
            bpp = format_bpp.get(fmt, 4)
            expected_size = int(width * height * bpp) if width and height else 0
            
            textures.append({
                'pos': pos,
                'name': name,
                'width': width,
                'height': height,
                'format': fmt,
                'expected_size': expected_size,
            })
            
        except Exception as e:
            print(f"  Error parsing TXOB at 0x{pos:X}: {e}")
        
        pos += 4
    
    # Display texture info
    format_names = {
        0: 'RGBA8', 1: 'RGB8', 2: 'RGBA5551', 3: 'RGB565', 4: 'RGBA4',
        5: 'LA8', 6: 'HILO8', 7: 'L8', 8: 'A8', 9: 'LA4',
        10: 'L4', 11: 'A4', 12: 'ETC1', 13: 'ETC1A4'
    }
    
    print(f"\nFound {len(textures)} TXOB entries:\n")
    for i, tex in enumerate(textures):
        fmt_name = format_names.get(tex['format'], f"Unknown({tex['format']})")
        print(f"Texture {i + 1}:")
        print(f"  TXOB at: 0x{tex['pos']:05X}")
        if tex['name']:
            print(f"  Name: {tex['name']}")
        if tex['width'] and tex['height']:
            print(f"  Dimensions: {tex['width']}×{tex['height']}")
            print(f"  Format: {fmt_name}")
            print(f"  Expected data size: {tex['expected_size']:,} bytes")
        print()
    
    # Now let's search for COMMON texture names directly
    print("-" * 60)
    print("Searching for COMMON texture name strings...")
    print("-" * 60)
    
    common_names = ['COMMON1', 'COMMON2', 'COMMON3', 'COMMON4', 'COMMON5']
    for name in common_names:
        pos = 0
        while True:
            pos = data.find(name.encode(), pos)
            if pos == -1:
                break
            
            # Check if it's a proper null-terminated string
            end = data.find(b'\x00', pos)
            if end != -1 and end - pos < 32:
                full_name = data[pos:end].decode('ascii', errors='replace')
                print(f"  Found '{full_name}' at offset 0x{pos:05X}")
            pos += 1
    
    # Heuristic: Find large data blocks that could be textures
    print("\n" + "-" * 60)
    print("Texture data region detection...")
    print("-" * 60)
    
    # Common texture sizes
    known_sizes = [
        (128 * 128 * 4, "128×128 RGBA8"),  # 65536
        (128 * 128 * 2, "128×128 LA8"),    # 32768
        (256 * 64 * 2, "256×64 LA8"),      # 32768
        (256 * 64 * 4, "256×64 RGBA8"),    # 65536
        (128 * 128 // 2, "128×128 ETC1"),  # 8192
        (64 * 64 * 4, "64×64 RGBA8"),      # 16384
        (256 * 128 * 4, "256×128 RGBA8"),  # 131072
    ]
    
    # Look for patterns that suggest texture data boundaries
    # Texture data is usually aligned to nice boundaries
    print("\nPotential texture data regions (aligned to 0x100):")
    for align in [0x100, 0x1000]:
        start = (data_pos + 0x100) if data_pos != -1 else 0x100
        for off in range(start, len(data) - 0x1000, align):
            # Check if this looks like texture data start
            # (high entropy, or specific patterns)
            chunk = data[off:off+16]
            if len(set(chunk)) > 8:  # Some entropy
                # Check if size matches known texture sizes from this offset
                for size, desc in known_sizes:
                    if off + size <= len(data):
                        # Could be a texture
                        pass
    
    # Final summary with guessed offsets
    print("\n" + "=" * 60)
    print("RECOMMENDED OFFSETS TO TRY:")
    print("=" * 60)
    print("""
Based on analysis, try these offsets in TEMPLATES dict:

For a typical VC banner with:
- COMMON1 = 128×128 RGBA8 (65,536 bytes) = cartridge label
- COMMON2 = 256×64 LA8 (32,768 bytes) = footer text  
- COMMON3 = 128×128 ETC1 (8,192 bytes) = background

The GBA template uses:
- COMMON1 offset: 0x19100
- COMMON2 offset: 0xBC00

To find correct offsets for another template:
1. Look at the COMMON name strings found above
2. The texture data usually starts ~0x80-0x100 bytes before the name
3. Use a hex editor to verify the offset points to texture data

You can also run:
  python3 cgfx_analyzer.py <template.cgfx> --dump-at 0xOFFSET 128 128

to dump and visualize what's at a specific offset.
""")


def dump_texture(cgfx_path: str, offset: int, width: int, height: int, format_name: str = 'RGBA8'):
    """Dump a texture from a specific offset for verification."""
    try:
        from PIL import Image
    except ImportError:
        print("PIL required for texture dumping. Install with: pip install Pillow")
        return
    
    with open(cgfx_path, 'rb') as f:
        data = f.read()
    
    def morton_index(x: int, y: int) -> int:
        morton = 0
        for i in range(3):
            morton |= ((x >> i) & 1) << (2 * i)
            morton |= ((y >> i) & 1) << (2 * i + 1)
        return morton
    
    if format_name == 'RGBA8':
        bpp = 4
        tile_size = 256
    elif format_name == 'LA8':
        bpp = 2
        tile_size = 128
    else:
        print(f"Unknown format: {format_name}")
        return
    
    tiles_x = width // 8
    tiles_y = height // 8
    expected_size = tiles_x * tiles_y * tile_size
    
    print(f"Extracting {width}×{height} {format_name} texture from offset 0x{offset:X}")
    print(f"Expected size: {expected_size:,} bytes")
    
    if offset + expected_size > len(data):
        print("ERROR: Offset + size exceeds file length!")
        return
    
    tex_data = data[offset:offset + expected_size]
    
    img = Image.new('RGBA', (width, height))
    pixels = img.load()
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = (ty * tiles_x + tx) * tile_size
            for py in range(8):
                for px in range(8):
                    x = tx * 8 + px
                    y = ty * 8 + py
                    morton = morton_index(px, py)
                    
                    if format_name == 'RGBA8':
                        idx = tile_off + morton * 4
                        a, b, g, r = tex_data[idx:idx + 4]
                        pixels[x, y] = (r, g, b, a)
                    elif format_name == 'LA8':
                        idx = tile_off + morton * 2
                        a, l = tex_data[idx:idx + 2]
                        pixels[x, y] = (l, l, l, a)
    
    out_path = f"texture_0x{offset:X}_{width}x{height}_{format_name}.png"
    img.save(out_path)
    print(f"Saved: {out_path}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 cgfx_analyzer.py <banner.cgfx>")
        print("  python3 cgfx_analyzer.py <banner.cgfx> --dump-at 0xOFFSET WIDTH HEIGHT [FORMAT]")
        print()
        print("Examples:")
        print("  python3 cgfx_analyzer.py templates/gbc_vc/banner.cgfx")
        print("  python3 cgfx_analyzer.py banner.cgfx --dump-at 0x19100 128 128 RGBA8")
        print("  python3 cgfx_analyzer.py banner.cgfx --dump-at 0xBC00 256 64 LA8")
        sys.exit(1)
    
    cgfx_path = sys.argv[1]
    
    if not Path(cgfx_path).exists():
        print(f"File not found: {cgfx_path}")
        sys.exit(1)
    
    if len(sys.argv) > 2 and sys.argv[2] == '--dump-at':
        if len(sys.argv) < 6:
            print("Usage: --dump-at 0xOFFSET WIDTH HEIGHT [FORMAT]")
            sys.exit(1)
        offset = int(sys.argv[3], 16) if sys.argv[3].startswith('0x') else int(sys.argv[3])
        width = int(sys.argv[4])
        height = int(sys.argv[5])
        fmt = sys.argv[6] if len(sys.argv) > 6 else 'RGBA8'
        dump_texture(cgfx_path, offset, width, height, fmt)
    else:
        analyze_cgfx(cgfx_path)


if __name__ == '__main__':
    main()
