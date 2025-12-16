#!/usr/bin/env python3
"""
3DS Banner Editor - Reverse engineered from NSUI/ba-GUI-nnertool

Banner Structure (banner.bnr):
- Extracted using: 3dstool -x -t banner --banner-dir <dir> -f banner.bnr
- Contains:
  - banner.cbmd: CBMD container/header
  - banner.bcwav: Audio (up to 3 seconds, stereo)
  - banner0.bcmdl through banner13.bcmdl: Region-specific CGFX/model files

Region mapping for bcmdl files:
  banner0.bcmdl  = Common/Default
  banner1.bcmdl  = EUR_EN (English - Europe)
  banner2.bcmdl  = EUR_FR (French - Europe)
  banner3.bcmdl  = EUR_GE (German - Europe)
  banner4.bcmdl  = EUR_IT (Italian - Europe)
  banner5.bcmdl  = EUR_SP (Spanish - Europe)
  banner6.bcmdl  = EUR_DU (Dutch - Europe)
  banner7.bcmdl  = EUR_PO (Portuguese - Europe)
  banner8.bcmdl  = EUR_RU (Russian - Europe)
  banner9.bcmdl  = JPN_JP (Japanese)
  banner10.bcmdl = USA_EN (English - USA)
  banner11.bcmdl = USA_FR (French - USA)
  banner12.bcmdl = USA_SP (Spanish - USA)
  banner13.bcmdl = USA_PO (Portuguese - USA)

BCMDL Texture Format (offset 0x400 / 1024):
  0x05 = LA8 (Grayscale with Alpha) - default NSUI
  0x0D = ETC1A4 (ETC1 with 4-bit Alpha) - for colored textures

Texture locations in GBA VC banner BCMDL:
  - Screen texture: 128x128 or 256x128 (game screenshot on GBA screen)
  - Title plate: 256x64 (text banner below GBA model)
"""

import os
import sys
import struct
import subprocess
import shutil
import tempfile
from pathlib import Path

# Region codes
REGION_CODES = {
    0: 'COMMON',
    1: 'EUR_EN',
    2: 'EUR_FR',
    3: 'EUR_GE',
    4: 'EUR_IT',
    5: 'EUR_SP',
    6: 'EUR_DU',
    7: 'EUR_PO',
    8: 'EUR_RU',
    9: 'JPN_JP',
    10: 'USA_EN',
    11: 'USA_FR',
    12: 'USA_SP',
    13: 'USA_PO',
}

# Texture format codes
TEXTURE_FORMATS = {
    0x00: 'RGBA8',
    0x01: 'RGB8',
    0x02: 'RGBA5551',
    0x03: 'RGB565',
    0x04: 'RGBA4',
    0x05: 'LA8',      # Grayscale + Alpha (NSUI default for GBA VC)
    0x06: 'HILO8',
    0x07: 'L8',
    0x08: 'A8',
    0x09: 'LA4',
    0x0A: 'L4',
    0x0B: 'A4',
    0x0C: 'ETC1',
    0x0D: 'ETC1A4',   # ETC1 with 4-bit alpha (for colored banners)
}


def run_3dstool(args, check=True):
    """Run 3dstool with given arguments"""
    cmd = ['3dstool'] + args
    try:
        result = subprocess.run(cmd, check=check, capture_output=True, text=True)
        return result.returncode == 0
    except FileNotFoundError:
        print("Error: 3dstool not found. Please install it.")
        return False
    except subprocess.CalledProcessError as e:
        print(f"3dstool error: {e.stderr}")
        return False


def extract_banner(banner_path, output_dir):
    """Extract banner.bnr to directory using 3dstool"""
    os.makedirs(output_dir, exist_ok=True)
    return run_3dstool(['-x', '-t', 'banner', '--banner-dir', output_dir, '-f', banner_path])


def build_banner(input_dir, output_path):
    """Build banner.bnr from directory using 3dstool"""
    return run_3dstool(['-c', '-t', 'banner', '--banner-dir', input_dir, '-f', output_path])


