#!/usr/bin/env python3
"""
3DS Texture Encoder for GBA VC Banner Texture Replacement

This module provides texture encoding for 3DS formats, specifically:
- LA8 (Luminance + Alpha, 16-bit per pixel) - grayscale with alpha
- ETC1A4 (ETC1 compressed + 4-bit alpha) - color with alpha

3DS textures use Morton order (Z-order) tiling in 8x8 blocks.
"""

import struct
from PIL import Image
from typing import Tuple, List
import subprocess
import os


# Morton order lookup table for 8x8 tile
# This maps (x, y) coordinates within an 8x8 tile to linear offset
MORTON_TABLE_8x8 = []

def _init_morton_table():
    """Initialize Morton order lookup table for 8x8 tiles."""
    global MORTON_TABLE_8x8
    MORTON_TABLE_8x8 = [0] * 64
    
    for y in range(8):
        for x in range(8):
            # Interleave bits of x and y for Morton code
            morton = 0
            for i in range(3):  # 3 bits needed for 0-7
                morton |= ((x >> i) & 1) << (2 * i)
                morton |= ((y >> i) & 1) << (2 * i + 1)
            MORTON_TABLE_8x8[y * 8 + x] = morton

_init_morton_table()


def morton_index(x: int, y: int) -> int:
    """
    Calculate Morton index for a pixel within an 8x8 tile.
    
    Args:
        x: X coordinate within tile (0-7)
        y: Y coordinate within tile (0-7)
    
    Returns:
        Morton order index (0-63)
    """
    return MORTON_TABLE_8x8[y * 8 + x]


def get_tile_offset(tile_x: int, tile_y: int, tiles_per_row: int, bytes_per_tile: int) -> int:
    """
    Get byte offset of a tile in the texture data.
    
    Tiles are arranged in row-major order, but pixels within tiles use Morton order.
    
    Args:
        tile_x: Tile column index
        tile_y: Tile row index  
        tiles_per_row: Number of tiles per row
        bytes_per_tile: Number of bytes per tile
    
    Returns:
        Byte offset of tile start
    """
    return (tile_y * tiles_per_row + tile_x) * bytes_per_tile


def encode_la8(image: Image.Image) -> bytes:
    """
    Encode image to LA8 (Luminance + Alpha) format with Morton order tiling.
    
    LA8 format:
    - 2 bytes per pixel: [Alpha, Luminance]
    - Tiled in 8x8 blocks with Morton order
    - Y-axis is flipped (bottom-to-top)
    
    Args:
        image: PIL Image (will be converted to grayscale+alpha)
    
    Returns:
        Encoded texture data bytes
    """
    # Ensure image has alpha channel
    if image.mode != 'LA':
        if image.mode == 'L':
            # Grayscale without alpha - add full alpha
            image = image.convert('LA')
        elif image.mode in ('RGB', 'RGBA', 'P'):
            image = image.convert('RGBA')
            # Convert to grayscale + alpha
            r, g, b, a = image.split()
            # ITU-R BT.601 luma coefficients
            gray = Image.merge('RGB', (r, g, b)).convert('L')
            image = Image.merge('LA', (gray, a))
        else:
            image = image.convert('LA')
    
    width, height = image.size
    
    # Ensure dimensions are multiples of 8 (tile size)
    padded_width = (width + 7) // 8 * 8
    padded_height = (height + 7) // 8 * 8
    
    if padded_width != width or padded_height != height:
        # Create padded image
        padded = Image.new('LA', (padded_width, padded_height), (0, 0))
        padded.paste(image, (0, 0))
        image = padded
        width, height = padded_width, padded_height
    
    # Flip Y axis (3DS textures are bottom-to-top)
    image = image.transpose(Image.FLIP_TOP_BOTTOM)
    
    pixels = image.load()
    
    tiles_per_row = width // 8
    tiles_per_col = height // 8
    bytes_per_tile = 8 * 8 * 2  # 64 pixels * 2 bytes
    
    output = bytearray(tiles_per_row * tiles_per_col * bytes_per_tile)
    
    for tile_y in range(tiles_per_col):
        for tile_x in range(tiles_per_row):
            tile_offset = get_tile_offset(tile_x, tile_y, tiles_per_row, bytes_per_tile)
            
            for py in range(8):
                for px in range(8):
                    # Get pixel from image
                    img_x = tile_x * 8 + px
                    img_y = tile_y * 8 + py
                    
                    lum, alpha = pixels[img_x, img_y]
                    
                    # Get Morton index within tile
                    morton_idx = morton_index(px, py)
                    pixel_offset = tile_offset + morton_idx * 2
                    
                    # LA8 format: [Alpha, Luminance]
                    output[pixel_offset] = alpha
                    output[pixel_offset + 1] = lum
    
    return bytes(output)


