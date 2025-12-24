#!/usr/bin/env python3
"""
GBA VC Banner Patcher v2 - NSUI Compatible

Creates banners with proper region-specific CGFX sections for real 3DS hardware.

NSUI Template Structure:
- Common CGFX (266KB decompressed):
  - COMMON1 at 0x38F80: 128x128 RGB565 8x8-tiled (cartridge label)
  - COMMON3: Shell texture
- Region CGFX (39KB each, 13 regions):
  - COMMON2 at 0x1980: 256x64 LA8 Morton-tiled (footer text)

Final Banner Structure:
- CBMD header (0x88 bytes) with region offsets
- Common CGFX (LZ11 compressed)
- 13 Region CGFX sections (LZ11 compressed)
- CWAV audio
"""

import struct
import os
import subprocess
import tempfile
from pathlib import Path
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


# ============================================================================
# TEXTURE ENCODING/DECODING
# ============================================================================

def morton_index(x, y):
    """Calculate Morton code index for pixel within 8x8 tile"""
    morton = 0
    for i in range(3):
        morton |= ((x >> i) & 1) << (2 * i)
        morton |= ((y >> i) & 1) << (2 * i + 1)
    return morton


def resize_cover(img, width, height):
    """
    Resize image using 'cover' mode: scale to cover target area, crop center.
    This maintains aspect ratio without distortion.
    """
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    dst_ratio = width / height
    
    if src_ratio > dst_ratio:
        # Image is wider - scale by height, crop width
        new_h = height
        new_w = int(src_w * (height / src_h))
    else:
        # Image is taller - scale by width, crop height
        new_w = width
        new_h = int(src_h * (width / src_w))
    
    img = img.resize((new_w, new_h), Image.LANCZOS)
    
    # Crop center
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    img = img.crop((left, top, left + width, top + height))
    
    return img


def resize_stretch(img, width, height):
    """
    Resize image using 'stretch' mode: stretch to exact dimensions.
    This may distort the aspect ratio.
    """
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    return img.resize((width, height), Image.LANCZOS)


def resize_fit(img, width, height):
    """
    Resize image using 'fit' mode: scale to fit within target area, center on transparent background.
    This maintains aspect ratio without cropping - the entire image is visible.
    """
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    dst_ratio = width / height
    
    if src_ratio > dst_ratio:
        # Image is wider - scale by width
        new_w = width
        new_h = int(src_h * (width / src_w))
    else:
        # Image is taller - scale by height
        new_h = height
        new_w = int(src_w * (height / src_h))
    
    # Ensure we don't exceed target dimensions
    new_w = min(new_w, width)
    new_h = min(new_h, height)
    
    img = img.resize((new_w, new_h), Image.LANCZOS)
    
    # Convert to RGBA if needed
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    
    # Create transparent background and center the image
    result = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    paste_x = (width - new_w) // 2
    paste_y = (height - new_h) // 2
    result.paste(img, (paste_x, paste_y), img if img.mode == 'RGBA' else None)
    
    return result


