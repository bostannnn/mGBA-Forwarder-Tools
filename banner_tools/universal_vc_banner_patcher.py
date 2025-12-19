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
    """
    Compress data using a faster, simplified LZ11 encoder.

    This prioritises speed over maximum compression ratio; acceptable for banners.
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
                # Find last occurrence of the next 3 bytes to seed the search.
                seed = bytes(data[pos : pos + 3])
                search_end = len(window)
                search_pos = bytes(window).rfind(seed, 0, search_end)
                while search_pos != -1:
                    disp = pos - (window_start + search_pos)
                    # Extend match using slices for speed.
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

    # COMMON3 texture (ETC1, 128x128, 8192 bytes). Used for cartridge/background tinting.
    SHELL_ETC1_OFFSET = 0x23C70
    SHELL_ETC1_SIZE = 0x2000

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

    @staticmethod
    def _etc1_solid_block(rgb: Tuple[int, int, int]) -> bytes:
        """
        Build a single ETC1 block approximating a solid RGB color.

        This is intentionally limited to *solid fills only* to recolor the
        Universal VC COMMON3 texture without relying on external encoders.
        """

        r, g, b = rgb

        def clamp_u8(v: int) -> int:
            return 0 if v < 0 else 255 if v > 255 else v

        # Using table 0 with selector 0 applies a negative modifier (≈ -8).
        # Compensate so the decoded color is closer to the requested one.
        base_r = clamp_u8(r + 8)
        base_g = clamp_u8(g + 8)
        base_b = clamp_u8(b + 8)

        def to_5bit(v: int) -> int:
            return int(round(v * 31 / 255)) & 0x1F

        r5 = to_5bit(base_r)
        g5 = to_5bit(base_g)
        b5 = to_5bit(base_b)

        # Differential mode, same color for both subblocks
        dr = dg = db = 0
        table1 = 0
        table2 = 0
        diff = 1
        flip = 0

        # ETC1 high 32-bit word (standard layout)
        hi32 = (
            (r5 << 27)
            | (g5 << 22)
            | (b5 << 17)
            | (dr << 14)
            | (dg << 11)
            | (db << 8)
            | (table1 << 5)
            | (table2 << 2)
            | (diff << 1)
            | flip
        )
        low32 = 0x00000000  # selector bits all 0 => uniform
        # CGFX data is little-endian; store the block accordingly.
        return ((hi32 << 32) | low32).to_bytes(8, "little")

    def patch_shell_color(self, shell_color: Tuple[int, int, int]) -> None:
        """Patch COMMON3 (ETC1) to a solid color so front/back match."""
        off = self.SHELL_ETC1_OFFSET
        size = self.SHELL_ETC1_SIZE
        if off + size > len(self.cgfx_data):
            print(
                f"Warning: COMMON3 ETC1 region out of range (0x{off:X}..0x{off+size:X}); skipping shell recolor"
            )
            return

        block = self._etc1_solid_block(shell_color)
        self.cgfx_data[off : off + size] = block * (size // 8)
        print(f"  Patched COMMON3 shell color @ 0x{off:X} (ETC1 solid RGB{shell_color})")

    def patch_cartridge_label(self, image_path: str, bg_color: Optional[Tuple[int, int, int]] = None) -> None:
        if Image is None:
            print("Warning: Pillow (PIL) not available; skipping cartridge label patch")
            return

        img = Image.open(image_path)
        print(f"  Patching COMMON1 label: {image_path}")

        # Build a framed 128x128 label (transparent outside, purple rounded border, centered art),
        # then downscale to smaller mips to keep the border thickness consistent.
        outer_size = 128
        inner_w, inner_h = 120, 80  # closer to GBA VC proportions (thicker border)
        offset_x = (outer_size - inner_w) // 2  # 4 px
        offset_y = (outer_size - inner_h) // 2  # 24 px
        border_color = (140, 110, 200, 255)  # Soft purple border

        # Fit art inside inner box (preserve aspect, no crop).
        inner_img = self._fit_image(img, inner_w, inner_h, bg_color)

        framed_128 = Image.new("RGBA", (outer_size, outer_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(framed_128)
        draw.rounded_rectangle((0, 0, outer_size - 1, outer_size - 1), radius=12, fill=border_color)

        # Paste inner art with rounded mask to soften corners.
        mask = Image.new("L", (inner_w, inner_h), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, inner_w - 1, inner_h - 1), radius=8, fill=255)
        framed_128.paste(inner_img, (offset_x, offset_y), mask)

        mip_specs = [
            (128, 128, self.LABEL_128_OFFSET),
            (64, 64, self.LABEL_64_OFFSET),
            (32, 32, self.LABEL_32_OFFSET),
            (16, 16, self.LABEL_16_OFFSET),
            (8, 8, self.LABEL_8_OFFSET),
        ]

        for w, h, off in mip_specs:
            mip_img = framed_128.resize((w, h), Image.Resampling.LANCZOS)
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
        footer = self.create_footer_image(title, subtitle)
        if footer is None:
            return

        encoded = self._encode_la8(footer.convert("RGBA"), footer_w, footer_h)
        self.cgfx_data[self.FOOTER_OFFSET : self.FOOTER_OFFSET + len(encoded)] = encoded
        print(f"  Patched COMMON2 footer @ 0x{self.FOOTER_OFFSET:X}")

    def create_footer_image(self, title: str, subtitle: Optional[str] = None) -> "Image.Image | None":
        """Render a PIL image for the footer (COMMON2) using the template background."""
        if Image is None:
            return None

        title = (title or "").strip()
        subtitle = (subtitle or "").strip()
        if not title:
            return None

        # Reuse GBA VC footer rendering for 1:1 layout/parity.
        try:
            from banner_tools.gba_vc_banner_patcher import GBAVCBannerPatcher

            gba = GBAVCBannerPatcher(str(self.template_dir.parent / "nsui_template"))
            footer_img = gba.create_footer_image(title, subtitle)
            return footer_img
        except Exception as e:
            print(f"Warning: failed to render GBA VC footer: {e}")
            return None

    def _draw_virtual_console_branding(self, draw: "ImageDraw.ImageDraw", font, badge_rect: tuple[int, int, int, int]) -> None:
        """Draw the two-line 'Virtual Console' badge text inside the given rect."""
        if font is None or draw is None:
            return

        box_left, box_top, box_right, box_bottom = badge_rect
        lines = ["Virtual", "Console"]
        total_h = 0
        line_metrics = []
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
            line_metrics.append((ln, w, h))
            total_h += h
        spacing = 2
        total_h += spacing
        y = box_top + (box_bottom - box_top - total_h) // 2
        for ln, w, h in line_metrics:
            x = box_left + (box_right - box_left - w) // 2
            draw.text((x, y), ln, fill=(245, 245, 245, 255), font=font)
            y += h + spacing

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
    parser.add_argument("--shell-color", help="Cartridge/background color R,G,B (solid fill; e.g. 80,40,120)")
    parser.add_argument("--title", help="Footer title text")
    parser.add_argument("--subtitle", help="Footer subtitle text")
    args = parser.parse_args()

    bg_color = None
    if args.bg_color:
        parts = args.bg_color.split(",")
        if len(parts) == 3:
            bg_color = (int(parts[0]), int(parts[1]), int(parts[2]))

    shell_color = None
    if args.shell_color:
        parts = args.shell_color.split(",")
        if len(parts) == 3:
            shell_color = (int(parts[0]), int(parts[1]), int(parts[2]))

    patcher = UniversalVCBannerPatcher(args.template)

    if shell_color:
        patcher.patch_shell_color(shell_color)

    if args.cartridge:
        # If the user picked a shell tint but no explicit bg, use the shell color for label background
        # so the front and back stay consistent.
        effective_bg = bg_color if bg_color is not None else shell_color
        patcher.patch_cartridge_label(args.cartridge, effective_bg)

    if args.title or args.subtitle:
        patcher.patch_footer_text(args.title or "", args.subtitle)

    out = patcher.build_banner(args.output)
    print(f"Success! Created: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
