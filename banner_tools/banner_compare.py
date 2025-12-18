#!/usr/bin/env python3
"""
Banner Compare Tool - Compare generated banner with template and working reference.
Shows all texture regions side-by-side for visual debugging.
"""

import struct
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
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
                px = (t & 1) | ((t >> 1) & 2) | ((t >> 2) & 4)
                py = ((t >> 1) & 1) | ((t >> 2) & 2) | ((t >> 3) & 4)

                x = tx * 8 + px
                y = ty * 8 + py

                if pos + 4 <= len(data):
                    a, b, g, r = data[pos:pos+4]
                    if x < width and y < height:
                        pixels[x, y] = (r, g, b, a)
                pos += 4

    return img


def decode_la8_texture(data, offset, width, height):
    """Decode LA8 Morton-tiled texture to PIL Image."""
    img = Image.new('RGBA', (width, height))
    pixels = img.load()

    tiles_x = width // 8
    tiles_y = height // 8
    pos = offset

    for ty in range(tiles_y):
        for tx in range(tiles_x):
            for t in range(64):
                px = (t & 1) | ((t >> 1) & 2) | ((t >> 2) & 4)
                py = ((t >> 1) & 1) | ((t >> 2) & 2) | ((t >> 3) & 4)

                x = tx * 8 + px
                y = ty * 8 + py

                if pos + 2 <= len(data):
                    a, l = data[pos:pos+2]
                    if x < width and y < height:
                        pixels[x, y] = (l, l, l, a)
                pos += 2

    return img


def load_banner(path):
    """Load and decompress banner CGFX."""
    path = Path(path)

    if path.suffix.lower() == '.cia':
        # Extract banner from CIA
        cia_data = path.read_bytes()
        cbmd_offset = cia_data.find(b'CBMD')
        if cbmd_offset == -1:
            raise ValueError("No CBMD header found in CIA")
        banner = cia_data[cbmd_offset:]
    elif path.suffix.lower() == '.bnr':
        banner = path.read_bytes()
    elif path.suffix.lower() == '.cgfx':
        # Raw uncompressed CGFX
        return path.read_bytes()
    else:
        raise ValueError(f"Unknown file type: {path.suffix}")

    if banner[:4] != b'CBMD':
        raise ValueError("Not a valid banner (no CBMD header)")

    cgfx_offset = struct.unpack('<I', banner[0x08:0x0C])[0]
    cwav_offset = struct.unpack('<I', banner[0x84:0x88])[0]

    return decompress_lz11(banner, cgfx_offset)


def analyze_cgfx(cgfx, name=""):
    """Analyze CGFX and return texture info."""
    info = {
        'name': name,
        'size': len(cgfx),
        'textures': {}
    }

    # Universal VC template (172416 bytes)
    if len(cgfx) == 172416:
        info['template'] = 'Universal VC'
        info['textures']['label_128'] = (0x5880, 0x15880, "COMMON1 base (128x128)")
        info['textures']['label_64'] = (0x15880, 0x19880, "COMMON1 mip (64x64)")
        info['textures']['label_32'] = (0x19880, 0x1A880, "COMMON1 mip (32x32)")
        info['textures']['label_16'] = (0x1A880, 0x1AC80, "COMMON1 mip (16x16)")
        info['textures']['label_8'] = (0x1AC80, 0x1AD80, "COMMON1 mip (8x8)")
        info['textures']['footer'] = (0x1AD80, 0x22D80, "COMMON2 footer (256x64 LA8)")
    else:
        info['template'] = 'Unknown'
        info['textures']['main'] = (0x18000, 0x28000, "Main texture")

    return info


def count_colors(cgfx, start, end):
    """Count special colors in a region."""
    red = 0
    transparent = 0
    green = 0

    for i in range(start, min(end, len(cgfx)), 4):
        a, b, g, r = cgfx[i:i+4]
        if a == 0:
            transparent += 1
        if r > 200 and g < 50 and b < 50 and a > 200:
            red += 1
        if g > 200 and r < 180 and b < 180 and a > 200:
            green += 1

    total = (end - start) // 4
    return {
        'total': total,
        'red': red,
        'transparent': transparent,
        'green': green,
        'red_pct': 100 * red / total if total > 0 else 0,
        'transparent_pct': 100 * transparent / total if total > 0 else 0,
    }