def encode_rgb565_tiled(img, width=128, height=128, bg_color=None, fit_mode="fit"):
    """
    Encode image to RGB565 8x8 Morton-tiled format (NSUI COMMON1 format)

    Layout: 8x8 tiles with Morton (Z-order) pixel arrangement.
    Each pixel is 2 bytes (RGB565).

    Args:
        img: PIL Image to encode
        width: Target width (default 128)
        height: Target height (default 128)
        bg_color: Optional background color as (R, G, B) tuple (0-255).
                  If None, uses default dark color (50, 50, 70).
        fit_mode: Resize mode - 'fit' (default), 'fill', or 'stretch'
                  - fit: Scale to fit within target, center on background (entire image visible)
                  - fill: Scale to cover target, crop center (fills area, may crop edges)
                  - stretch: Stretch to exact dimensions (may distort aspect ratio)

    Note: RGB565 has no alpha channel. Images with transparency are
    composited onto a background color to prevent white lines.
    """
    if img.size != (width, height):
        if fit_mode == "fill":
            img = resize_cover(img, width, height)
        elif fit_mode == "stretch":
            img = resize_stretch(img, width, height)
        else:  # default to "fit"
            img = resize_fit(img, width, height)

    # Default background color if not specified
    if bg_color is None:
        bg_color = (50, 50, 70)  # Dark color that matches GBA shell

    # CRITICAL: Handle transparency by compositing onto solid background
    # This prevents white lines from transparent pixels
    if img.mode == 'RGBA' or img.mode == 'LA' or img.mode == 'PA':
        # Create background with specified color
        background = Image.new('RGB', (width, height), bg_color)
        # Convert to RGBA for proper compositing
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        # Composite: paste image onto background using alpha channel as mask
        background.paste(img, (0, 0), img)
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    data = bytearray(width * height * 2)
    tiles_x, tiles_y = width // 8, height // 8
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = (ty * tiles_x + tx) * 128  # 8x8 * 2 bytes
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 2
                    x = tx * 8 + px
                    y = ty * 8 + py
                    r, g, b = img.getpixel((x, y))[:3]
                    
                    # Convert to RGB565
                    r5 = (r >> 3) & 0x1F
                    g6 = (g >> 2) & 0x3F
                    b5 = (b >> 3) & 0x1F
                    rgb565 = (r5 << 11) | (g6 << 5) | b5
                    
                    struct.pack_into('<H', data, idx, rgb565)
    
    return bytes(data)


def decode_rgb565_tiled(data, offset, width, height):
    """Decode RGB565 8x8 Morton-tiled texture"""
    img = Image.new('RGB', (width, height))
    pixels = img.load()
    tiles_x, tiles_y = width // 8, height // 8
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = offset + (ty * tiles_x + tx) * 128  # 8x8 * 2 bytes
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 2
                    if idx + 2 <= len(data):
                        pixel = struct.unpack_from('<H', data, idx)[0]
                        r = ((pixel >> 11) & 0x1F) << 3
                        g = ((pixel >> 5) & 0x3F) << 2
                        b = (pixel & 0x1F) << 3
                        pixels[tx * 8 + px, ty * 8 + py] = (r, g, b)
    return img


def encode_la8_morton(img, width=256, height=64):
    """
    Encode image to LA8 Morton-tiled format (COMMON2 format)
    
    Layout: 8x8 tiles with Morton (Z-order) pixel arrangement.
    Each pixel is 2 bytes: alpha, luminance.
    """
    if img.size != (width, height):
        img = resize_cover(img, width, height)
    if img.mode != 'RGBA':
        img = img.convert('RGBA')
    
    data = bytearray(width * height * 2)
    tiles_x, tiles_y = width // 8, height // 8
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = (ty * tiles_x + tx) * 128
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 2
                    r, g, b, a = img.getpixel((tx * 8 + px, ty * 8 + py))
                    l = (r + g + b) // 3  # Luminance
                    data[idx] = a
                    data[idx + 1] = l
    
    return bytes(data)


def decode_la8_morton(data, offset, width, height):
    """Decode LA8 Morton-tiled texture"""
    img = Image.new('RGBA', (width, height))
    pixels = img.load()
    tiles_x, tiles_y = width // 8, height // 8
    
    for ty in range(tiles_y):
        for tx in range(tiles_x):
            tile_off = offset + (ty * tiles_x + tx) * 128
            for py in range(8):
                for px in range(8):
                    idx = tile_off + morton_index(px, py) * 2
                    if idx + 2 <= len(data):
                        a, l = data[idx:idx + 2]
                        pixels[tx * 8 + px, ty * 8 + py] = (l, l, l, a)
    return img


# ============================================================================
# LZ11 COMPRESSION
# ============================================================================

