#!/usr/bin/env python3
"""
GBA VC Banner Editor

Edits textures in GBA Virtual Console style 3D banners.
The GBA VC banner contains a 3D model of a GBA console with:
- Screen texture (the game screenshot displayed on the GBA screen)
- Title plate texture (the text banner below)

Banner structure (banner.bnr):
- CBMD header (0x88 bytes)
- CGFX blocks (LZ11 compressed) for each region
- BCWAV audio at the end

This tool works by:
1. Extracting the banner components using 3dstool
2. Decompressing CGFX with LZ11
3. Locating and replacing texture data at known offsets
4. Recompressing and rebuilding the banner
"""

import os
import sys
import struct
import subprocess
import shutil
from pathlib import Path
import tempfile

# 3DS texture tile order (Morton/Z-order)
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
    """Decompress LZ11 data (Nintendo's variant of LZ77)"""
    if len(data) < 4:
        return data
    
    # Check for LZ11 magic (0x11 at start)
    if data[0] != 0x11:
        return data  # Not compressed
    
    # Get decompressed size from header
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
                # Compressed block
                if pos + 1 >= len(data):
                    break
                
                byte1 = data[pos]
                byte2 = data[pos + 1]
                pos += 2
                
                # Determine length encoding type
                indicator = byte1 >> 4
                
                if indicator == 0:
                    # 8-bit length (3-byte header)
                    if pos >= len(data):
                        break
                    byte3 = data[pos]
                    pos += 1
                    length = ((byte1 & 0x0F) << 4 | (byte2 >> 4)) + 0x11
                    disp = ((byte2 & 0x0F) << 8 | byte3) + 1
                elif indicator == 1:
                    # 16-bit length (4-byte header)
                    if pos + 1 >= len(data):
                        break
                    byte3 = data[pos]
                    byte4 = data[pos + 1]
                    pos += 2
                    length = ((byte1 & 0x0F) << 12 | byte2 << 4 | (byte3 >> 4)) + 0x111
                    disp = ((byte3 & 0x0F) << 8 | byte4) + 1
                else:
                    # Standard 2-byte header
                    length = indicator + 1
                    disp = ((byte1 & 0x0F) << 8 | byte2) + 1
                
                # Copy from back-reference
                for _ in range(length):
                    if len(result) < disp:
                        result.append(0)
                    else:
                        result.append(result[-disp])
            else:
                # Literal byte
                result.append(data[pos])
                pos += 1
    
    return bytes(result)