def find_locale_string_offset(data, locale='USA_EN'):
    """Find offset of locale string in BCMDL data"""
    # Locale strings are ASCII in the file
    locale_bytes = locale.encode('ascii')
    offset = data.find(locale_bytes)
    return offset


def patch_locale_string(data, old_locale, new_locale):
    """Replace locale string in BCMDL data"""
    if len(old_locale) != len(new_locale):
        # Pad with underscore or truncate
        if len(new_locale) < len(old_locale):
            new_locale = new_locale + '_' * (len(old_locale) - len(new_locale))
        else:
            new_locale = new_locale[:len(old_locale)]
    
    old_bytes = old_locale.encode('ascii')
    new_bytes = new_locale.encode('ascii')
    
    return data.replace(old_bytes, new_bytes)


def get_texture_format_offset(bcmdl_data):
    """
    Find texture format byte offset in BCMDL file.
    For GBA VC banners created by NSUI, this is typically at offset 0x400 (1024).
    """
    # Standard offset for NSUI GBA VC banners
    return 0x400


def get_texture_format(bcmdl_data):
    """Get current texture format from BCMDL"""
    offset = get_texture_format_offset(bcmdl_data)
    if offset < len(bcmdl_data):
        fmt_byte = bcmdl_data[offset]
        return fmt_byte, TEXTURE_FORMATS.get(fmt_byte, f'Unknown (0x{fmt_byte:02X})')
    return None, 'Unknown'


def set_texture_format(bcmdl_data, new_format):
    """
    Set texture format in BCMDL.
    
    new_format: Either format code (int) or format name (str)
    """
    if isinstance(new_format, str):
        # Look up format code
        for code, name in TEXTURE_FORMATS.items():
            if name == new_format:
                new_format = code
                break
        else:
            raise ValueError(f"Unknown format: {new_format}")
    
    offset = get_texture_format_offset(bcmdl_data)
    result = bytearray(bcmdl_data)
    result[offset] = new_format
    return bytes(result)


class BCMDLEditor:
    """Editor for BCMDL (3DS model/texture) files"""
    
    def __init__(self, path=None):
        self.data = None
        self.path = path
        if path:
            self.load(path)
    
    def load(self, path):
        """Load BCMDL file"""
        with open(path, 'rb') as f:
            self.data = f.read()
        self.path = path
    
    def save(self, path=None):
        """Save BCMDL file"""
        path = path or self.path
        with open(path, 'wb') as f:
            f.write(self.data)
    
    def get_locale(self):
        """Get locale string from BCMDL"""
        for locale in REGION_CODES.values():
            if locale != 'COMMON':
                offset = find_locale_string_offset(self.data, locale)
                if offset >= 0:
                    return locale
        return None
    
    def set_locale(self, new_locale):
        """Set locale string in BCMDL"""
        old_locale = self.get_locale()
        if old_locale:
            self.data = patch_locale_string(self.data, old_locale, new_locale)
            return True
        return False
    
    def get_texture_format(self):
        """Get texture format"""
        return get_texture_format(self.data)
    
    def set_texture_format(self, new_format):
        """Set texture format (for enabling color instead of grayscale)"""
        self.data = set_texture_format(self.data, new_format)
    
    def enable_color_textures(self):
        """Change from LA8 (grayscale) to ETC1A4 (color)"""
        self.set_texture_format('ETC1A4')