def compress_lz11(data):
    """
    Compress data using a faster, simplified LZ11 encoder.

    This prioritizes speed over maximum compression ratio; acceptable for banners.
    """
    result = bytearray()
    size = len(data)
    result.extend([0x11, size & 0xFF, (size >> 8) & 0xFF, (size >> 16) & 0xFF])

    pos = 0
    max_window = 0x1000  # 4096

    while pos < size:
        flags_pos = len(result)
        result.append(0)  # placeholder for flags
        flags = 0
        for bit in range(8):
            if pos >= size:
                break

            best_len = 0
            best_disp = 0
            window_start = max(0, pos - max_window)
            window = memoryview(data)[window_start:pos]
            max_len = min(0x10110, size - pos)

            if len(window) >= 3:
                seed = bytes(data[pos : pos + 3])
                search_pos = bytes(window).rfind(seed, 0, len(window))
                while search_pos != -1:
                    disp = pos - (window_start + search_pos)
                    match_len = 3
                    while match_len < max_len:
                        next_len = min(match_len + 32, max_len)
                        if data[pos + match_len : pos + next_len] != data[pos + match_len - disp : pos + next_len - disp]:
                            break
                        match_len = next_len
                    if match_len > best_len:
                        best_len = match_len
                        best_disp = disp
                        if best_len >= max_len:
                            break
                    search_pos = bytes(window).rfind(seed, 0, search_pos)

            if best_len >= 3:
                flags |= 0x80 >> bit
                disp_m1 = best_disp - 1
                if best_len <= 0x10:
                    result.append(((best_len - 1) << 4) | ((disp_m1 >> 8) & 0x0F))
                    result.append(disp_m1 & 0xFF)
                elif best_len <= 0x110:
                    adj_len = best_len - 0x11
                    result.append((adj_len >> 4) & 0x0F)
                    result.append(((adj_len & 0x0F) << 4) | ((disp_m1 >> 8) & 0x0F))
                    result.append(disp_m1 & 0xFF)
                else:
                    adj_len = best_len - 0x111
                    result.append(0x10 | ((adj_len >> 12) & 0x0F))
                    result.append((adj_len >> 4) & 0xFF)
                    result.append(((adj_len & 0x0F) << 4) | ((disp_m1 >> 8) & 0x0F))
                    result.append(disp_m1 & 0xFF)
                pos += best_len
            else:
                result.append(data[pos])
                pos += 1

        result[flags_pos] = flags

    return bytes(result)


def decompress_lz11(data, offset=0):
    """Decompress LZ11 data"""
    if offset >= len(data) or data[offset] != 0x11:
        return None
    decompressed_size = struct.unpack_from('<I', data, offset)[0] >> 8
    if decompressed_size > 0x500000:
        return None
    result = bytearray()
    pos = offset + 4
    while len(result) < decompressed_size and pos < len(data):
        flags = data[pos]
        pos += 1
        for i in range(8):
            if len(result) >= decompressed_size:
                break
            if flags & (0x80 >> i):
                if pos + 2 > len(data):
                    break
                byte1 = data[pos]
                byte2 = data[pos + 1]
                pos += 2
                if byte1 >> 4 == 0:
                    if pos >= len(data):
                        break
                    byte3 = data[pos]
                    pos += 1
                    length = ((byte1 & 0x0F) << 4) + (byte2 >> 4) + 0x11
                    disp = ((byte2 & 0x0F) << 8) + byte3 + 1
                elif byte1 >> 4 == 1:
                    if pos + 1 >= len(data):
                        break
                    byte3 = data[pos]
                    byte4 = data[pos + 1]
                    pos += 2
                    length = ((byte1 & 0x0F) << 12) + (byte2 << 4) + (byte3 >> 4) + 0x111
                    disp = ((byte3 & 0x0F) << 8) + byte4 + 1
                else:
                    length = (byte1 >> 4) + 1
                    disp = ((byte1 & 0x0F) << 8) + byte2 + 1
                for j in range(length):
                    if len(result) >= decompressed_size:
                        break
                    if len(result) < disp:
                        result.append(0)
                    else:
                        result.append(result[-disp])
            else:
                if pos >= len(data):
                    break
                result.append(data[pos])
                pos += 1
    return bytes(result)


# ============================================================================
# BANNER PATCHER
# ============================================================================

