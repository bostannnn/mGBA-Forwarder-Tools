#!/usr/bin/env python3
"""
GBA VC Banner Texture Patcher

This tool patches textures in GBA VC 3D banners (CGFX/BCMDL files).
It works similarly to NSUI by using known offsets in the GBA VC template
to replace texture data.

Key offsets in GBA VC banner files:
- Texture format byte: 0x2CA0 in CGFX (identifies LA8=0x05, ETC1A4=0x0D)
- Texture data follows shortly after in the TXOB section

The banner consists of:
- Header (CGFX magic, size info)
- DATA section containing:
  - DICT (dictionary with names)
  - TXOB (texture object) - contains format byte and texture data
  - Other model data
"""

import struct
import os
import sys
from typing import Optional, Tuple, List
from PIL import Image

# Import our texture encoder
try:
    from texture_encoder import encode_la8, encode_etc1, TextureFormat, get_texture_size
except ImportError:
    # Running as standalone script
    import importlib.util
    spec = importlib.util.spec_from_file_location("texture_encoder", 
        os.path.join(os.path.dirname(__file__), "texture_encoder.py"))
    texture_encoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(texture_encoder)
    encode_la8 = texture_encoder.encode_la8
    encode_etc1 = texture_encoder.encode_etc1
    TextureFormat = texture_encoder.TextureFormat
    get_texture_size = texture_encoder.get_texture_size


class CGFXParser:
    """
    Parser for CGFX (CTR GFX) files used in 3DS banners.
    
    CGFX structure:
    - Header: 'CGFX' magic, BOM, version, file size, num sections
    - DATA section: Contains models, textures, etc.
    
    Within DATA, textures are in TXOB (Texture Object) chunks.
    """
    
    def __init__(self, data: bytes):
        self.data = bytearray(data)
        self.textures = []
        self._parse()
    
    def _parse(self):
        """Parse CGFX structure to find textures."""
        # Check magic
        magic = self.data[0:4]
        if magic not in (b'CGFX', b'BCRES'):
            raise ValueError(f"Not a CGFX file: {magic}")
        
        # Parse header
        bom = struct.unpack_from('<H', self.data, 4)[0]
        self.little_endian = (bom == 0xFEFF)
        
        header_size = struct.unpack_from('<H', self.data, 6)[0]
        version = struct.unpack_from('<I', self.data, 8)[0]
        file_size = struct.unpack_from('<I', self.data, 12)[0]
        num_sections = struct.unpack_from('<I', self.data, 16)[0]
        
        # Find DATA section
        offset = header_size
        for _ in range(num_sections):
            section_magic = self.data[offset:offset+4]
            section_size = struct.unpack_from('<I', self.data, offset + 4)[0]
            
            if section_magic == b'DATA':
                self._parse_data_section(offset, section_size)
            
            offset += section_size
    
    def _parse_data_section(self, base_offset: int, size: int):
        """Parse DATA section to find texture objects."""
        # DATA section contains a dictionary pointing to various resources
        # We'll scan for texture format patterns instead of full parsing
        
        # Look for texture format bytes followed by dimension data
        # GBA VC banner texture is 256x64, format LA8 (0x05) or ETC1A4 (0x0D)
        
        for offset in range(base_offset, base_offset + size - 32):
            # Check for potential TXOB/texture header
            # Format byte at specific offsets relative to texture sections
            
            # Known pattern: texture dimensions followed by format
            # Try to find 256x64 dimensions (0x100, 0x40)
            
            try:
                # Check for dimension pattern (width=256, height=64)
                w = struct.unpack_from('<H', self.data, offset)[0]
                h = struct.unpack_from('<H', self.data, offset + 2)[0]
                
                if w == 256 and h == 64:
                    # Potential texture header found
                    # Look nearby for format byte
                    for fmt_offset in range(max(0, offset - 32), min(len(self.data), offset + 32)):
                        fmt = self.data[fmt_offset]
                        if fmt in (TextureFormat.LA8, TextureFormat.ETC1A4, 
                                   TextureFormat.RGB565, TextureFormat.RGBA8):
                            # Check if this looks like a valid texture entry
                            texture_info = {
                                'dimension_offset': offset,
                                'format_offset': fmt_offset,
                                'format': fmt,
                                'width': w,
                                'height': h,
                            }
                            # Try to find texture data
                            texture_info['data_offset'] = self._find_texture_data(
                                offset, w, h, fmt)
                            if texture_info['data_offset']:
                                self.textures.append(texture_info)
                                return  # Found the main texture
            except:
                continue
    
    def _find_texture_data(self, near_offset: int, width: int, height: int, 
                          format_type: int) -> Optional[int]:
        """
        Find the offset of texture data in the file.
        
        Texture data typically follows the texture metadata.
        """
        expected_size = get_texture_size(width, height, format_type)
        
        # Search forward from the dimension offset
        for offset in range(near_offset, min(len(self.data) - expected_size, 
                                              near_offset + 0x1000)):
            # Check if this could be the start of texture data
            # For LA8, first bytes would be alpha, luminance pairs
            # We can't easily validate without knowing the image content
            
            # Use heuristic: texture data is usually aligned
            if offset % 8 == 0:
                # Check if there's enough data
                if offset + expected_size <= len(self.data):
                    return offset
        
        return None


