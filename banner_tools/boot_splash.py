#!/usr/bin/env python3
"""
3DS Boot Splash (Logo) Editor

The boot splash is what appears when launching a CIA before the app loads.
For homebrew, this is typically the Homebrew Launcher logo.

Boot splash structure:
- Stored in ExeFS as logo.bcma.lz or logo.darc.lz (LZ11 compressed DARC archive)
- Contains BCLIM images for top and bottom screens
- Top screen: 400x240 (or 800x240 for wide mode)
- Bottom screen: 320x240

This tool can:
1. Create custom boot splashes from PNG images
2. Extract existing boot splashes
3. Convert between formats
"""

import os
import sys
import struct
from pathlib import Path


# BCLIM texture formats
BCLIM_FORMATS = {
    0: ('L8', 1),       # 8-bit luminance
    1: ('A8', 1),       # 8-bit alpha
    2: ('LA4', 1),      # 4-bit luminance + 4-bit alpha
    3: ('LA8', 2),      # 8-bit luminance + 8-bit alpha
    4: ('HILO8', 2),    # HILO
    5: ('RGB565', 2),   # 16-bit RGB
    6: ('RGB8', 3),     # 24-bit RGB
    7: ('RGBA5551', 2), # 16-bit RGBA
    8: ('RGBA4', 2),    # 16-bit RGBA
    9: ('RGBA8', 4),    # 32-bit RGBA
    10: ('ETC1', 0.5),  # ETC1 compressed
    11: ('ETC1A4', 1),  # ETC1 with 4-bit alpha
    12: ('L4', 0.5),    # 4-bit luminance
    13: ('A4', 0.5),    # 4-bit alpha
}

# Morton order for 8x8 tiles
TILE_ORDER = [
    0, 1, 8, 9, 2, 3, 10, 11,
    16, 17, 24, 25, 18, 19, 26, 27,
    4, 5, 12, 13, 6, 7, 14, 15,
    20, 21, 28, 29, 22, 23, 30, 31,
    32, 33, 40, 41, 34, 35, 42, 43,
    48, 49, 56, 57, 50, 51, 58, 59,
    36, 37, 44, 45, 38, 39, 46, 47,
    52, 53, 60, 61, 54, 55, 62, 63
]


def lz11_decompress(data):
    """Decompress LZ11 data"""
    if len(data) < 4 or data[0] != 0x11:
        return data
    
    decompressed_size = struct.unpack_from('<I', data, 0)[0] >> 8
    result = bytearray()
    pos = 4
    
    while len(result) < decompressed_size and pos < len(data):
        flags = data[pos]
        pos += 1
        
        for i in range(8):
            if pos >= len(data) or len(result) >= decompressed_size:
                break
            
            if flags & (0x80 >> i):
                if pos + 1 >= len(data):
                    break
                
                byte1 = data[pos]
                byte2 = data[pos + 1]
                pos += 2
                
                indicator = byte1 >> 4
                
                if indicator == 0:
                    if pos >= len(data):
                        break
                    byte3 = data[pos]
                    pos += 1
                    length = ((byte1 & 0x0F) << 4 | (byte2 >> 4)) + 0x11
                    disp = ((byte2 & 0x0F) << 8 | byte3) + 1
                elif indicator == 1:
                    if pos + 1 >= len(data):
                        break
                    byte3 = data[pos]
                    byte4 = data[pos + 1]
                    pos += 2
                    length = ((byte1 & 0x0F) << 12 | byte2 << 4 | (byte3 >> 4)) + 0x111
                    disp = ((byte3 & 0x0F) << 8 | byte4) + 1
                else:
                    length = indicator + 1
                    disp = ((byte1 & 0x0F) << 8 | byte2) + 1
                
                for _ in range(length):
                    if len(result) < disp:
                        result.append(0)
                    else:
                        result.append(result[-disp])
            else:
                result.append(data[pos])
                pos += 1
    
    return bytes(result)


