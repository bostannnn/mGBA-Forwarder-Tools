#!/usr/bin/env python3
"""
CGFX Texture Finder

Scans a CGFX file and extracts textures at various candidate offsets.
Helps identify the correct offsets for different VC templates.
"""

import sys
import struct
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def morton_index(x: int, y: int) -> int:
    """Morton Z-order index for 8x8 tile."""
    morton = 0
    for i in range(3):
        morton |= ((x >> i) & 1) << (2 * i)
        morton |= ((y >> i) & 1) << (2 * i + 1)
    return morton


def decode_rgba8(data: bytes, width: int, height: int) -> 'Image.Image':
    """Decode RGBA8 Morton-tiled texture."""
    tiles_x = width // 8
    tiles_y = height // 8
    expected = tiles_x * tiles_y * 256
    
    if len(data) < expected:
        return None
    
    img = Image.new('RGBA', (width, height))
    pixels = img.load()
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = (ty * tiles_x + tx) * 256
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 4
                    if idx + 4 <= len(data):
                        a, b, g, r = data[idx:idx + 4]
                        pixels[tx * 8 + px, ty * 8 + py] = (r, g, b, a)
    return img


def decode_la8(data: bytes, width: int, height: int) -> 'Image.Image':
    """Decode LA8 Morton-tiled texture."""
    tiles_x = width // 8
    tiles_y = height // 8
    expected = tiles_x * tiles_y * 128
    
    if len(data) < expected:
        return None
    
    img = Image.new('RGBA', (width, height))
    pixels = img.load()
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = (ty * tiles_x + tx) * 128
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 2
                    if idx + 2 <= len(data):
                        a, l = data[idx:idx + 2]
                        pixels[tx * 8 + px, ty * 8 + py] = (l, l, l, a)
    return img


def find_textures(cgfx_path: str, output_dir: str = '.'):
    """Find and extract textures from CGFX file."""
    with open(cgfx_path, 'rb') as f:
        data = f.read()
    
    basename = Path(cgfx_path).stem
    out_path = Path(output_dir)
    out_path.mkdir(exist_ok=True)
    
    print(f"\n{'='*60}")
    print(f"Scanning: {Path(cgfx_path).name} ({len(data):,} bytes)")
    print(f"{'='*60}\n")
    
    # Known texture sizes to look for
    texture_configs = [
        # (width, height, format, name, expected_bytes)
        (128, 128, 'RGBA8', 'COMMON1_128x128_RGBA8', 65536),
        (256, 64, 'LA8', 'COMMON2_256x64_LA8', 32768),
        (128, 128, 'LA8', 'texture_128x128_LA8', 32768),
        (128, 128, 'ETC1', 'COMMON3_128x128_ETC1', 8192),  # Can't decode easily
    ]
    
    # Scan for potential texture data starts
    # Textures are usually 0x100 or 0x1000 aligned
    candidates = []
    
    for align in [0x100]:
        for offset in range(align, len(data) - 0x8000, align):
            # Check if this looks like texture data
            # (non-zero, somewhat random-looking)
            chunk = data[offset:offset + 64]
            if len(set(chunk)) > 10:  # Has some variety
                candidates.append(offset)
    
    print(f"Found {len(candidates)} candidate offsets (0x100 aligned with entropy)\n")
    
    # Try to extract textures at candidate offsets
    print("Extracting candidate textures...\n")
    
    found_textures = []
    
    for offset in candidates:
        for width, height, fmt, name, expected in texture_configs:
            if offset + expected > len(data):
                continue
            
            chunk = data[offset:offset + expected]
            
            # Try to decode
            if fmt == 'RGBA8':
                img = decode_rgba8(chunk, width, height)
            elif fmt == 'LA8':
                img = decode_la8(chunk, width, height)
            else:
                continue
            
            if img:
                # Check if it looks like a real texture (not all same color)
                colors = set()
                for y in range(min(height, 32)):
                    for x in range(min(width, 32)):
                        colors.add(img.getpixel((x, y)))
                
                if len(colors) > 3:  # Has some variety
                    out_file = out_path / f"{basename}_0x{offset:05X}_{width}x{height}_{fmt}.png"
                    img.save(str(out_file))
                    found_textures.append((offset, width, height, fmt, str(out_file)))
                    print(f"  Found: 0x{offset:05X} - {width}x{height} {fmt}")
    
    print(f"\n{'='*60}")
    print(f"Summary: Found {len(found_textures)} potential textures")
    print(f"{'='*60}\n")
    
    for offset, width, height, fmt, path in found_textures:
        print(f"  Offset 0x{offset:05X}: {width}x{height} {fmt}")
        print(f"    Saved: {Path(path).name}")
    
    # Known GBA offsets for comparison
    print(f"\n{'='*60}")
    print("GBA VC Known Offsets (for reference):")
    print(f"{'='*60}")
    print("  COMMON1 (cartridge label): 0x19100 - 128x128 RGBA8")
    print("  COMMON2 (footer text):     0x0BC00 - 256x64 LA8")
    print("  COMMON3 (background):      0x05C00 - 128x128 ETC1")
    
    return found_textures


