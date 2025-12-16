#!/usr/bin/env python3
"""
GBA VC 3D Banner Creator using Templates

This tool creates GBA VC 3D banners by patching textures into template files.
Templates can be obtained from:
- Asia81's templates: https://gbatemp.net/threads/release-templates-of-all-3ds-virtual-console-official-banners.404880/
- Extracted from official GBA VC CIAs

Template directory structure:
  templates/
    gba_vc/
      banner.cgfx          - Main CGFX template (single-file banner)
      banner.cbmd          - OR multi-file: CBMD header
      banner0.bcmdl        - Common region banner model
      banner1.bcmdl        - EUR_EN banner model
      ...
      banner13.bcmdl       - USA_PO banner model

The screen texture in GBA VC banners is 256x64 pixels.
"""

import os
import sys
import struct
import shutil
from typing import Optional, Tuple, Dict
from PIL import Image

# Import our texture encoder
try:
    from texture_encoder import encode_la8, TextureFormat, get_texture_size
except ImportError:
    import importlib.util
    spec = importlib.util.spec_from_file_location("texture_encoder", 
        os.path.join(os.path.dirname(__file__), "texture_encoder.py"))
    texture_encoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(texture_encoder)
    encode_la8 = texture_encoder.encode_la8
    TextureFormat = texture_encoder.TextureFormat
    get_texture_size = texture_encoder.get_texture_size


# Known offsets for GBA VC templates
# These were reverse-engineered from official GBA VC banners and NSUI output
GBA_VC_OFFSETS = {
    # For CGFX files (single-file banners like from early NSUI/GBA VC Converter)
    'cgfx': {
        'format_offset': 0x2CA0,  # Texture format byte
        'data_offset': 0x2CC0,    # Start of texture data (approximate)
        'texture_size': 256 * 64 * 2,  # LA8 = 2 bytes per pixel
    },
    # For BCMDL files (NSUI v28 style multi-file banners)
    'bcmdl': {
        'format_offset': 0x400,   # Offset 1024 decimal
        'data_offset': 0x440,     # Texture data start (approximate)
        'texture_size': 256 * 64 * 2,
    }
}

# Region codes for banner locale strings
REGION_CODES = {
    0: b'COMMON\x00\x00',   # banner0.bcmdl
    1: b'EUR_EN\x00\x00',   # banner1.bcmdl
    2: b'EUR_FR\x00\x00',   # banner2.bcmdl
    3: b'EUR_GE\x00\x00',   # banner3.bcmdl
    4: b'EUR_IT\x00\x00',   # banner4.bcmdl
    5: b'EUR_SP\x00\x00',   # banner5.bcmdl
    6: b'EUR_DU\x00\x00',   # banner6.bcmdl
    7: b'EUR_PO\x00\x00',   # banner7.bcmdl
    8: b'EUR_RU\x00\x00',   # banner8.bcmdl
    9: b'JPN_JP\x00\x00',   # banner9.bcmdl
    10: b'USA_EN\x00\x00',  # banner10.bcmdl
    11: b'USA_FR\x00\x00',  # banner11.bcmdl
    12: b'USA_SP\x00\x00',  # banner12.bcmdl
    13: b'USA_PO\x00\x00',  # banner13.bcmdl
}


def find_template_dir() -> Optional[str]:
    """Find the templates directory."""
    # Check relative to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    candidates = [
        os.path.join(script_dir, '..', 'templates', 'gba_vc'),
        os.path.join(script_dir, 'templates', 'gba_vc'),
        '/opt/forwarder/templates/gba_vc',
        './templates/gba_vc',
    ]
    
    for path in candidates:
        if os.path.isdir(path):
            return os.path.abspath(path)
    
    return None