def lz11_compress(data):
    """Simple LZ11 compression (not optimal but functional)"""
    result = bytearray()
    
    # Header: magic byte + 24-bit size
    size = len(data)
    result.append(0x11)
    result.append(size & 0xFF)
    result.append((size >> 8) & 0xFF)
    result.append((size >> 16) & 0xFF)
    
    pos = 0
    while pos < len(data):
        flag_pos = len(result)
        result.append(0)  # Placeholder for flags
        flags = 0
        
        for i in range(8):
            if pos >= len(data):
                break
            
            # Try to find a match in the sliding window
            best_match_len = 0
            best_match_disp = 0
            
            # Search in sliding window (max 4096 bytes back)
            search_start = max(0, pos - 4096)
            for search_pos in range(search_start, pos):
                match_len = 0
                while (pos + match_len < len(data) and 
                       match_len < 0x10110 and
                       data[search_pos + (match_len % (pos - search_pos))] == data[pos + match_len]):
                    match_len += 1
                
                if match_len > best_match_len:
                    best_match_len = match_len
                    best_match_disp = pos - search_pos
            
            # Minimum match length is 3
            if best_match_len >= 3:
                flags |= (0x80 >> i)
                disp = best_match_disp - 1
                
                if best_match_len <= 0x10:
                    # Standard 2-byte header
                    length = best_match_len - 1
                    result.append((length << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                elif best_match_len <= 0x110:
                    # 8-bit length (3-byte header)
                    length = best_match_len - 0x11
                    result.append((length >> 4) & 0x0F)
                    result.append(((length & 0x0F) << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                else:
                    # 16-bit length (4-byte header)
                    length = best_match_len - 0x111
                    result.append(0x10 | ((length >> 12) & 0x0F))
                    result.append((length >> 4) & 0xFF)
                    result.append(((length & 0x0F) << 4) | ((disp >> 8) & 0x0F))
                    result.append(disp & 0xFF)
                
                pos += best_match_len
            else:
                # Literal byte
                result.append(data[pos])
                pos += 1
        
        result[flag_pos] = flags
    
    return bytes(result)


def rgba8_to_3ds_texture(image_data, width, height):
    """
    Convert RGBA8 image data to 3DS texture format (8x8 tiled, Morton order)
    
    image_data: bytes in RGBA format (4 bytes per pixel)
    width, height: dimensions (must be multiples of 8)
    """
    if width % 8 != 0 or height % 8 != 0:
        raise ValueError("Dimensions must be multiples of 8")
    
    result = bytearray()
    
    # Process 8x8 tiles
    for tile_y in range(height // 8):
        for tile_x in range(width // 8):
            # Each 8x8 tile
            for morton_idx in range(64):
                # Get actual position within tile from Morton order
                pixel_idx = TILE_ORDER[morton_idx]
                local_x = pixel_idx % 8
                local_y = pixel_idx // 8
                
                # Global position
                x = tile_x * 8 + local_x
                y = tile_y * 8 + local_y
                
                # Get pixel from source (flip Y for 3DS)
                src_y = height - 1 - y
                src_idx = (src_y * width + x) * 4
                
                if src_idx + 3 < len(image_data):
                    r = image_data[src_idx]
                    g = image_data[src_idx + 1]
                    b = image_data[src_idx + 2]
                    a = image_data[src_idx + 3]
                else:
                    r, g, b, a = 0, 0, 0, 255
                
                # 3DS RGBA8 is stored as ABGR
                result.extend([a, b, g, r])
    
    return bytes(result)


def png_to_rgba(png_path):
    """Load PNG and return RGBA data, width, height"""
    try:
        from PIL import Image
        img = Image.open(png_path).convert('RGBA')
        return img.tobytes(), img.width, img.height
    except ImportError:
        raise RuntimeError("PIL/Pillow required: pip install Pillow")


def find_texture_offset(cgfx_data, texture_name=None, width=None, height=None):
    """
    Find texture data offset in CGFX file.
    
    For GBA VC banners, the main textures are:
    - Screen texture (usually 128x128 or 256x128)
    - Title plate texture
    
    Returns: (offset, width, height, format) or None
    """
    # CGFX magic check
    if cgfx_data[:4] != b'CGFX':
        return None
    
    # This is a simplified search - real implementation would parse CGFX properly
    # Looking for TXOB (texture object) markers
    
    pos = 0
    while pos < len(cgfx_data) - 4:
        # Look for texture format markers
        # Common 3DS texture formats in CGFX:
        # RGBA8: format 0x0003
        # RGB8:  format 0x0002
        # ETC1:  format 0x000B
        
        # This is heuristic - we look for size patterns
        if pos + 16 < len(cgfx_data):
            # Check for potential texture header pattern
            potential_width = struct.unpack_from('<H', cgfx_data, pos)[0]
            potential_height = struct.unpack_from('<H', cgfx_data, pos + 2)[0]
            
            if (potential_width in [64, 128, 256, 512] and 
                potential_height in [64, 128, 256, 512]):
                if width is None or (potential_width == width and potential_height == height):
                    # Found potential texture
                    return (pos, potential_width, potential_height, 'RGBA8')
        
        pos += 1
    
    return None


class GBAVCBanner:
    """Editor for GBA Virtual Console style 3D banners"""
    
    def __init__(self, banner_path=None):
        self.banner_path = banner_path
        self.cbmd_header = None
        self.cgfx_data = {}  # region -> decompressed CGFX
        self.bcwav_data = None
        self.temp_dir = None
        
        if banner_path and os.path.exists(banner_path):
            self.load(banner_path)
    
    def load(self, banner_path):
        """Load and parse a banner.bnr file"""
        with open(banner_path, 'rb') as f:
            data = f.read()
        
        # Check for CBMD magic
        if data[:4] != b'CBMD':
            raise ValueError("Not a valid CBMD banner file")
        
        # Parse CBMD header
        self.cbmd_header = data[:0x88]
        
        # Get CGFX offsets from header
        common_cgfx_offset = struct.unpack_from('<I', data, 0x08)[0]
        bcwav_offset = struct.unpack_from('<I', data, 0x84)[0]
        
        # Region-specific CGFX offsets (0x0C to 0x3F)
        region_offsets = []
        for i in range(13):
            offset = struct.unpack_from('<I', data, 0x0C + i * 4)[0]
            region_offsets.append(offset)
        
        # Extract BCWAV
        if bcwav_offset > 0 and bcwav_offset < len(data):
            self.bcwav_data = data[bcwav_offset:]
        
        # Extract and decompress common CGFX
        if common_cgfx_offset > 0:
            # Find end of CGFX (next non-zero offset or BCWAV)
            cgfx_end = bcwav_offset if bcwav_offset > common_cgfx_offset else len(data)
            for offset in region_offsets:
                if offset > common_cgfx_offset and offset < cgfx_end:
                    cgfx_end = offset
            
            compressed_cgfx = data[common_cgfx_offset:cgfx_end]
            self.cgfx_data['common'] = lz11_decompress(compressed_cgfx)
        
        # Extract region-specific CGFX
        region_names = [
            'EUR_EN', 'EUR_FR', 'EUR_DE', 'EUR_IT', 'EUR_ES', 
            'EUR_NL', 'EUR_PT', 'EUR_RU', 'JPN_JP', 'USA_EN',
            'USA_FR', 'USA_ES', 'USA_PT'
        ]
        
        for i, offset in enumerate(region_offsets):
            if offset > 0:
                # Find end
                cgfx_end = bcwav_offset if bcwav_offset > offset else len(data)
                for other_offset in region_offsets:
                    if other_offset > offset and other_offset < cgfx_end:
                        cgfx_end = other_offset
                
                compressed_cgfx = data[offset:cgfx_end]
                self.cgfx_data[region_names[i]] = lz11_decompress(compressed_cgfx)
    
    def extract_with_3dstool(self, banner_path, output_dir):
        """Use 3dstool to extract banner components"""
        os.makedirs(output_dir, exist_ok=True)
        
        cmd = [
            '3dstool', '-xvtf', 'banner', banner_path,
            '--banner-dir', output_dir
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"3dstool extraction failed: {e}")
            return False
    
    def rebuild_with_3dstool(self, input_dir, output_path):
        """Use 3dstool to rebuild banner from components"""
        cmd = [
            '3dstool', '-cvtf', 'banner', output_path,
            '--banner-dir', input_dir
        ]
        
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"3dstool rebuild failed: {e}")
            return False
    
    def replace_screen_texture(self, image_path, region='USA_EN'):
        """
        Replace the screen texture in the banner.
        
        The screen texture is what appears on the GBA screen in the 3D model.
        Typically 128x128 or similar size.
        """
        # Load and convert image
        rgba_data, width, height = png_to_rgba(image_path)
        
        # Ensure dimensions are power of 2 and multiples of 8
        target_width = 128  # GBA screen texture is typically 128x128
        target_height = 128
        
        # Resize if needed
        from PIL import Image
        img = Image.open(image_path).convert('RGBA')
        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        rgba_data = img.tobytes()
        
        # Convert to 3DS texture format
        texture_data = rgba8_to_3ds_texture(rgba_data, target_width, target_height)
        
        # Find and replace texture in CGFX
        cgfx_key = region if region in self.cgfx_data else 'common'
        if cgfx_key not in self.cgfx_data:
            raise ValueError(f"No CGFX data for region {region}")
        
        cgfx = bytearray(self.cgfx_data[cgfx_key])
        
        # Find texture offset (this is simplified - real implementation needs CGFX parsing)
        texture_info = find_texture_offset(cgfx, width=target_width, height=target_height)
        
        if texture_info:
            offset, w, h, fmt = texture_info
            # Calculate texture data size
            tex_size = w * h * 4  # RGBA8
            # Replace texture data
            if offset + tex_size <= len(cgfx):
                cgfx[offset:offset + tex_size] = texture_data[:tex_size]
                self.cgfx_data[cgfx_key] = bytes(cgfx)
                return True
        
        print("Warning: Could not find screen texture offset")
        return False
    
    def save(self, output_path):
        """Save the modified banner"""
        # This requires rebuilding the CBMD structure
        # For now, use 3dstool approach
        
        result = bytearray()
        
        # CBMD header
        result.extend(self.cbmd_header)
        
        # Compress and add CGFX blocks
        # Update offsets in header as we go
        
        current_offset = 0x88  # After header
        
        # Common CGFX
        if 'common' in self.cgfx_data:
            compressed = lz11_compress(self.cgfx_data['common'])
            struct.pack_into('<I', result, 0x08, current_offset)
            result.extend(compressed)
            current_offset += len(compressed)
        
        # Region CGFX (simplified - just using common for now)
        # Real implementation would handle each region
        
        # BCWAV
        if self.bcwav_data:
            # Align to 4 bytes
            while len(result) % 4 != 0:
                result.append(0)
            current_offset = len(result)
            struct.pack_into('<I', result, 0x84, current_offset)
            result.extend(self.bcwav_data)
        
        with open(output_path, 'wb') as f:
            f.write(result)


def create_simple_banner(screen_image_path, title_text, output_path, 
                         template_path=None, audio_path=None):
    """
    Create a GBA VC style banner with custom screen image and title.
    
    If template_path is provided, use it as base. Otherwise create a simple 2D banner.
    """
    # For simple case without 3D template, fall back to bannertool
    if template_path is None or not os.path.exists(template_path):
        # Create 2D banner using bannertool
        print("No 3D template provided, creating 2D banner...")
        
        cmd = ['bannertool', 'makebanner']
        cmd.extend(['-i', screen_image_path])
        
        if audio_path and os.path.exists(audio_path):
            cmd.extend(['-a', audio_path])
        else:
            # Create silent audio
            silent_wav = '/tmp/silent.wav'
            try:
                subprocess.run(['sox', '-n', '-r', '22050', '-c', '1', '-b', '16',
                              silent_wav, 'trim', '0.0', '1.0'],
                             capture_output=True)
                cmd.extend(['-a', silent_wav])
            except:
                print("Warning: Could not create silent audio")
        
        cmd.extend(['-o', output_path])
        
        try:
            subprocess.run(cmd, check=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"bannertool failed: {e}")
            return False
    
    # Use template and modify
    banner = GBAVCBanner(template_path)
    banner.replace_screen_texture(screen_image_path)
    banner.save(output_path)
    return True


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='GBA VC Banner Editor')
    parser.add_argument('action', choices=['extract', 'build', 'edit'],
                       help='Action to perform')
    parser.add_argument('-i', '--input', required=True, help='Input file')
    parser.add_argument('-o', '--output', required=True, help='Output file/directory')
    parser.add_argument('-s', '--screen', help='Screen image (PNG)')
    parser.add_argument('-t', '--template', help='Banner template')
    parser.add_argument('-a', '--audio', help='Audio file (WAV)')
    
    args = parser.parse_args()
    
    if args.action == 'extract':
        # Extract banner components
        banner = GBAVCBanner()
        if banner.extract_with_3dstool(args.input, args.output):
            print(f"Extracted to {args.output}")
        else:
            # Try manual extraction
            banner.load(args.input)
            os.makedirs(args.output, exist_ok=True)
            for region, data in banner.cgfx_data.items():
                with open(os.path.join(args.output, f'banner_{region}.cgfx'), 'wb') as f:
                    f.write(data)
            if banner.bcwav_data:
                with open(os.path.join(args.output, 'banner.bcwav'), 'wb') as f:
                    f.write(banner.bcwav_data)
            print(f"Manually extracted to {args.output}")
    
    elif args.action == 'build':
        # Build banner from screen image
        if not args.screen:
            print("Error: --screen required for build")
            sys.exit(1)
        
        success = create_simple_banner(
            args.screen,
            "Game",  # Title text
            args.output,
            args.template,
            args.audio
        )
        
        if success:
            print(f"Created banner: {args.output}")
        else:
            print("Failed to create banner")
            sys.exit(1)
    
    elif args.action == 'edit':
        # Edit existing banner
        if not args.screen:
            print("Error: --screen required for edit")
            sys.exit(1)
        
        banner = GBAVCBanner(args.input)
        if banner.replace_screen_texture(args.screen):
            banner.save(args.output)
            print(f"Edited banner saved: {args.output}")
        else:
            print("Failed to edit banner")
            sys.exit(1)