def find_texture_offset_by_format(data: bytes, format_byte: int) -> Optional[int]:
    """
    Find texture format byte offset in CGFX data.
    
    GBA VC banners have a known structure where the format byte is at 0x2CA0.
    This function tries that offset first, then searches if not found.
    """
    # Known offset for GBA VC CGFX (v27 style)
    KNOWN_FORMAT_OFFSET = 0x2CA0
    
    if len(data) > KNOWN_FORMAT_OFFSET:
        if data[KNOWN_FORMAT_OFFSET] == format_byte or data[KNOWN_FORMAT_OFFSET] in (0x05, 0x0D):
            return KNOWN_FORMAT_OFFSET
    
    # Search for format byte in likely locations
    for offset in range(0x100, min(len(data), 0x10000)):
        if data[offset] == format_byte:
            # Validate: should have dimension data nearby
            try:
                # Check for 256x64 dimensions within 64 bytes
                for dim_offset in range(max(0, offset - 64), min(len(data) - 4, offset + 64)):
                    w = struct.unpack_from('<H', data, dim_offset)[0]
                    h = struct.unpack_from('<H', data, dim_offset + 2)[0]
                    if w == 256 and h == 64:
                        return offset
            except:
                continue
    
    return None


def find_texture_data_offset(data: bytes, format_offset: int, width: int, height: int,
                            format_type: int) -> Optional[int]:
    """
    Find the texture data offset given format offset and dimensions.
    
    For GBA VC banners, texture data typically starts around 0x2CC0-0x2D00.
    """
    expected_size = get_texture_size(width, height, format_type)
    
    # Known offset range for GBA VC
    LIKELY_DATA_START = 0x2CC0
    LIKELY_DATA_END = 0x3000
    
    # Check known offset first
    if format_offset:
        # Data usually starts shortly after format info (within 0x100 bytes)
        search_start = format_offset + 0x10
    else:
        search_start = LIKELY_DATA_START
    
    # Search for texture data
    for offset in range(search_start, min(len(data) - expected_size, search_start + 0x1000)):
        # Check alignment (texture data is usually 8-byte aligned)
        if offset % 8 == 0:
            # Verify we have enough space
            if offset + expected_size <= len(data):
                return offset
    
    return None


def analyze_banner(filepath: str) -> dict:
    """
    Analyze a GBA VC banner file to find texture locations.
    
    Returns dict with texture info and offsets.
    """
    with open(filepath, 'rb') as f:
        data = f.read()
    
    info = {
        'filepath': filepath,
        'size': len(data),
        'magic': data[0:4].decode('ascii', errors='replace'),
        'format_offset': None,
        'format_type': None,
        'data_offset': None,
        'texture_width': 256,
        'texture_height': 64,
    }
    
    # Find format byte
    format_offset = find_texture_offset_by_format(data, 0x05)  # LA8
    if format_offset is None:
        format_offset = find_texture_offset_by_format(data, 0x0D)  # ETC1A4
    
    if format_offset:
        info['format_offset'] = format_offset
        info['format_type'] = data[format_offset]
        
        # Find data offset
        data_offset = find_texture_data_offset(data, format_offset, 256, 64, 
                                                info['format_type'])
        info['data_offset'] = data_offset
        
        if data_offset:
            info['texture_size'] = get_texture_size(256, 64, info['format_type'])
    
    return info