def find_texture_offset(data: bytes, file_type: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Find texture format and data offsets in a CGFX/BCMDL file.
    
    Returns:
        Tuple of (format_offset, data_offset) or (None, None) if not found
    """
    offsets = GBA_VC_OFFSETS.get(file_type, GBA_VC_OFFSETS['cgfx'])
    
    # Try known offsets first
    format_offset = offsets['format_offset']
    data_offset = offsets['data_offset']
    
    if format_offset < len(data):
        fmt_byte = data[format_offset]
        if fmt_byte in (0x05, 0x0D):  # LA8 or ETC1A4
            return format_offset, data_offset
    
    # Search for format byte if known offset doesn't work
    # Look for 0x05 (LA8) or 0x0D (ETC1A4) followed by texture dimensions
    for offset in range(0x100, min(len(data), 0x10000)):
        if data[offset] in (0x05, 0x0D):
            # Check for 256x64 dimensions nearby
            for dim_off in range(max(0, offset - 64), min(len(data) - 4, offset + 64)):
                try:
                    w = struct.unpack_from('<H', data, dim_off)[0]
                    h = struct.unpack_from('<H', data, dim_off + 2)[0]
                    if w == 256 and h == 64:
                        # Found it - data usually starts shortly after
                        data_start = max(offset, dim_off) + 0x40
                        # Align to 8 bytes
                        data_start = (data_start + 7) & ~7
                        return offset, data_start
                except:
                    continue
    
    return None, None


def patch_texture(template_data: bytes, texture_image: Image.Image, 
                  file_type: str = 'cgfx') -> bytes:
    """
    Patch a texture into a template file.
    
    Args:
        template_data: Original template file bytes
        texture_image: PIL Image to use as texture (will be resized to 256x64)
        file_type: 'cgfx' or 'bcmdl'
    
    Returns:
        Patched file bytes
    """
    data = bytearray(template_data)
    
    # Find offsets
    format_offset, data_offset = find_texture_offset(data, file_type)
    
    if format_offset is None:
        raise ValueError(f"Could not find texture offset in {file_type} file")
    
    # Resize image if needed
    if texture_image.size != (256, 64):
        texture_image = texture_image.resize((256, 64), Image.Resampling.LANCZOS)
    
    # Encode texture as LA8 (grayscale)
    texture_data = encode_la8(texture_image)
    
    # Check size
    expected_size = get_texture_size(256, 64, TextureFormat.LA8)
    if len(texture_data) != expected_size:
        raise ValueError(f"Encoded texture size {len(texture_data)} != expected {expected_size}")
    
    # Ensure we have space
    if data_offset + len(texture_data) > len(data):
        raise ValueError(f"Texture data would exceed file size")
    
    # Set format to LA8
    data[format_offset] = TextureFormat.LA8
    
    # Write texture data
    data[data_offset:data_offset + len(texture_data)] = texture_data
    
    return bytes(data)


def fix_locale_string(data: bytes, region_index: int) -> bytes:
    """
    Fix the locale string in a BCMDL file to match the region.
    
    This fixes the NSUI v28 bug where all regions get USA_EN locale.
    """
    if region_index not in REGION_CODES:
        return data
    
    correct_locale = REGION_CODES[region_index]
    
    # Search for any USA_EN string and replace with correct locale
    data = bytearray(data)
    search = b'USA_EN\x00\x00'
    
    offset = 0
    while True:
        pos = data.find(search, offset)
        if pos == -1:
            break
        data[pos:pos + len(correct_locale)] = correct_locale
        offset = pos + len(correct_locale)
    
    return bytes(data)


class GBAVCBannerCreator:
    """Create GBA VC 3D banners using templates."""
    
    def __init__(self, template_dir: Optional[str] = None):
        """
        Initialize with template directory.
        
        Args:
            template_dir: Path to GBA VC templates. If None, auto-detect.
        """
        if template_dir is None:
            template_dir = find_template_dir()
        
        self.template_dir = template_dir
        self.template_type = None  # 'cgfx' or 'bcmdl'
        
        if template_dir and os.path.isdir(template_dir):
            self._detect_template_type()
    
    def _detect_template_type(self):
        """Detect what type of templates are available."""
        if not self.template_dir:
            return
        
        # Check for single CGFX
        if os.path.exists(os.path.join(self.template_dir, 'banner.cgfx')):
            self.template_type = 'cgfx'
        # Check for multi-file BCMDL
        elif os.path.exists(os.path.join(self.template_dir, 'banner0.bcmdl')):
            self.template_type = 'bcmdl'
        elif os.path.exists(os.path.join(self.template_dir, 'banner.cbmd')):
            self.template_type = 'bcmdl'
    
    def has_templates(self) -> bool:
        """Check if templates are available."""
        return self.template_type is not None
    
    def create_banner_cgfx(self, texture_image: Image.Image, output_path: str) -> bool:
        """
        Create a single CGFX banner file.
        
        Args:
            texture_image: Screen texture (256x64 recommended)
            output_path: Path to save banner.cgfx
        
        Returns:
            True if successful
        """
        if not self.template_dir:
            print("Error: No template directory configured")
            return False
        
        template_path = os.path.join(self.template_dir, 'banner.cgfx')
        if not os.path.exists(template_path):
            print(f"Error: Template not found: {template_path}")
            return False
        
        with open(template_path, 'rb') as f:
            template_data = f.read()
        
        try:
            patched = patch_texture(template_data, texture_image, 'cgfx')
            
            with open(output_path, 'wb') as f:
                f.write(patched)
            
            return True
        except Exception as e:
            print(f"Error creating banner: {e}")
            return False
    
    def create_banner_bnr(self, texture_image: Image.Image, output_dir: str,
                         fix_locales: bool = True) -> bool:
        """
        Create a full banner.bnr directory structure (BCMDL style).
        
        Args:
            texture_image: Screen texture (256x64 recommended)
            output_dir: Directory to create banner files in
            fix_locales: Fix locale strings for all regions
        
        Returns:
            True if successful
        """
        if not self.template_dir:
            print("Error: No template directory configured")
            return False
        
        os.makedirs(output_dir, exist_ok=True)
        
        # Copy CBMD header if exists
        cbmd_src = os.path.join(self.template_dir, 'banner.cbmd')
        if os.path.exists(cbmd_src):
            shutil.copy(cbmd_src, os.path.join(output_dir, 'banner.cbmd'))
        
        # Copy BCWAV if exists
        bcwav_src = os.path.join(self.template_dir, 'banner.bcwav')
        if os.path.exists(bcwav_src):
            shutil.copy(bcwav_src, os.path.join(output_dir, 'banner.bcwav'))
        
        # Process each region's BCMDL
        for i in range(14):
            template_path = os.path.join(self.template_dir, f'banner{i}.bcmdl')
            if not os.path.exists(template_path):
                continue
            
            with open(template_path, 'rb') as f:
                template_data = f.read()
            
            try:
                # Patch texture
                patched = patch_texture(template_data, texture_image, 'bcmdl')
                
                # Fix locale if requested
                if fix_locales:
                    patched = fix_locale_string(patched, i)
                
                # Save
                output_path = os.path.join(output_dir, f'banner{i}.bcmdl')
                with open(output_path, 'wb') as f:
                    f.write(patched)
                
            except Exception as e:
                print(f"Warning: Failed to process banner{i}.bcmdl: {e}")
        
        return True
    
    def create_banner(self, texture_path: str, output_path: str,
                     fix_locales: bool = True) -> bool:
        """
        Create a banner from a texture image.
        
        Automatically chooses the right output format based on templates.
        
        Args:
            texture_path: Path to screen texture image
            output_path: Output path (file for CGFX, directory for BNR)
            fix_locales: Fix locale strings for BCMDL banners
        
        Returns:
            True if successful
        """
        try:
            image = Image.open(texture_path)
        except Exception as e:
            print(f"Error loading image: {e}")
            return False
        
        if self.template_type == 'cgfx':
            return self.create_banner_cgfx(image, output_path)
        elif self.template_type == 'bcmdl':
            return self.create_banner_bnr(image, output_path, fix_locales)
        else:
            print("Error: No templates available")
            print(f"Please add templates to: {self.template_dir or 'templates/gba_vc/'}")
            print("\nTemplates can be obtained from:")
            print("  https://gbatemp.net/threads/release-templates-of-all-3ds-virtual-console-official-banners.404880/")
            return False


def create_readme():
    """Create README for templates directory."""
    readme = """# GBA VC Banner Templates

This directory should contain GBA Virtual Console 3D banner templates.

## Getting Templates

Templates can be downloaded from:
- Asia81's templates: https://gbatemp.net/threads/release-templates-of-all-3ds-virtual-console-official-banners.404880/
  - Download link: https://mega.nz/file/UAU2xL4L#tXzeuDizB605KS-6LQ5ukRMz1LD5ERxFJSp13-W_AoU
  - Password: Asia81

## Directory Structure

For single-file CGFX templates:
```
gba_vc/
  banner.cgfx
```

For multi-file BCMDL templates (NSUI v28 style):
```
gba_vc/
  banner.cbmd       (header)
  banner.bcwav      (sound, optional)
  banner0.bcmdl     (COMMON)
  banner1.bcmdl     (EUR_EN)
  banner2.bcmdl     (EUR_FR)
  banner3.bcmdl     (EUR_GE)
  banner4.bcmdl     (EUR_IT)
  banner5.bcmdl     (EUR_SP)
  banner6.bcmdl     (EUR_DU)
  banner7.bcmdl     (EUR_PO)
  banner8.bcmdl     (EUR_RU)
  banner9.bcmdl     (JPN_JP)
  banner10.bcmdl    (USA_EN)
  banner11.bcmdl    (USA_FR)
  banner12.bcmdl    (USA_SP)
  banner13.bcmdl    (USA_PO)
```

## Extracting from Existing Banners

You can also extract templates from existing GBA VC CIAs:

```bash
# Using 3dstool
3dstool -x -t banner --banner-dir ./extracted/ -f banner.bnr
```

## Usage

Once templates are in place, the banner creator will automatically use them:

```python
from template_banner_creator import GBAVCBannerCreator

creator = GBAVCBannerCreator()
creator.create_banner('screen.png', 'output/banner.cgfx')
```
"""
    return readme


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='GBA VC 3D Banner Creator',
        epilog='Create GBA VC 3D banners using templates'
    )
    
    parser.add_argument('texture', nargs='?', help='Screen texture image (256x64 PNG)')
    parser.add_argument('-o', '--output', help='Output path')
    parser.add_argument('-t', '--template-dir', help='Template directory')
    parser.add_argument('--fix-locales', action='store_true', default=True,
                       help='Fix locale strings (default: True)')
    parser.add_argument('--no-fix-locales', action='store_false', dest='fix_locales',
                       help='Do not fix locale strings')
    parser.add_argument('--check', action='store_true',
                       help='Check if templates are available')
    parser.add_argument('--init', action='store_true',
                       help='Initialize template directories with README')
    
    args = parser.parse_args()
    
    if args.init:
        # Create template directories and README
        base_dir = args.template_dir or './templates'
        for vc_type in ['gba_vc', 'gb_vc', 'gbc_vc', 'nes_vc']:
            dir_path = os.path.join(base_dir, vc_type)
            os.makedirs(dir_path, exist_ok=True)
            readme_path = os.path.join(dir_path, 'README.md')
            with open(readme_path, 'w') as f:
                f.write(create_readme())
        print(f"Initialized template directories in {base_dir}/")
        print("Please download templates and place them in the appropriate directories.")
        return
    
    creator = GBAVCBannerCreator(args.template_dir)
    
    if args.check:
        if creator.has_templates():
            print(f"Templates found: {creator.template_type}")
            print(f"Template directory: {creator.template_dir}")
        else:
            print("No templates found!")
            print(f"Searched in: {creator.template_dir or 'default locations'}")
            print("\nRun with --init to create template directories,")
            print("then download templates from:")
            print("  https://gbatemp.net/threads/release-templates-of-all-3ds-virtual-console-official-banners.404880/")
        return
    
    if not args.texture:
        parser.print_help()
        return
    
    if not args.output:
        # Default output name
        base = os.path.splitext(args.texture)[0]
        args.output = f"{base}_banner.cgfx"
    
    success = creator.create_banner(args.texture, args.output, args.fix_locales)
    
    if success:
        print(f"Created banner: {args.output}")
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
