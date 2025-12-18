#!/usr/bin/env python3
"""
Universal VC Banner Patcher (Template: templates/gba_vc/universal_vc_template)

Patches two things in the template's uncompressed `banner.cgfx`:
- COMMON1 cartridge label (RGBA8) including its mip chain
- COMMON2 footer (LA8) to show title/subtitle

Offsets are for the bundled template file:
- COMMON1 mip chain (RGBA8 morton-tiled, ABGR byte order):
  - 128×128 @ 0x5880
  - 64×64   @ 0x15880
  - 32×32   @ 0x19880
  - 16×16   @ 0x1A880
  - 8×8     @ 0x1AC80
- COMMON2 footer (LA8 morton-tiled):
  - 256×64 @ 0x1AD80
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


def compress_lz11(data: bytes) -> bytes:
    """Compress data using the LZ11 algorithm."""
    result = bytearray()

    size = len(data)
    result.append(0x11)
    result.append(size & 0xFF)
    result.append((size >> 8) & 0xFF)
    result.append((size >> 16) & 0xFF)

    pos = 0
    while pos < len(data):
        block_flags = 0
        block_data = bytearray()

        for bit in range(8):
            if pos >= len(data):
                break

            best_len = 0
            best_disp = 0
            search_start = max(0, pos - 4096)
            max_len = min(0x10110, len(data) - pos)

            for search_pos in range(search_start, pos):
                match_len = 0
                while match_len < max_len:
                    src_pos = search_pos + (match_len % (pos - search_pos))
                    if data[src_pos] == data[pos + match_len]:
                        match_len += 1
                    else:
                        break
                if match_len >= 3 and match_len > best_len:
                    best_len = match_len
                    best_disp = pos - search_pos
                    if best_len == max_len:
                        break

            if best_len >= 3:
                block_flags |= (0x80 >> bit)
                disp_m1 = best_disp - 1

                if best_len <= 0x10:
                    byte1 = ((best_len - 1) << 4) | ((disp_m1 >> 8) & 0x0F)
                    byte2 = disp_m1 & 0xFF
                    block_data.extend([byte1, byte2])
                elif best_len <= 0x110:
                    adj_len = best_len - 0x11
                    byte1 = (adj_len >> 4) & 0x0F
                    byte2 = ((adj_len & 0x0F) << 4) | ((disp_m1 >> 8) & 0x0F)
                    byte3 = disp_m1 & 0xFF
                    block_data.extend([byte1, byte2, byte3])
                else:
                    adj_len = best_len - 0x111
                    byte1 = 0x10 | ((adj_len >> 12) & 0x0F)
                    byte2 = (adj_len >> 4) & 0xFF
                    byte3 = ((adj_len & 0x0F) << 4) | ((disp_m1 >> 8) & 0x0F)
                    byte4 = disp_m1 & 0xFF
                    block_data.extend([byte1, byte2, byte3, byte4])

                pos += best_len
            else:
                block_data.append(data[pos])
                pos += 1

        result.append(block_flags)
        result.extend(block_data)

    return bytes(result)


class UniversalVCBannerPatcher:
    REQUIRED_FILES = ["banner.cgfx", "banner.bcwav", "banner.cbmd"]

    # COMMON1 (label) mip chain offsets (RGBA8)
    LABEL_128_OFFSET = 0x5880
    LABEL_64_OFFSET = 0x15880
    LABEL_32_OFFSET = 0x19880
    LABEL_16_OFFSET = 0x1A880
    LABEL_8_OFFSET = 0x1AC80

    # COMMON2 footer offset (LA8, 256x64)
    FOOTER_OFFSET = 0x1AD80
    FOOTER_SIZE = (256, 64)

    def __init__(self, template_dir: str):
        self.template_dir = Path(template_dir)
        self._validate_template()

        self.cgfx_data = bytearray((self.template_dir / "banner.cgfx").read_bytes())
        self.bcwav_data = (self.template_dir / "banner.bcwav").read_bytes()
        self.cbmd_template = (self.template_dir / "banner.cbmd").read_bytes()

    def _validate_template(self) -> None:
        missing = [f for f in self.REQUIRED_FILES if not (self.template_dir / f).exists()]
        if missing:
            raise FileNotFoundError(f"Missing template files: {', '.join(missing)}")

    def _z_order_index(self, x: int, y: int) -> int:
        """Morton/Z-order index within an 8x8 tile."""
        return (
            ((x & 1))
            | ((y & 1) << 1)
            | ((x & 2) << 1)
            | ((y & 2) << 2)
            | ((x & 4) << 2)
            | ((y & 4) << 3)
        )

    def _encode_rgba8_tiled_abgr(self, img: "Image.Image", width: int, height: int) -> bytes:
        """Encode RGBA image to 3DS RGBA8 tiled format (ABGR in file)."""
        img = img.convert("RGBA")
        if img.size != (width, height):
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        px = img.load()

        tiles_x = width // 8
        tiles_y = height // 8
        out = bytearray(width * height * 4)
        pos = 0

        for ty in range(tiles_y):
            for tx in range(tiles_x):
                for py in range(8):
                    for px_i in range(8):
                        morton = self._z_order_index(px_i, py)
                        x = tx * 8 + px_i
                        y = ty * 8 + py
                        r, g, b, a = px[x, y]
                        # ABGR order
                        out[pos + morton * 4 + 0] = a
                        out[pos + morton * 4 + 1] = b
                        out[pos + morton * 4 + 2] = g
                        out[pos + morton * 4 + 3] = r
                pos += 256

        return bytes(out)

    def _decode_la8(self, offset: int, width: int, height: int) -> "Image.Image":
        """Decode LA8 morton-tiled texture from cgfx to RGBA image."""
        img = Image.new("RGBA", (width, height))
        px = img.load()

        tiles_x = width // 8
        tiles_y = height // 8

        for ty in range(tiles_y):
            for tx in range(tiles_x):
                tile_base = offset + (ty * tiles_x + tx) * 128
                for py in range(8):
                    for px_i in range(8):
                        morton = self._z_order_index(px_i, py)
                        i = tile_base + morton * 2
                        a = self.cgfx_data[i]
                        l = self.cgfx_data[i + 1]
                        x = tx * 8 + px_i
                        y = ty * 8 + py
                        px[x, y] = (l, l, l, a)
        return img

    def _encode_la8(self, img: "Image.Image", width: int, height: int) -> bytes:
        """Encode RGBA image to 3DS LA8 morton-tiled (alpha, luminance)."""
        img = img.convert("RGBA")
        if img.size != (width, height):
            img = img.resize((width, height), Image.Resampling.LANCZOS)
        px = img.load()

        tiles_x = width // 8
        tiles_y = height // 8
        out = bytearray(width * height * 2)

        for ty in range(tiles_y):
            for tx in range(tiles_x):
                tile_base = (ty * tiles_x + tx) * 128
                for py in range(8):
                    for px_i in range(8):
                        morton = self._z_order_index(px_i, py)
                        x = tx * 8 + px_i
                        y = ty * 8 + py
                        r, g, b, a = px[x, y]
                        l = (r + g + b) // 3
                        i = tile_base + morton * 2
                        out[i] = a
                        out[i + 1] = l
        return bytes(out)

    def _fit_image(
        self,
        img: "Image.Image",
        width: int,
        height: int,
        bg_color: Optional[Tuple[int, int, int]] = None,
    ) -> "Image.Image":
        """Resize to fit within width/height; center on background (transparent or bg_color)."""
        img = img.convert("RGBA")

        if bg_color:
            canvas = Image.new("RGBA", (width, height), (*bg_color, 255))
        else:
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        img_ratio = img.width / img.height
        target_ratio = width / height
        if img_ratio > target_ratio:
            new_w = width
            new_h = max(1, int(width / img_ratio))
        else:
            new_h = height
            new_w = max(1, int(height * img_ratio))

        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        x = (width - new_w) // 2
        y = (height - new_h) // 2
        canvas.paste(img, (x, y), img)
        return canvas

    def _default_label_bg_color(self, offset: int, width: int, height: int) -> Tuple[int, int, int]:
        """
        Derive a reasonable default label background color from the template's
        existing COMMON1 texture (at `offset`) by averaging non-transparent pixels.

        This helps keep the front/back cartridge tint consistent even when the
        label image doesn't fill the whole square (fit mode).
        """
        region = self.cgfx_data[offset : offset + (width * height * 4)]
        if len(region) < width * height * 4:
            return (128, 0, 192)

        rs = gs = bs = count = 0
        for i in range(0, len(region), 4):
            a = region[i]
            b = region[i + 1]
            g = region[i + 2]
            r = region[i + 3]
            if a == 0:
                continue
            rs += r
            gs += g
            bs += b
            count += 1

        if count == 0:
            return (128, 0, 192)

        return (rs // count, gs // count, bs // count)

    def patch_cartridge_label(self, image_path: str, bg_color: Optional[Tuple[int, int, int]] = None) -> None:
        if Image is None:
            print("Warning: Pillow (PIL) not available; skipping cartridge label patch")
            return

        img = Image.open(image_path)
        print(f"  Patching COMMON1 label: {image_path}")

        mip_specs = [
            (128, 128, self.LABEL_128_OFFSET),
            (64, 64, self.LABEL_64_OFFSET),
            (32, 32, self.LABEL_32_OFFSET),
            (16, 16, self.LABEL_16_OFFSET),
            (8, 8, self.LABEL_8_OFFSET),
        ]

        for w, h, off in mip_specs:
            effective_bg = bg_color if bg_color is not None else self._default_label_bg_color(off, w, h)
            mip_img = self._fit_image(img, w, h, effective_bg)
            encoded = self._encode_rgba8_tiled_abgr(mip_img, w, h)
            self.cgfx_data[off : off + len(encoded)] = encoded
            print(f"    mip {w}x{h} -> 0x{off:X} ({len(encoded)} bytes)")

    def patch_footer_text(self, title: str, subtitle: Optional[str] = None) -> None:
        if Image is None:
            print("Warning: Pillow (PIL) not available; skipping footer patch")
            return

        title = (title or "").strip()
        subtitle = (subtitle or "").strip()
        if not title:
            return

        footer_w, footer_h = self.FOOTER_SIZE
        footer = self._decode_la8(self.FOOTER_OFFSET, footer_w, footer_h)
        draw = ImageDraw.Draw(footer)

        # Fonts: match GBA VC template if available (SCE-PS3-RD-R-LATIN.TTF, 16/12)
        font_title = None
        font_sub = None

        candidate_fonts = []
        # 1) If the user has the NSUI template alongside the Universal VC template, reuse its bundled font.
        candidate_fonts.append(self.template_dir.parent / "nsui_template" / "SCE-PS3-RD-R-LATIN.TTF")
        # 2) In case template dir contains it
        candidate_fonts.append(self.template_dir / "SCE-PS3-RD-R-LATIN.TTF")
        # 3) System fallbacks
        candidate_fonts.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
            ]
        )

        for fp in candidate_fonts:
            try:
                if fp.exists():
                    font_title = ImageFont.truetype(str(fp), 16)
                    font_sub = ImageFont.truetype(str(fp), 12)
                    break
            except Exception:
                continue

        if font_title is None:
            font_title = ImageFont.load_default()
            font_sub = font_title

        # Right box area
        box_left = 92
        box_right = 252
        box_top = 8
        box_bottom = 56
        max_w = box_right - box_left
        center_x = (box_left + box_right) // 2

        def measure(text: str, font) -> tuple[int, int]:
            bbox = draw.textbbox((0, 0), text, font=font)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]

        # Wrap title if needed
        words = title.split()
        lines: list[str] = []
        current: list[str] = []
        for word in words:
            test = " ".join(current + [word])
            if measure(test, font_title)[0] <= max_w:
                current.append(word)
            else:
                if current:
                    lines.append(" ".join(current))
                current = [word]
        if current:
            lines.append(" ".join(current))

        if len(lines) >= 2:
            subtitle = ""

        color_title = (32, 32, 32, 255)
        color_sub = (40, 40, 40, 255)

        if subtitle and lines:
            tw, th = measure(lines[0], font_title)
            sw, sh = measure(subtitle, font_sub)
            total_h = th + 4 + sh
            y = box_top + (box_bottom - box_top - total_h) // 2
            draw.text((center_x - tw // 2, y), lines[0], fill=color_title, font=font_title)
            draw.text((center_x - sw // 2, y + th + 4), subtitle, fill=color_sub, font=font_sub)
        elif len(lines) == 1:
            tw, th = measure(lines[0], font_title)
            y = box_top + (box_bottom - box_top - th) // 2
            draw.text((center_x - tw // 2, y), lines[0], fill=color_title, font=font_title)
        else:
            lines = (lines or [title])[:3]
            heights = [measure(ln, font_title)[1] for ln in lines]
            total_h = sum(heights) + 2 * (len(lines) - 1)
            y = box_top + (box_bottom - box_top - total_h) // 2
            for ln in lines:
                tw, th = measure(ln, font_title)
                draw.text((center_x - tw // 2, y), ln, fill=color_title, font=font_title)
                y += th + 2

        encoded = self._encode_la8(footer, footer_w, footer_h)
        self.cgfx_data[self.FOOTER_OFFSET : self.FOOTER_OFFSET + len(encoded)] = encoded
        print(f"  Patched COMMON2 footer @ 0x{self.FOOTER_OFFSET:X}")

    def build_banner(self, output_path: str) -> str:
        output_path = str(output_path)

        cgfx_compressed = compress_lz11(bytes(self.cgfx_data))
        padding = (4 - (len(cgfx_compressed) % 4)) % 4
        cgfx_compressed += b"\x00" * padding

        cbmd = bytearray(self.cbmd_template)
        cgfx_offset = 0x88
        struct.pack_into("<I", cbmd, 0x08, cgfx_offset)
        cwav_offset = cgfx_offset + len(cgfx_compressed)
        struct.pack_into("<I", cbmd, 0x84, cwav_offset)

        banner = bytearray()
        banner.extend(cbmd)
        banner.extend(cgfx_compressed)
        banner.extend(self.bcwav_data)

        with open(output_path, "wb") as f:
            f.write(banner)

        return output_path


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Universal VC Banner Patcher (mGBA Forwarder Tools)")
    parser.add_argument("-t", "--template", required=True, help="Path to universal VC template directory")
    parser.add_argument("-o", "--output", default="banner.bnr", help="Output banner file")
    parser.add_argument("--cartridge", "--label", help="Cartridge label image (any format)")
    parser.add_argument("--bg-color", help="Background color R,G,B for label (e.g. 50,50,70)")
    parser.add_argument("--title", help="Footer title text")
    parser.add_argument("--subtitle", help="Footer subtitle text")
    args = parser.parse_args()

    bg_color = None
    if args.bg_color:
        parts = args.bg_color.split(",")
        if len(parts) == 3:
            bg_color = (int(parts[0]), int(parts[1]), int(parts[2]))

    patcher = UniversalVCBannerPatcher(args.template)

    if args.cartridge:
        patcher.patch_cartridge_label(args.cartridge, bg_color)

    if args.title or args.subtitle:
        patcher.patch_footer_text(args.title or "", args.subtitle)

    out = patcher.build_banner(args.output)
    print(f"Success! Created: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