def patch_banner_texture(banner_path: str, image_path: str, output_path: str,
                        color: bool = False, verbose: bool = False) -> bool:
    """
    Patch a GBA VC banner with a new texture.
    
    Args:
        banner_path: Path to original banner.cgfx or bannerX.bcmdl
        image_path: Path to new texture image (256x64 PNG)
        output_path: Path to save patched banner
        color: If True, convert to ETC1A4 (color); if False, use LA8 (grayscale)
        verbose: Print detailed progress
    
    Returns:
        True if successful, False otherwise
    """
    # Load banner
    with open(banner_path, 'rb') as f:
        data = bytearray(f.read())
    
    # Analyze to find offsets
    info = analyze_banner(banner_path)
    
    if verbose:
        print(f"Analyzing {banner_path}:")
        print(f"  Magic: {info['magic']}")
        print(f"  Size: {info['size']} bytes")
        print(f"  Format offset: {hex(info['format_offset']) if info['format_offset'] else 'Not found'}")
        print(f"  Format type: {hex(info['format_type']) if info['format_type'] else 'Not found'}")
        print(f"  Data offset: {hex(info['data_offset']) if info['data_offset'] else 'Not found'}")
    
    if not info['format_offset']:
        print(f"Error: Could not find texture format offset in {banner_path}")
        return False
    
    if not info['data_offset']:
        print(f"Error: Could not find texture data offset in {banner_path}")
        print("Trying hardcoded offsets for GBA VC...")
        
        # Use hardcoded offsets for GBA VC
        # These are typical offsets in GBA VC banners
        HARDCODED_OFFSETS = [
            (0x2CA0, 0x2CC0),  # Common CGFX offset
            (0x2C9C, 0x2CC0),  # Alternate
            (0x400, 0x500),    # BCMDL style
        ]
        
        for fmt_off, data_off in HARDCODED_OFFSETS:
            if fmt_off < len(data) and data_off < len(data):
                info['format_offset'] = fmt_off
                info['data_offset'] = data_off
                break
        
        if not info['data_offset']:
            print("Error: Could not determine texture data offset")
            return False
    
    # Load and encode new texture
    try:
        image = Image.open(image_path)
    except Exception as e:
        print(f"Error loading image: {e}")
        return False
    
    # Resize if needed
    if image.size != (256, 64):
        if verbose:
            print(f"Resizing image from {image.size} to (256, 64)")
        image = image.resize((256, 64), Image.Resampling.LANCZOS)
    
    # Encode texture
    if color:
        new_format = TextureFormat.ETC1A4
        try:
            texture_data = encode_etc1(image, with_alpha=True)
        except Exception as e:
            print(f"Warning: ETC1 encoding failed ({e}), using RGBA8")
            # Fallback - note: may not be compatible with all parsers
            texture_data = encode_la8(image.convert('LA'))
            new_format = TextureFormat.LA8
            color = False
    else:
        new_format = TextureFormat.LA8
        texture_data = encode_la8(image)
    
    expected_size = get_texture_size(256, 64, new_format)
    
    if verbose:
        print(f"Encoded texture: {len(texture_data)} bytes (expected: {expected_size})")
    
    # Check if we have enough space
    data_end = info['data_offset'] + expected_size
    if data_end > len(data):
        print(f"Warning: Texture data would exceed file size")
        print(f"  File size: {len(data)}, Data end: {data_end}")
    
    # Patch format byte
    old_format = data[info['format_offset']]
    data[info['format_offset']] = new_format
    if verbose:
        print(f"Patched format byte at {hex(info['format_offset'])}: "
              f"{hex(old_format)} -> {hex(new_format)}")
    
    # Patch texture data
    data[info['data_offset']:info['data_offset'] + len(texture_data)] = texture_data
    if verbose:
        print(f"Patched texture data at {hex(info['data_offset'])}: {len(texture_data)} bytes")
    
    # Save
    with open(output_path, 'wb') as f:
        f.write(data)
    
    if verbose:
        print(f"Saved to {output_path}")
    
    return True


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='GBA VC Banner Texture Patcher',
        epilog='Replace textures in GBA VC 3D banners (CGFX/BCMDL files)'
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Analyze command
    analyze_parser = subparsers.add_parser('analyze', help='Analyze banner file')
    analyze_parser.add_argument('banner', help='Banner file to analyze')
    
    # Patch command
    patch_parser = subparsers.add_parser('patch', help='Patch banner texture')
    patch_parser.add_argument('banner', help='Banner file to patch')
    patch_parser.add_argument('image', help='New texture image (256x64 PNG)')
    patch_parser.add_argument('-o', '--output', required=True, help='Output file')
    patch_parser.add_argument('-c', '--color', action='store_true',
                             help='Enable color (ETC1A4 instead of LA8)')
    patch_parser.add_argument('-v', '--verbose', action='store_true',
                             help='Verbose output')
    
    args = parser.parse_args()
    
    if args.command == 'analyze':
        info = analyze_banner(args.banner)
        print(f"Banner Analysis: {args.banner}")
        print(f"  File size: {info['size']} bytes")
        print(f"  Magic: {info['magic']}")
        if info['format_offset']:
            print(f"  Format offset: {hex(info['format_offset'])}")
            print(f"  Format type: {hex(info['format_type'])} "
                  f"({'LA8' if info['format_type'] == 0x05 else 'ETC1A4' if info['format_type'] == 0x0D else 'Unknown'})")
        else:
            print("  Format offset: Not found")
        if info['data_offset']:
            print(f"  Data offset: {hex(info['data_offset'])}")
            print(f"  Texture size: {info.get('texture_size', 'Unknown')} bytes")
        else:
            print("  Data offset: Not found")
    
    elif args.command == 'patch':
        success = patch_banner_texture(
            args.banner, args.image, args.output,
            color=args.color, verbose=args.verbose
        )
        if success:
            print(f"Successfully patched banner to {args.output}")
        else:
            print("Failed to patch banner")
            sys.exit(1)
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