class BannerEditor:
    """Editor for 3DS banner.bnr files"""
    
    def __init__(self, banner_path=None):
        self.banner_path = banner_path
        self.temp_dir = None
        self.bcmdl_files = {}
        self.cbmd_data = None
        self.bcwav_data = None
        
        if banner_path:
            self.load(banner_path)
    
    def load(self, banner_path):
        """Load and extract banner"""
        self.banner_path = banner_path
        self.temp_dir = tempfile.mkdtemp(prefix='banner_edit_')
        
        if not extract_banner(banner_path, self.temp_dir):
            raise RuntimeError(f"Failed to extract banner: {banner_path}")
        
        # Load all files
        for f in os.listdir(self.temp_dir):
            path = os.path.join(self.temp_dir, f)
            if f.endswith('.bcmdl'):
                # Extract region number from filename
                num = int(f.replace('banner', '').replace('.bcmdl', ''))
                self.bcmdl_files[num] = BCMDLEditor(path)
            elif f == 'banner.cbmd':
                with open(path, 'rb') as file:
                    self.cbmd_data = file.read()
            elif f == 'banner.bcwav':
                with open(path, 'rb') as file:
                    self.bcwav_data = file.read()
    
    def get_regions(self):
        """Get list of available regions"""
        return {num: REGION_CODES.get(num, f'Unknown_{num}') 
                for num in sorted(self.bcmdl_files.keys())}
    
    def fix_all_locales(self):
        """Fix all bcmdl files to have correct locale strings"""
        for num, bcmdl in self.bcmdl_files.items():
            if num == 0:
                continue  # Common doesn't need locale fix
            
            correct_locale = REGION_CODES.get(num)
            if correct_locale and correct_locale != 'COMMON':
                current = bcmdl.get_locale()
                if current != correct_locale:
                    print(f"  Fixing banner{num}.bcmdl: {current} -> {correct_locale}")
                    bcmdl.set_locale(correct_locale)
    
    def enable_color_all(self):
        """Enable color textures in all bcmdl files"""
        for num, bcmdl in self.bcmdl_files.items():
            code, name = bcmdl.get_texture_format()
            if name == 'LA8':
                print(f"  Enabling color in banner{num}.bcmdl")
                bcmdl.enable_color_textures()
    
    def copy_region(self, source_region, target_regions):
        """
        Copy one region's bcmdl to others.
        Useful when you only have USA_EN and want to copy to all regions.
        """
        if source_region not in self.bcmdl_files:
            raise ValueError(f"Source region {source_region} not found")
        
        source = self.bcmdl_files[source_region]
        
        for target in target_regions:
            if target != source_region:
                # Copy data
                target_bcmdl = BCMDLEditor()
                target_bcmdl.data = bytearray(source.data)
                
                # Fix locale
                target_locale = REGION_CODES.get(target)
                if target_locale and target_locale != 'COMMON':
                    target_bcmdl.set_locale(target_locale)
                
                self.bcmdl_files[target] = target_bcmdl
                print(f"  Copied banner{source_region}.bcmdl to banner{target}.bcmdl")
    
    def save(self, output_path=None):
        """Save modified banner"""
        output_path = output_path or self.banner_path
        
        # Save all bcmdl files
        for num, bcmdl in self.bcmdl_files.items():
            bcmdl.save(os.path.join(self.temp_dir, f'banner{num}.bcmdl'))
        
        # Build banner
        if not build_banner(self.temp_dir, output_path):
            raise RuntimeError("Failed to build banner")
        
        return output_path
    
    def cleanup(self):
        """Clean up temporary files"""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None
    
    def __del__(self):
        self.cleanup()


def create_banner_from_template(template_path, output_path, 
                                screen_image=None, title_image=None,
                                fix_locales=True, enable_color=False):
    """
    Create a new banner based on a template.
    
    template_path: Path to existing banner.bnr (e.g., from GBA VC)
    output_path: Path for new banner
    screen_image: PNG for screen texture (optional)
    title_image: PNG for title plate texture (optional)
    fix_locales: Fix all locale strings for proper region support
    enable_color: Change texture format to support color
    """
    editor = BannerEditor(template_path)
    
    print(f"Loaded banner with {len(editor.bcmdl_files)} region files")
    print(f"Regions: {editor.get_regions()}")
    
    if fix_locales:
        print("Fixing locale strings...")
        editor.fix_all_locales()
    
    if enable_color:
        print("Enabling color textures...")
        editor.enable_color_all()
    
    # TODO: Add texture replacement using Ohana3DS-style parsing
    # This would require implementing BCMDL texture import
    if screen_image or title_image:
        print("Note: Texture replacement not yet implemented")
        print("Use Ohana3DS to import textures into the bcmdl files")
    
    editor.save(output_path)
    print(f"Saved banner: {output_path}")
    
    return output_path