def create_comparison_image(sources, output_path):
    """Create a side-by-side comparison image."""

    # Layout: 3 columns (template, generated, reference) x 2 rows (back, front)
    cell_size = 160  # Texture + label
    padding = 10
    label_height = 30

    cols = len(sources)
    rows = 2  # backside, frontside

    width = cols * cell_size + (cols + 1) * padding
    height = rows * (128 + label_height) + (rows + 1) * padding + 60  # Extra for header

    result = Image.new('RGBA', (width, height), (40, 40, 40, 255))
    draw = ImageDraw.Draw(result)

    # Try to load a font
    try:
        font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 14)
        small_font = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans.ttf", 11)
    except:
        font = ImageFont.load_default()
        small_font = font

    # Draw header
    draw.text((padding, 10), "Banner Texture Comparison", fill=(255, 255, 255), font=font)

    y_offset = 50

    for row, (region_key, region_name) in enumerate([('label_128', 'Label 128x128 (0x5880)'), ('footer', 'Footer (0x1AD80)')]):
        for col, (name, cgfx) in enumerate(sources):
            x = padding + col * (cell_size + padding)
            y = y_offset + row * (128 + label_height + padding)

            # Draw column header (only first row)
            if row == 0:
                draw.text((x, y - 25), name[:20], fill=(200, 200, 200), font=small_font)

            # Extract texture
            if len(cgfx) == 172416:  # Universal VC
                if region_key == 'footer':
                    offset = 0x1AD80
                    tex = decode_la8_texture(cgfx, offset, 256, 64)
                else:
                    offset = 0x5880
                    tex = decode_rgba8_texture(cgfx, offset, 128, 128)

                # Count special pixels
                colors = count_colors(cgfx, offset, offset + 0x10000)

                # Draw texture
                result.paste(tex, (x, y))

                # Draw border based on issues
                border_color = (100, 100, 100)
                if colors['red'] > 100:
                    border_color = (255, 0, 0)  # Red = has placeholder
                elif colors['transparent'] > 8000:
                    border_color = (0, 150, 255)  # Blue = mostly transparent

                draw.rectangle([x-1, y-1, x+129, y+129], outline=border_color, width=2)

                # Draw stats
                stats = f"R:{colors['red']} T:{colors['transparent']}"
                draw.text((x, y + 130), stats, fill=(150, 150, 150), font=small_font)
            else:
                draw.text((x + 20, y + 50), "N/A", fill=(100, 100, 100), font=font)

        # Row label
        draw.text((5, y_offset + row * (128 + label_height + padding) + 50),
                  region_name, fill=(255, 255, 100), font=small_font)

    # Legend
    legend_y = height - 25
    draw.rectangle([padding, legend_y, padding + 15, legend_y + 15], outline=(255, 0, 0), width=2)
    draw.text((padding + 20, legend_y), "Has red placeholder", fill=(200, 200, 200), font=small_font)

    draw.rectangle([padding + 180, legend_y, padding + 195, legend_y + 15], outline=(0, 150, 255), width=2)
    draw.text((padding + 200, legend_y), "Mostly transparent", fill=(200, 200, 200), font=small_font)

    result.save(output_path)
    return output_path


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Compare banner textures')
    parser.add_argument('files', nargs='+', help='Banner files to compare (.bnr, .cia, .cgfx)')
    parser.add_argument('-o', '--output', default='/tmp/banner_comparison.png',
                        help='Output comparison image')
    parser.add_argument('-t', '--template', help='Template CGFX file for comparison')
    parser.add_argument('--open', action='store_true', help='Open result in viewer')

    args = parser.parse_args()

    sources = []

    # Load template if provided
    if args.template:
        try:
            cgfx = load_banner(args.template)
            sources.append(('Template', cgfx))
            print(f"Loaded template: {args.template} ({len(cgfx)} bytes)")
        except Exception as e:
            print(f"Warning: Failed to load template: {e}")

    # Load input files
    for path in args.files:
        try:
            cgfx = load_banner(path)
            name = Path(path).stem
            sources.append((name, cgfx))

            info = analyze_cgfx(cgfx, name)
            print(f"\nLoaded: {path}")
            print(f"  Size: {info['size']} bytes")
            print(f"  Template: {info['template']}")

            for tex_name, (start, end, desc) in info['textures'].items():
                colors = count_colors(cgfx, start, end)
                print(f"  {desc}:")
                print(f"    Red pixels: {colors['red']} ({colors['red_pct']:.1f}%)")
                print(f"    Transparent: {colors['transparent']} ({colors['transparent_pct']:.1f}%)")

        except Exception as e:
            print(f"Error loading {path}: {e}")

    if len(sources) >= 1:
        output = create_comparison_image(sources, args.output)
        print(f"\nComparison image saved: {output}")

        if args.open:
            import subprocess
            try:
                subprocess.Popen(['xdg-open', output],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                pass
    else:
        print("No valid files to compare")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