def encode_rgb565(image: Image.Image) -> bytes:
    """
    Encode image to RGB565 format with Morton order tiling.
    
    RGB565 format:
    - 2 bytes per pixel: 5-bit R, 6-bit G, 5-bit B (little-endian)
    - Tiled in 8x8 blocks with Morton order
    - Y-axis is flipped
    
    Args:
        image: PIL Image (will be converted to RGB)
    
    Returns:
        Encoded texture data bytes
    """
    image = image.convert('RGB')
    width, height = image.size
    
    # Ensure dimensions are multiples of 8
    padded_width = (width + 7) // 8 * 8
    padded_height = (height + 7) // 8 * 8
    
    if padded_width != width or padded_height != height:
        padded = Image.new('RGB', (padded_width, padded_height), (0, 0, 0))
        padded.paste(image, (0, 0))
        image = padded
        width, height = padded_width, padded_height
    
    # Flip Y axis
    image = image.transpose(Image.FLIP_TOP_BOTTOM)
    
    pixels = image.load()
    
    tiles_per_row = width // 8
    tiles_per_col = height // 8
    bytes_per_tile = 8 * 8 * 2  # 64 pixels * 2 bytes
    
    output = bytearray(tiles_per_row * tiles_per_col * bytes_per_tile)
    
    for tile_y in range(tiles_per_col):
        for tile_x in range(tiles_per_row):
            tile_offset = get_tile_offset(tile_x, tile_y, tiles_per_row, bytes_per_tile)
            
            for py in range(8):
                for px in range(8):
                    img_x = tile_x * 8 + px
                    img_y = tile_y * 8 + py
                    
                    r, g, b = pixels[img_x, img_y]
                    
                    # Convert to RGB565
                    r5 = (r >> 3) & 0x1F
                    g6 = (g >> 2) & 0x3F
                    b5 = (b >> 3) & 0x1F
                    rgb565 = (r5 << 11) | (g6 << 5) | b5
                    
                    morton_idx = morton_index(px, py)
                    pixel_offset = tile_offset + morton_idx * 2
                    
                    # Little-endian
                    output[pixel_offset] = rgb565 & 0xFF
                    output[pixel_offset + 1] = (rgb565 >> 8) & 0xFF
    
    return bytes(output)


def encode_rgba8(image: Image.Image) -> bytes:
    """
    Encode image to RGBA8 format with Morton order tiling.
    
    RGBA8 format:
    - 4 bytes per pixel: [A, B, G, R]
    - Tiled in 8x8 blocks with Morton order
    - Y-axis is flipped
    
    Args:
        image: PIL Image (will be converted to RGBA)
    
    Returns:
        Encoded texture data bytes
    """
    image = image.convert('RGBA')
    width, height = image.size
    
    # Ensure dimensions are multiples of 8
    padded_width = (width + 7) // 8 * 8
    padded_height = (height + 7) // 8 * 8
    
    if padded_width != width or padded_height != height:
        padded = Image.new('RGBA', (padded_width, padded_height), (0, 0, 0, 0))
        padded.paste(image, (0, 0))
        image = padded
        width, height = padded_width, padded_height
    
    # Flip Y axis
    image = image.transpose(Image.FLIP_TOP_BOTTOM)
    
    pixels = image.load()
    
    tiles_per_row = width // 8
    tiles_per_col = height // 8
    bytes_per_tile = 8 * 8 * 4  # 64 pixels * 4 bytes
    
    output = bytearray(tiles_per_row * tiles_per_col * bytes_per_tile)
    
    for tile_y in range(tiles_per_col):
        for tile_x in range(tiles_per_row):
            tile_offset = get_tile_offset(tile_x, tile_y, tiles_per_row, bytes_per_tile)
            
            for py in range(8):
                for px in range(8):
                    img_x = tile_x * 8 + px
                    img_y = tile_y * 8 + py
                    
                    r, g, b, a = pixels[img_x, img_y]
                    
                    morton_idx = morton_index(px, py)
                    pixel_offset = tile_offset + morton_idx * 4
                    
                    # RGBA8 format in 3DS: [A, B, G, R]
                    output[pixel_offset] = a
                    output[pixel_offset + 1] = b
                    output[pixel_offset + 2] = g
                    output[pixel_offset + 3] = r
    
    return bytes(output)


def encode_etc1(image: Image.Image, with_alpha: bool = True, quality: str = 'medium') -> bytes:
    """
    Encode image to ETC1 or ETC1A4 format.
    
    ETC1 is a compressed texture format. ETC1A4 adds 4-bit alpha per pixel.
    This function uses an external tool (tex3ds) if available, otherwise
    falls back to a simple approximation.
    
    Args:
        image: PIL Image
        with_alpha: If True, encode as ETC1A4; if False, encode as ETC1
        quality: Encoding quality ('low', 'medium', 'high')
    
    Returns:
        Encoded texture data bytes
    """
    # Try to use tex3ds if available
    try:
        return _encode_etc1_external(image, with_alpha, quality)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    
    # Fallback: Convert to RGB565 or RGBA8 instead
    # ETC1 encoding is complex and lossy; proper implementation requires
    # significant algorithm work. For banner editing, RGB565/RGBA8 may work
    # depending on the CGFX parser.
    print("Warning: ETC1 encoding not available, falling back to RGBA8")
    return encode_rgba8(image)