def lz11_compress(data):
    """Simple LZ11 compression"""
    result = bytearray()
    size = len(data)
    result.extend([0x11, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF])
    
    pos = 0
    while pos < len(data):
        flag_pos = len(result)
        result.append(0)
        flags = 0
        
        for i in range(8):
            if pos >= len(data):
                break
            
            best_len = 0
            best_disp = 0
            
            search_start = max(0, pos - 4096)
            for sp in range(search_start, pos):
                ml = 0
                while (pos + ml < len(data) and ml < 0x10110 and
                       data[sp + (ml % (pos - sp))] == data[pos + ml]):
                    ml += 1
                if ml > best_len:
                    best_len = ml
                    best_disp = pos - sp
            
            if best_len >= 3:
                flags |= (0x80 >> i)
                disp = best_disp - 1
                
                if best_len <= 0x10:
                    result.append(((best_len - 1) << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                elif best_len <= 0x110:
                    length = best_len - 0x11
                    result.append((length >> 4) & 0x0F)
                    result.append(((length & 0x0F) << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                else:
                    length = best_len - 0x111
                    result.append(0x10 | ((length >> 12) & 0x0F))
                    result.append((length >> 4) & 0xFF)
                    result.append(((length & 0x0F) << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                
                pos += best_len
            else:
                result.append(data[pos])
                pos += 1
        
        result[flag_pos] = flags
    
    return bytes(result)


def rgb565_to_rgba(value):
    """Convert RGB565 to RGBA"""
    r = ((value >> 11) & 0x1F) << 3
    g = ((value >> 5) & 0x3F) << 2
    b = (value & 0x1F) << 3
    return (r, g, b, 255)


def rgba_to_rgb565(r, g, b, a=255):
    """Convert RGBA to RGB565"""
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def create_bclim(image_data, width, height, format_type=5):
    """
    Create a BCLIM image file.
    
    format_type: 5 = RGB565 (recommended for boot splash)
    """
    # Calculate padded dimensions (must be multiples of 8)
    padded_width = ((width + 7) // 8) * 8
    padded_height = ((height + 7) // 8) * 8
    
    # Convert image data to tiles
    if format_type == 5:  # RGB565
        bytes_per_pixel = 2
    elif format_type == 9:  # RGBA8
        bytes_per_pixel = 4
    else:
        raise ValueError(f"Unsupported format: {format_type}")
    
    # Create tiled texture data
    texture_data = bytearray()
    
    for tile_y in range(padded_height // 8):
        for tile_x in range(padded_width // 8):
            for morton_idx in range(64):
                pixel_idx = TILE_ORDER[morton_idx]
                local_x = pixel_idx % 8
                local_y = pixel_idx // 8
                
                x = tile_x * 8 + local_x
                y = tile_y * 8 + local_y
                
                # Flip Y axis
                src_y = height - 1 - y
                
                if x < width and src_y >= 0 and src_y < height:
                    src_idx = (src_y * width + x) * 4
                    if src_idx + 3 < len(image_data):
                        r = image_data[src_idx]
                        g = image_data[src_idx + 1]
                        b = image_data[src_idx + 2]
                        a = image_data[src_idx + 3]
                    else:
                        r, g, b, a = 0, 0, 0, 255
                else:
                    r, g, b, a = 0, 0, 0, 255
                
                if format_type == 5:  # RGB565
                    value = rgba_to_rgb565(r, g, b)
                    texture_data.extend(struct.pack('<H', value))
                elif format_type == 9:  # RGBA8
                    texture_data.extend([a, b, g, r])
    
    # Build BCLIM file
    result = bytearray()
    
    # Texture data first
    result.extend(texture_data)
    
    # BCLIM header (at end of file)
    # FLIM magic (reversed BCLIM)
    result.extend(b'FLIM')
    result.extend(struct.pack('<H', 0xFEFF))  # BOM
    result.extend(struct.pack('<H', 0x14))    # Header size
    result.extend(struct.pack('<I', 0x02))    # Version
    result.extend(struct.pack('<I', len(result) + 0x28))  # File size
    result.extend(struct.pack('<I', 1))       # Number of sections
    
    # imag section
    result.extend(b'imag')
    result.extend(struct.pack('<I', 0x10))    # Section size
    result.extend(struct.pack('<H', width))
    result.extend(struct.pack('<H', height))
    result.extend(struct.pack('<I', format_type))  # Format
    result.extend(struct.pack('<I', len(texture_data)))
    
    return bytes(result)


def parse_darc(data):
    """Parse DARC archive and extract files"""
    if data[:4] != b'darc':
        raise ValueError("Not a DARC file")
    
    header_size = struct.unpack_from('<I', data, 0x08)[0]
    file_data_offset = struct.unpack_from('<I', data, 0x0C)[0]
    
    # File entry table
    root_offset = struct.unpack_from('<I', data, 0x18)[0]
    root_count = struct.unpack_from('<I', data, 0x1C)[0]
    
    files = {}
    
    # Simplified extraction - read entries
    entry_offset = 0x1C
    name_table_offset = entry_offset + root_count * 12
    
    for i in range(1, root_count):  # Skip root entry
        entry_pos = entry_offset + i * 12
        name_offset = struct.unpack_from('<I', data, entry_pos)[0] & 0x00FFFFFF
        data_offset = struct.unpack_from('<I', data, entry_pos + 4)[0]
        data_size = struct.unpack_from('<I', data, entry_pos + 8)[0]
        
        # Read name
        name_pos = name_table_offset + name_offset
        name_end = data.find(b'\x00', name_pos)
        if name_end == -1:
            name_end = name_pos + 256
        name = data[name_pos:name_end].decode('utf-8', errors='ignore')
        
        if data_size > 0 and data_offset > 0:
            files[name] = data[data_offset:data_offset + data_size]
    
    return files


def create_darc(files):
    """Create DARC archive from dictionary of files"""
    # Build file entries and name table
    entries = []
    name_table = bytearray()
    file_data = bytearray()
    
    # Root entry
    entries.append((0, 0, len(files) + 1))
    
    # Add empty name for root
    name_table.extend(b'\x00\x00')
    
    data_offset = 0
    for name, data in files.items():
        name_offset = len(name_table)
        name_table.extend(name.encode('utf-8'))
        name_table.extend(b'\x00\x00')  # Null terminator + padding
        
        entries.append((name_offset, data_offset, len(data)))
        file_data.extend(data)
        data_offset += len(data)
    
    # Calculate offsets
    header_size = 0x1C
    entries_size = len(entries) * 12
    name_table_size = len(name_table)
    
    # Align name table
    while name_table_size % 4 != 0:
        name_table.append(0)
        name_table_size += 1
    
    file_data_start = header_size + entries_size + name_table_size
    
    # Build DARC
    result = bytearray()
    
    # Header
    result.extend(b'darc')
    result.extend(struct.pack('<H', 0xFEFF))  # BOM
    result.extend(struct.pack('<H', header_size))
    result.extend(struct.pack('<I', 0x01000000))  # Version
    file_size_pos = len(result)
    result.extend(struct.pack('<I', 0))  # File size (placeholder)
    result.extend(struct.pack('<I', file_data_start))  # File data offset
    result.extend(struct.pack('<I', len(file_data)))   # File data size
    result.extend(struct.pack('<I', entries_size))     # Entries size
    
    # Entries
    for name_off, data_off, size in entries:
        if data_off == 0 and size > 1:  # Root
            result.extend(struct.pack('<I', 0x01000000 | name_off))
        else:
            result.extend(struct.pack('<I', name_off))
        result.extend(struct.pack('<I', file_data_start + data_off if size > 0 else 0))
        result.extend(struct.pack('<I', size))
    
    # Name table
    result.extend(name_table)
    
    # File data
    result.extend(file_data)
    
    # Update file size
    struct.pack_into('<I', result, file_size_pos, len(result))
    
    return bytes(result)


def create_boot_splash(top_image_path, bottom_image_path=None, output_path='logo.bcma.lz'):
    """
    Create a boot splash from PNG images.
    
    top_image_path: 400x240 PNG for top screen
    bottom_image_path: 320x240 PNG for bottom screen (optional, will be black if not provided)
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("PIL/Pillow required: pip install Pillow")
    
    # Load and prepare top screen image
    top_img = Image.open(top_image_path).convert('RGBA')
    if top_img.size != (400, 240):
        print(f"Resizing top image from {top_img.size} to 400x240")
        top_img = top_img.resize((400, 240), Image.Resampling.LANCZOS)
    
    top_bclim = create_bclim(top_img.tobytes(), 400, 240, format_type=5)
    
    # Load or create bottom screen image
    if bottom_image_path and os.path.exists(bottom_image_path):
        bottom_img = Image.open(bottom_image_path).convert('RGBA')
        if bottom_img.size != (320, 240):
            print(f"Resizing bottom image from {bottom_img.size} to 320x240")
            bottom_img = bottom_img.resize((320, 240), Image.Resampling.LANCZOS)
    else:
        # Create black bottom screen
        bottom_img = Image.new('RGBA', (320, 240), (0, 0, 0, 255))
    
    bottom_bclim = create_bclim(bottom_img.tobytes(), 320, 240, format_type=5)
    
    # Create DARC archive
    files = {
        'top.bclim': top_bclim,
        'bottom.bclim': bottom_bclim
    }
    
    darc_data = create_darc(files)
    
    # Compress with LZ11
    compressed = lz11_compress(darc_data)
    
    # Write output
    with open(output_path, 'wb') as f:
        f.write(compressed)
    
    print(f"Created boot splash: {output_path}")
    print(f"  Uncompressed size: {len(darc_data)} bytes")
    print(f"  Compressed size: {len(compressed)} bytes")
    
    return output_path


def extract_boot_splash(splash_path, output_dir):
    """Extract images from a boot splash file"""
    with open(splash_path, 'rb') as f:
        data = f.read()
    
    # Decompress if LZ11
    if data[0] == 0x11:
        data = lz11_decompress(data)
    
    # Parse DARC
    files = parse_darc(data)
    
    os.makedirs(output_dir, exist_ok=True)
    
    for name, content in files.items():
        out_path = os.path.join(output_dir, name)
        with open(out_path, 'wb') as f:
            f.write(content)
        print(f"Extracted: {name}")


class BootSplashCreator:
    """High-level interface for boot splash creation"""
    
    def __init__(self):
        self.top_image = None
        self.bottom_image = None
    
    def set_top_screen(self, image_path):
        """Set top screen image (400x240)"""
        self.top_image = image_path
    
    def set_bottom_screen(self, image_path):
        """Set bottom screen image (320x240)"""
        self.bottom_image = image_path
    
    def create(self, output_path):
        """Create the boot splash"""
        if not self.top_image:
            raise ValueError("Top screen image required")
        
        return create_boot_splash(
            self.top_image,
            self.bottom_image,
            output_path
        )


# GBA VC style boot splash - purple gradient with "GAME BOY ADVANCE" text
def create_gba_vc_splash(game_name=None, output_path='logo.bcma.lz'):
    """
    Create a GBA VC style boot splash.
    
    This mimics the official GBA VC boot animation:
    - Purple gradient background
    - "GAME BOY ADVANCE" text
    - Game name (optional)
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("PIL/Pillow required: pip install Pillow")
    
    # Create purple gradient top screen
    top = Image.new('RGBA', (400, 240))
    draw = ImageDraw.Draw(top)
    
    # Purple gradient (similar to GBA VC)
    for y in range(240):
        # Gradient from dark purple to lighter purple
        r = int(48 + (y / 240) * 32)
        g = int(0 + (y / 240) * 16)
        b = int(80 + (y / 240) * 48)
        draw.line([(0, y), (399, y)], fill=(r, g, b, 255))
    
    # Add GBA text (simplified - real font would need TTF)
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 24)
        small_font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
    except:
        font = ImageFont.load_default()
        small_font = font
    
    # "GAME BOY ADVANCE" text
    text = "GAME BOY ADVANCE"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    x = (400 - text_width) // 2
    draw.text((x, 100), text, fill=(255, 255, 255, 255), font=font)
    
    # Game name if provided
    if game_name:
        bbox = draw.textbbox((0, 0), game_name, font=small_font)
        text_width = bbox[2] - bbox[0]
        x = (400 - text_width) // 2
        draw.text((x, 140), game_name, fill=(200, 200, 200, 255), font=small_font)
    
    # Bottom screen - just dark
    bottom = Image.new('RGBA', (320, 240), (16, 0, 32, 255))
    
    # Save temp files and create splash
    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        top_path = f.name
        top.save(top_path)
    
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        bottom_path = f.name
        bottom.save(bottom_path)
    
    try:
        result = create_boot_splash(top_path, bottom_path, output_path)
    finally:
        os.unlink(top_path)
        os.unlink(bottom_path)
    
    return result


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='3DS Boot Splash Creator')
    parser.add_argument('action', choices=['create', 'extract', 'gba-vc'],
                       help='Action to perform')
    parser.add_argument('-t', '--top', help='Top screen image (400x240 PNG)')
    parser.add_argument('-b', '--bottom', help='Bottom screen image (320x240 PNG)')
    parser.add_argument('-o', '--output', required=True, help='Output file')
    parser.add_argument('-n', '--name', help='Game name (for GBA VC style)')
    parser.add_argument('-i', '--input', help='Input file (for extract)')
    
    args = parser.parse_args()
    
    if args.action == 'create':
        if not args.top:
            print("Error: --top required for create")
            sys.exit(1)
        create_boot_splash(args.top, args.bottom, args.output)
    
    elif args.action == 'extract':
        if not args.input:
            print("Error: --input required for extract")
            sys.exit(1)
        extract_boot_splash(args.input, args.output)
    
    elif args.action == 'gba-vc':
        create_gba_vc_splash(args.name, args.output)
        print(f"Created GBA VC style boot splash: {args.output}")