class GBAVCBannerPatcher:
    """
    Creates GBA VC banners compatible with real 3DS hardware.
    
    Uses NSUI-style template with:
    - RGB565 8x8-tiled COMMON1 at offset 0x38F80 in common CGFX
    - LA8 Morton-tiled COMMON2 at offset 0x1980 in each region CGFX
    """
    
    # NSUI template offsets
    COMMON1_OFFSET = 0x38F80  # In common CGFX
    COMMON1_SIZE = 32768      # 128x128 RGB565 = 32KB
    
    COMMON2_OFFSET = 0x1980   # In region CGFX
    COMMON2_SIZE = 32768      # 256x64 LA8 = 32KB
    
    # Region names
    REGIONS = [
        "JPN", "USA_EN", "EUR_EN", "EUR_FR", "EUR_GE",
        "EUR_IT", "EUR_SP", "CHN", "KOR", "TWN",
        "USA_FR", "USA_SP", "USA_PO"
    ]
    
    def __init__(self, template_dir):
        """
        Initialize with NSUI template directory containing:
        - banner_common.cgfx (decompressed common CGFX)
        - region_XX_NAME.cgfx (decompressed region CGFX files)
        - banner.bcwav (audio)
        """
        self.template_dir = template_dir
        self._load_templates()
    
    def _load_templates(self):
        """Load template files"""
        # Load common CGFX
        common_path = os.path.join(self.template_dir, 'banner_common.cgfx')
        with open(common_path, 'rb') as f:
            self.common_cgfx = bytearray(f.read())
        print(f"Loaded common CGFX: {len(self.common_cgfx):,} bytes")
        
        # Load region templates
        self.region_templates = []
        for i, name in enumerate(self.REGIONS):
            region_path = os.path.join(self.template_dir, f'region_{i:02d}_{name}.cgfx')
            with open(region_path, 'rb') as f:
                self.region_templates.append(bytearray(f.read()))
        print(f"Loaded {len(self.region_templates)} region templates")
        
        # Load audio
        audio_path = os.path.join(self.template_dir, 'banner.bcwav')
        with open(audio_path, 'rb') as f:
            self.audio = f.read()
        print(f"Loaded audio: {len(self.audio):,} bytes")
    
    def patch_common1(self, image_path, bg_color=None, fit_mode="fit"):
        """
        Patch COMMON1 (cartridge label) in common CGFX.

        Args:
            image_path: Path to 128x128 image
            bg_color: Optional background color as (R, G, B) tuple (0-255)
            fit_mode: Resize mode - 'fit', 'fill', or 'stretch'
        """
        if Image is None:
            print("Warning: Pillow (PIL) not available; skipping COMMON1 patch")
            return
        img = Image.open(image_path)
        encoded = encode_rgb565_tiled(img, 128, 128, bg_color, fit_mode)

        if len(encoded) != self.COMMON1_SIZE:
            raise ValueError(f"Encoded size {len(encoded)} != expected {self.COMMON1_SIZE}")

        self.common_cgfx[self.COMMON1_OFFSET:self.COMMON1_OFFSET + self.COMMON1_SIZE] = encoded
        if bg_color:
            print(f"Patched COMMON1 with {image_path} (bg: RGB{bg_color})")
        else:
            print(f"Patched COMMON1 with {image_path}")
    
    def patch_common2(self, image_path):
        """
        Patch COMMON2 (footer text) in all region CGFX files.
        
        Args:
            image_path: Path to 256x64 image
        """
        if Image is None:
            print("Warning: Pillow (PIL) not available; skipping COMMON2 patch")
            return
        img = Image.open(image_path)
        encoded = encode_la8_morton(img, 256, 64)
        
        if len(encoded) != self.COMMON2_SIZE:
            raise ValueError(f"Encoded size {len(encoded)} != expected {self.COMMON2_SIZE}")
        
        for i, region in enumerate(self.region_templates):
            region[self.COMMON2_OFFSET:self.COMMON2_OFFSET + self.COMMON2_SIZE] = encoded
        
        print(f"Patched COMMON2 in all {len(self.REGIONS)} regions with {image_path}")
    
    def create_footer_image(self, title, subtitle="", save_path=None):
        """
        Create a footer image using the NSUI template as a base.
        
        This preserves the exact NSUI design (rounded boxes, gradients, 
        "Virtual Console" text) and only modifies the title text area.
        
        Handles both short and long titles:
        - Short titles: centered vertically with subtitle
        - Long titles: wrapped to multiple lines like NSUI
        
        Args:
            title: Game title
            subtitle: Optional subtitle (e.g., "Released: 2004")
            save_path: Optional path to save the image
            
        Returns:
            PIL Image object
        """
        if Image is None:
            if save_path:
                self._create_footer_image_magick(title, subtitle, save_path)
            return None
        # Load the original NSUI region template to get footer background
        template_file = os.path.join(self.template_dir, 'region_01_USA_EN.cgfx')
        
        with open(template_file, 'rb') as f:
            region_data = f.read()
        
        # Decode the footer from template (LA8 Morton at 0x1980)
        footer = self._decode_la8_texture(region_data, self.COMMON2_OFFSET, 256, 64)
        draw = ImageDraw.Draw(footer)
        
        # Clear the title text content area with proper gradient background
        for y in range(5, 59):
            progress = max(0, min(1, (y - 5) / 53.0))
            gray_val = int(255 - progress * (255 - 215))
            
            left_x = 95
            right_x = 250
            if y <= 6 or y >= 57:
                left_x = 100
                right_x = 245
            elif y <= 8 or y >= 55:
                left_x = 97
                right_x = 248
            
            for x in range(left_x, right_x):
                footer.putpixel((x, y), (gray_val, gray_val, gray_val, 255))
        
        # Right box center for centering text
        box_center = 172
        max_width = 148  # Available width in the title box
        
        # Load fonts
        font_title = None
        font_subtitle = None
        
        bundled_font = os.path.join(self.template_dir, 'SCE-PS3-RD-R-LATIN.TTF')
        if os.path.exists(bundled_font):
            try:
                font_title = ImageFont.truetype(bundled_font, 14)
                font_subtitle = ImageFont.truetype(bundled_font, 12)
            except:
                pass

        if font_title is None:
            fallback_fonts = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            ]
            for fp in fallback_fonts:
                try:
                    font_title = ImageFont.truetype(fp, 14)
                    font_subtitle = ImageFont.truetype(fp, 12)
                    break
                except:
                    continue
        
        if font_title is None:
            font_title = ImageFont.load_default()
            font_subtitle = font_title
        
        # Helper to draw centered text
        def draw_centered(text, y, font, color):
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            x = box_center - text_width // 2
            draw.text((x, y), text, fill=color, font=font)
        
        # Helper to wrap text (supports | for manual line breaks)
        def wrap_text(text, font, max_w):
            lines = []
            for segment in text.split("|"):
                segment = segment.strip()
                if not segment:
                    continue
                words = segment.split()
                current_line = []
                for word in words:
                    test_line = ' '.join(current_line + [word])
                    bbox = draw.textbbox((0, 0), test_line, font=font)
                    if bbox[2] - bbox[0] <= max_w:
                        current_line.append(word)
                    else:
                        if current_line:
                            lines.append(' '.join(current_line))
                        current_line = [word]
                if current_line:
                    lines.append(' '.join(current_line))
            return lines
        
        text_color = (32, 32, 32, 255)
        subtitle_color = (40, 40, 40, 255)
        
        # Wrap title if needed
        title_lines = wrap_text(title, font_title, max_width)

        # Drop subtitle if title wraps to 3+ lines
        if len(title_lines) >= 3:
            subtitle = None

        if len(title_lines) == 1:
            # Short title: vertically center title + subtitle
            if subtitle:
                draw_centered(title_lines[0], 16, font_title, text_color)
                draw_centered(subtitle, 36, font_subtitle, subtitle_color)
            else:
                draw_centered(title_lines[0], 24, font_title, text_color)

        elif len(title_lines) == 2:
            # Two-line title
            if subtitle:
                draw_centered(title_lines[0], 10, font_title, text_color)
                draw_centered(title_lines[1], 26, font_title, text_color)
                draw_centered(subtitle, 44, font_subtitle, subtitle_color)
            else:
                draw_centered(title_lines[0], 16, font_title, text_color)
                draw_centered(title_lines[1], 34, font_title, text_color)

        else:
            # Three+ lines: stack them (no subtitle)
            y = 10
            for line in title_lines[:3]:
                draw_centered(line, y, font_title, text_color)
                y += 17
        
        if save_path:
            footer.save(save_path)
        
        return footer

    def _create_footer_image_magick(self, title, subtitle, save_path):
        """Render footer with ImageMagick when Pillow is unavailable."""
        try:
            template_file = os.path.join(self.template_dir, 'region_01_USA_EN.cgfx')
            with open(template_file, 'rb') as f:
                region_data = f.read()
            raw = self._decode_la8_to_raw(region_data, self.COMMON2_OFFSET, 256, 64)
            tmp_dir = Path(tempfile.gettempdir())
            raw_path = tmp_dir / "footer_raw_rgba.bin"
            base_path = tmp_dir / "footer_base.png"
            raw_path.write_bytes(raw)

            subprocess.run(
                ["magick", "-size", "256x64", "-depth", "8", f"rgba:{raw_path}", str(base_path)],
                check=True,
                capture_output=True,
            )

            title = (title or "").strip()
            subtitle = (subtitle or "").strip()
            if not title:
                return

            # Clear title text area (same gradient as PIL path).
            subprocess.run(
                [
                    "magick",
                    str(base_path),
                    "-fill",
                    "none",
                    "-alpha",
                    "on",
                    "-draw",
                    self._magick_gradient_clear_draw(),
                    str(base_path),
                ],
                check=True,
                capture_output=True,
            )

            font_path = os.path.join(self.template_dir, "SCE-PS3-RD-R-LATIN.TTF")
            if not os.path.exists(font_path):
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

            lines = self._wrap_text_magick(title, font_path, 14, 148)
            if len(lines) >= 3:
                subtitle = ""

            annotate = ["magick", str(base_path)]
            if len(lines) == 1:
                if subtitle:
                    x = self._center_text_x(lines[0], font_path, 14, 172)
                    annotate += ["-font", font_path, "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+16", lines[0]]
                    sx = self._center_text_x(subtitle, font_path, 12, 172)
                    annotate += ["-font", font_path, "-pointsize", "12", "-fill", "rgb(40,40,40)",
                                 "-annotate", f"+{sx}+36", subtitle]
                else:
                    x = self._center_text_x(lines[0], font_path, 14, 172)
                    annotate += ["-font", font_path, "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+24", lines[0]]
            elif len(lines) == 2:
                x1 = self._center_text_x(lines[0], font_path, 14, 172)
                x2 = self._center_text_x(lines[1], font_path, 14, 172)
                if subtitle:
                    sx = self._center_text_x(subtitle, font_path, 12, 172)
                    annotate += ["-font", font_path, "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest",
                                 "-annotate", f"+{x1}+10", lines[0],
                                 "-annotate", f"+{x2}+26", lines[1]]
                    annotate += ["-font", font_path, "-pointsize", "12", "-fill", "rgb(40,40,40)",
                                 "-annotate", f"+{sx}+44", subtitle]
                else:
                    annotate += ["-font", font_path, "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest",
                                 "-annotate", f"+{x1}+16", lines[0],
                                 "-annotate", f"+{x2}+34", lines[1]]
            else:
                y = 10
                for line in lines[:3]:
                    x = self._center_text_x(line, font_path, 14, 172)
                    annotate += ["-font", font_path, "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+{y}", line]
                    y += 17

            annotate.append(save_path)
            subprocess.run(annotate, check=True, capture_output=True)
        except Exception:
            pass

    def _decode_la8_to_raw(self, data, offset, width, height):
        """Decode LA8 Morton-tiled texture to raw RGBA bytes (no Pillow)."""
        out = bytearray(width * height * 4)
        tiles_x, tiles_y = width // 8, height // 8

        for ty in range(tiles_y):
            for tx in range(tiles_x):
                tile_off = offset + (ty * tiles_x + tx) * 128
                for py in range(8):
                    for px in range(8):
                        idx = tile_off + morton_index(px, py) * 2
                        if idx + 2 <= len(data):
                            a, l = data[idx:idx + 2]
                            x = tx * 8 + px
                            y = ty * 8 + py
                            o = (y * width + x) * 4
                            out[o:o + 4] = bytes((l, l, l, a))
        return bytes(out)

    def _magick_gradient_clear_draw(self):
        """Return a draw command that clears the title area with the NSUI gradient."""
        cmds = []
        for y in range(5, 59):
            progress = max(0, min(1, (y - 5) / 53.0))
            gray_val = int(255 - progress * (255 - 215))
            left_x = 95
            right_x = 250
            if y <= 6 or y >= 57:
                left_x = 100
                right_x = 245
            elif y <= 8 or y >= 55:
                left_x = 97
                right_x = 248
            cmds.append(f"fill rgb({gray_val},{gray_val},{gray_val}) rectangle {left_x},{y} {right_x},{y+1}")
        return " ".join(cmds)

    def _measure_text_width(self, text, font_path, size):
        result = subprocess.run(
            ["magick", "-font", font_path, "-pointsize", str(size), f"label:{text}", "-format", "%w", "info:"],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip() or 0)

    def _center_text_x(self, text, font_path, size, center_x):
        w = self._measure_text_width(text, font_path, size)
        return max(0, center_x - w // 2)

    def _wrap_text_magick(self, text, font_path, size, max_w):
        # Support manual line breaks with | character
        lines = []
        for segment in text.split("|"):
            segment = segment.strip()
            if not segment:
                continue
            words = segment.split()
            current = []
            for word in words:
                test_line = " ".join(current + [word])
                if self._measure_text_width(test_line, font_path, size) <= max_w:
                    current.append(word)
                else:
                    if current:
                        lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
        return lines
    
    def _decode_la8_texture(self, data, offset, width, height):
        """Decode LA8 Morton-tiled texture to RGBA image"""
        img = Image.new('RGBA', (width, height))
        pixels = img.load()
        tiles_x, tiles_y = width // 8, height // 8
        
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                tile_off = offset + (ty * tiles_x + tx) * 128
                for py in range(8):
                    for px in range(8):
                        idx = tile_off + morton_index(px, py) * 2
                        if idx + 2 <= len(data):
                            a, l = data[idx:idx + 2]
                            pixels[tx * 8 + px, ty * 8 + py] = (l, l, l, a)
        return img
    
    def build_banner(self, output_path):
        """
        Build the final banner.bnr file.
        
        Args:
            output_path: Output path for banner file
            
        Returns:
            Path to created banner
        """
        print("\nBuilding banner...")
        
        def align4(size):
            """Align size to 4-byte boundary"""
            return (size + 3) & ~3
        
        def pad_to_align4(data):
            """Pad data to 4-byte alignment"""
            padding_needed = (4 - (len(data) % 4)) % 4
            return bytes(data) + b'\x00' * padding_needed
        
        # Compress common CGFX
        print("  Compressing common CGFX...")
        common_compressed = pad_to_align4(compress_lz11(bytes(self.common_cgfx)))
        print(f"    {len(self.common_cgfx):,} -> {len(common_compressed):,} bytes (aligned)")
        
        # Compress all region CGFX files with alignment padding
        print("  Compressing region CGFX files...")
        regions_compressed = []
        for i, region in enumerate(self.region_templates):
            compressed = pad_to_align4(compress_lz11(bytes(region)))
            regions_compressed.append(compressed)
        print(f"    13 regions compressed (aligned)")
        
        # Build CBMD header
        cbmd = bytearray(0x88)
        cbmd[0:4] = b'CBMD'
        
        # Common CGFX offset (right after CBMD header)
        common_offset = 0x88
        struct.pack_into('<I', cbmd, 0x08, common_offset)
        
        # Calculate region offsets (all aligned to 4 bytes)
        current_offset = common_offset + len(common_compressed)
        for i in range(len(self.REGIONS)):
            struct.pack_into('<I', cbmd, 0x0C + i * 4, current_offset)
            current_offset += len(regions_compressed[i])
        
        # CWAV audio offset at 0x84
        cwav_offset = current_offset
        struct.pack_into('<I', cbmd, 0x84, cwav_offset)
        
        # Build final banner
        banner = bytearray()
        banner.extend(cbmd)
        banner.extend(common_compressed)
        for region_data in regions_compressed:
            banner.extend(region_data)
        banner.extend(self.audio)
        
        # Write output
        with open(output_path, 'wb') as f:
            f.write(banner)
        
        print(f"\nBanner created: {output_path}")
        print(f"  Total size: {len(banner):,} bytes")
        
        return output_path
    
    def extract_common1(self, output_path):
        """Extract current COMMON1 texture to image file"""
        img = decode_rgb565_tiled(self.common_cgfx, self.COMMON1_OFFSET, 128, 128)
        img.save(output_path)
        print(f"Extracted COMMON1 to {output_path}")
        return img
    
    def extract_common2(self, output_path, region_index=1):
        """Extract current COMMON2 texture from specified region"""
        img = decode_la8_morton(self.region_templates[region_index], self.COMMON2_OFFSET, 256, 64)
        img.save(output_path)
        print(f"Extracted COMMON2 from region {self.REGIONS[region_index]} to {output_path}")
        return img


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Test the banner patcher"""
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description='GBA VC 3D Banner Patcher v2 (NSUI Compatible)')
    parser.add_argument('screen', nargs='?', help='COMMON2 footer image (256×64)')
    parser.add_argument('-c', '--cartridge', help='COMMON1 cartridge label (128×128)')
    parser.add_argument('--bg-color', help='Background color for cartridge as R,G,B (e.g., 128,0,128)')
    parser.add_argument('--fit-mode', choices=['fit', 'fill', 'stretch'], default='fit',
                        help='Cartridge label resize mode: fit (default), fill, or stretch')
    parser.add_argument('--title', help='Generate footer from title text')
    parser.add_argument('--subtitle', default='', help='Subtitle for generated footer')
    parser.add_argument('-t', '--template', help='Path to template directory')
    parser.add_argument('-o', '--output', default='banner.bnr', help='Output file')
    parser.add_argument('--bnr', action='store_true', help='Create complete .bnr file (default)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')

    args = parser.parse_args()

    # Parse background color if provided
    bg_color = None
    if args.bg_color:
        try:
            parts = args.bg_color.split(',')
            bg_color = (int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            print(f"Warning: Invalid bg-color format '{args.bg_color}', expected R,G,B")
    
    # Determine template directory
    template_dir = args.template or 'templates/gba_vc/nsui_template'
    
    # Check for nsui_template subdirectory
    if os.path.isdir(os.path.join(template_dir, 'nsui_template')):
        template_dir = os.path.join(template_dir, 'nsui_template')
    
    if not os.path.exists(template_dir):
        print(f"Error: Template directory not found: {template_dir}")
        sys.exit(1)
    
    # Check required files
    required = ['banner_common.cgfx', 'banner.bcwav', 'region_01_USA_EN.cgfx']
    for f in required:
        if not os.path.exists(os.path.join(template_dir, f)):
            print(f"Error: Missing template file: {f}")
            print(f"Template directory: {template_dir}")
            sys.exit(1)
    
    if not args.screen and not args.cartridge and not args.title:
        print("Error: Provide screen image, --cartridge, or --title")
        sys.exit(1)
    
    if args.verbose:
        print(f"Template: {template_dir}")
        print(f"COMMON1 offset: 0x{GBAVCBannerPatcher.COMMON1_OFFSET:X} (RGB565 Morton)")
        print(f"COMMON2 offset: 0x{GBAVCBannerPatcher.COMMON2_OFFSET:X} (LA8 Morton)")
    
    try:
        # Create patcher
        patcher = GBAVCBannerPatcher(template_dir)
        
        # Generate or use provided footer
        if args.title:
            footer_path = '/tmp/generated_footer.png'
            patcher.create_footer_image(args.title, args.subtitle, footer_path)
            args.screen = footer_path
            if args.verbose:
                print(f"Generated footer: {args.title}")
        
        # Patch COMMON2 (footer)
        if args.screen:
            patcher.patch_common2(args.screen)
        
        # Patch COMMON1 (cartridge label)
        if args.cartridge:
            patcher.patch_common1(args.cartridge, bg_color, args.fit_mode)
        
        # Build banner
        patcher.build_banner(args.output)
        
        print(f"Created: {args.output}")
        sys.exit(0)
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