def fix_nsui_banner(cia_path, output_path=None):
    """
    Fix NSUI v28 GBA VC banner locale issues.
    
    This replicates what nsui_banner_fixer does:
    1. Extract CIA
    2. Extract banner
    3. Fix locale strings in all bcmdl files
    4. Rebuild banner
    5. Rebuild CIA
    """
    output_path = output_path or cia_path.replace('.cia', '_fixed.cia')
    
    temp_dir = tempfile.mkdtemp(prefix='cia_fix_')
    
    try:
        # Extract CIA using ctrtool
        print(f"Extracting CIA: {cia_path}")
        # This would require ctrtool - for now just handle banner.bnr directly
        print("Note: Full CIA extraction requires ctrtool")
        print("For now, extract banner.bnr manually and use banner editing functions")
        
    finally:
        shutil.rmtree(temp_dir)
    
    return output_path


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='3DS Banner Editor')
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Extract command
    extract_parser = subparsers.add_parser('extract', help='Extract banner.bnr')
    extract_parser.add_argument('input', help='Input banner.bnr')
    extract_parser.add_argument('output', help='Output directory')
    
    # Build command
    build_parser = subparsers.add_parser('build', help='Build banner.bnr')
    build_parser.add_argument('input', help='Input directory')
    build_parser.add_argument('output', help='Output banner.bnr')
    
    # Info command
    info_parser = subparsers.add_parser('info', help='Show banner info')
    info_parser.add_argument('input', help='Input banner.bnr')
    
    # Fix command
    fix_parser = subparsers.add_parser('fix', help='Fix banner locales')
    fix_parser.add_argument('input', help='Input banner.bnr')
    fix_parser.add_argument('-o', '--output', help='Output banner.bnr')
    fix_parser.add_argument('--enable-color', action='store_true',
                           help='Enable color textures (ETC1A4 instead of LA8)')
    
    # Copy-regions command  
    copy_parser = subparsers.add_parser('copy-regions', 
                                        help='Copy one region to all others')
    copy_parser.add_argument('input', help='Input banner.bnr')
    copy_parser.add_argument('-o', '--output', help='Output banner.bnr')
    copy_parser.add_argument('-s', '--source', type=int, default=10,
                            help='Source region number (default: 10 = USA_EN)')
    
    args = parser.parse_args()
    
    if args.command == 'extract':
        if extract_banner(args.input, args.output):
            print(f"Extracted to: {args.output}")
        else:
            print("Extraction failed")
            sys.exit(1)
    
    elif args.command == 'build':
        if build_banner(args.input, args.output):
            print(f"Built: {args.output}")
        else:
            print("Build failed")
            sys.exit(1)
    
    elif args.command == 'info':
        editor = BannerEditor(args.input)
        print(f"Banner: {args.input}")
        print(f"Regions: {len(editor.bcmdl_files)}")
        for num, region in editor.get_regions().items():
            bcmdl = editor.bcmdl_files[num]
            locale = bcmdl.get_locale() or 'N/A'
            code, fmt = bcmdl.get_texture_format()
            print(f"  banner{num}.bcmdl: region={region}, locale={locale}, format={fmt}")
    
    elif args.command == 'fix':
        output = args.output or args.input.replace('.bnr', '_fixed.bnr')
        editor = BannerEditor(args.input)
        editor.fix_all_locales()
        if args.enable_color:
            editor.enable_color_all()
        editor.save(output)
        print(f"Fixed banner saved: {output}")
    
    elif args.command == 'copy-regions':
        output = args.output or args.input.replace('.bnr', '_all_regions.bnr')
        editor = BannerEditor(args.input)
        
        # Copy source to all other regions
        all_regions = list(range(14))
        editor.copy_region(args.source, all_regions)
        editor.fix_all_locales()
        editor.save(output)
        print(f"Banner with all regions saved: {output}")
    
    else:
        parser.print_help()