def extract_at_offset(cgfx_path: str, offset: int, width: int, height: int, 
                       fmt: str = 'RGBA8', output: str = None):
    """Extract texture at specific offset."""
    with open(cgfx_path, 'rb') as f:
        data = f.read()
    
    if fmt == 'RGBA8':
        expected = width * height * 4 // 64 * 256  # Morton tiled
        img = decode_rgba8(data[offset:offset + expected], width, height)
    elif fmt == 'LA8':
        expected = width * height * 2 // 64 * 128  # Morton tiled
        img = decode_la8(data[offset:offset + expected], width, height)
    else:
        print(f"Unknown format: {fmt}")
        return
    
    if img:
        if not output:
            output = f"texture_0x{offset:05X}_{width}x{height}_{fmt}.png"
        img.save(output)
        print(f"Saved: {output}")
    else:
        print("Failed to decode texture")


def compare_templates(cgfx_paths: list):
    """Compare multiple CGFX files to find differences."""
    print(f"\n{'='*60}")
    print("Comparing CGFX templates")
    print(f"{'='*60}\n")
    
    data_list = []
    for path in cgfx_paths:
        with open(path, 'rb') as f:
            data_list.append((Path(path).name, f.read()))
    
    for name, data in data_list:
        print(f"{name}: {len(data):,} bytes")
    
    # Find COMMON strings in each
    print("\nCOMMON string locations:")
    for name, data in data_list:
        print(f"\n  {name}:")
        for common in ['COMMON1', 'COMMON2', 'COMMON3']:
            pos = data.find(common.encode())
            if pos != -1:
                print(f"    {common}: 0x{pos:05X}")


def main():
    if not HAS_PIL:
        print("ERROR: Pillow is required. Install with: pip install Pillow")
        sys.exit(1)
    
    if len(sys.argv) < 2:
        print("CGFX Texture Finder")
        print()
        print("Usage:")
        print("  python3 find_textures.py <banner.cgfx>")
        print("    Scan and extract all potential textures")
        print()
        print("  python3 find_textures.py <banner.cgfx> -at OFFSET WIDTH HEIGHT [FORMAT]")
        print("    Extract texture at specific offset")
        print()
        print("  python3 find_textures.py -compare <cgfx1> <cgfx2> ...")
        print("    Compare multiple CGFX files")
        print()
        print("Examples:")
        print("  python3 find_textures.py gbc_vc/banner.cgfx")
        print("  python3 find_textures.py banner.cgfx -at 0x19100 128 128 RGBA8")
        print("  python3 find_textures.py -compare gba_vc/banner.cgfx gbc_vc/banner.cgfx")
        sys.exit(1)
    
    if sys.argv[1] == '-compare':
        compare_templates(sys.argv[2:])
    elif '-at' in sys.argv:
        idx = sys.argv.index('-at')
        cgfx = sys.argv[1]
        offset = int(sys.argv[idx + 1], 16) if sys.argv[idx + 1].startswith('0x') else int(sys.argv[idx + 1])
        width = int(sys.argv[idx + 2])
        height = int(sys.argv[idx + 3])
        fmt = sys.argv[idx + 4] if len(sys.argv) > idx + 4 else 'RGBA8'
        extract_at_offset(cgfx, offset, width, height, fmt)
    else:
        find_textures(sys.argv[1])


if __name__ == '__main__':
    main()