def _encode_etc1_external(image: Image.Image, with_alpha: bool, quality: str) -> bytes:
    """Use external tex3ds tool for ETC1 encoding."""
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, 'input.png')
        output_path = os.path.join(tmpdir, 'output.bin')
        
        # Ensure RGBA for alpha support
        if with_alpha:
            image = image.convert('RGBA')
        else:
            image = image.convert('RGB')
        
        image.save(input_path)
        
        fmt = 'etc1a4' if with_alpha else 'etc1'
        
        # Try tex3ds first
        try:
            subprocess.run([
                'tex3ds',
                '-f', fmt,
                '-q', quality,
                '-r',  # Raw output (no header)
                '-o', output_path,
                input_path
            ], check=True, capture_output=True)
            
            with open(output_path, 'rb') as f:
                return f.read()
        except FileNotFoundError:
            pass
        
        # Try 3dstex
        try:
            subprocess.run([
                '3dstex',
                '-r',  # Raw output
                '-o', fmt,
                '-c', {'low': '1', 'medium': '2', 'high': '3'}[quality],
                input_path,
                output_path
            ], check=True, capture_output=True)
            
            with open(output_path, 'rb') as f:
                return f.read()
        except FileNotFoundError:
            pass
        
        raise FileNotFoundError("No ETC1 encoder available (tex3ds or 3dstex)")


# Texture format constants (matching GPU_TEXCOLOR in ctrulib)
class TextureFormat:
    RGBA8 = 0x00
    RGB8 = 0x01
    RGBA5551 = 0x02
    RGB565 = 0x03
    RGBA4 = 0x04
    LA8 = 0x05
    HILO8 = 0x06
    L8 = 0x07
    A8 = 0x08
    LA4 = 0x09
    L4 = 0x0A
    A4 = 0x0B
    ETC1 = 0x0C
    ETC1A4 = 0x0D


def encode_texture(image: Image.Image, format_type: int) -> bytes:
    """
    Encode image to specified 3DS texture format.
    
    Args:
        image: PIL Image
        format_type: TextureFormat constant
    
    Returns:
        Encoded texture data bytes
    """
    if format_type == TextureFormat.LA8:
        return encode_la8(image)
    elif format_type == TextureFormat.RGB565:
        return encode_rgb565(image)
    elif format_type == TextureFormat.RGBA8:
        return encode_rgba8(image)
    elif format_type == TextureFormat.ETC1:
        return encode_etc1(image, with_alpha=False)
    elif format_type == TextureFormat.ETC1A4:
        return encode_etc1(image, with_alpha=True)
    else:
        raise ValueError(f"Unsupported texture format: 0x{format_type:02X}")


def get_texture_size(width: int, height: int, format_type: int) -> int:
    """
    Calculate encoded texture size in bytes.
    
    Args:
        width: Texture width (must be multiple of 8)
        height: Texture height (must be multiple of 8)
        format_type: TextureFormat constant
    
    Returns:
        Size in bytes
    """
    # Round up to multiple of 8
    width = (width + 7) // 8 * 8
    height = (height + 7) // 8 * 8
    num_pixels = width * height
    
    bytes_per_pixel = {
        TextureFormat.RGBA8: 4,
        TextureFormat.RGB8: 3,
        TextureFormat.RGBA5551: 2,
        TextureFormat.RGB565: 2,
        TextureFormat.RGBA4: 2,
        TextureFormat.LA8: 2,
        TextureFormat.HILO8: 2,
        TextureFormat.L8: 1,
        TextureFormat.A8: 1,
        TextureFormat.LA4: 1,
        TextureFormat.L4: 0.5,
        TextureFormat.A4: 0.5,
        # ETC1: 4x4 block = 8 bytes
        TextureFormat.ETC1: 0.5,  # 8 bytes per 16 pixels
        # ETC1A4: 4x4 block = 8 bytes ETC1 + 8 bytes alpha
        TextureFormat.ETC1A4: 1,  # 16 bytes per 16 pixels
    }
    
    bpp = bytes_per_pixel.get(format_type, 2)
    return int(num_pixels * bpp)


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) < 4:
        print("Usage: texture_encoder.py <input.png> <output.bin> <format>")
        print("Formats: la8, rgb565, rgba8, etc1, etc1a4")
        sys.exit(1)
    
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    fmt_name = sys.argv[3].lower()
    
    format_map = {
        'la8': TextureFormat.LA8,
        'rgb565': TextureFormat.RGB565,
        'rgba8': TextureFormat.RGBA8,
        'etc1': TextureFormat.ETC1,
        'etc1a4': TextureFormat.ETC1A4,
    }
    
    if fmt_name not in format_map:
        print(f"Unknown format: {fmt_name}")
        sys.exit(1)
    
    image = Image.open(input_path)
    data = encode_texture(image, format_map[fmt_name])
    
    with open(output_path, 'wb') as f:
        f.write(data)
    
    print(f"Encoded {image.size[0]}x{image.size[1]} -> {len(data)} bytes ({fmt_name})")
