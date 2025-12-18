# Universal VC Template

This is the "Custom Virtual Console" / "Universal VC" banner template for 3DS forwarders.

## Description

Unlike the NSUI GBA VC template which uses official Nintendo Virtual Console styling, this template provides a custom "Virtual Console" look that can be used for any console type.

The banner features:
- "Custom Virtual Console" branding in the footer
- Red cartridge placeholder for game box art
- Purple gradient background
- Spinning 3D cartridge animation

## Template Files

### Required Files

- `banner.cgfx` - Uncompressed CGFX model containing 3D banner data
- `banner.bcwav` - Audio file (plays when banner is shown)
- `banner.cbmd` - CBMD header template

### Reference PNG Files

These show what textures are embedded in the banner:

- `COMMON1.png` - Cartridge label (128×128) - red placeholder
- `COMMON1_2.png` through `COMMON1_5.png` - Mipmaps (64×64, 32×32, 16×16, 8×8)
- `COMMON2.png` - Footer text (256×64) - "Custom Virtual Console"
- `COMMON3.png` - Background (128×128) - purple gradient

## How It Works

The banner is built by:

1. Starting with `banner.cgfx` (uncompressed CGFX file)
2. LZ11 compressing the CGFX data
3. Building a CBMD header with correct offsets
4. Concatenating: CBMD header + compressed CGFX + BCWAV audio

## Texture Patching

**Note:** Full texture patching (replacing COMMON1, COMMON2 textures with custom images) requires additional tools like tex3ds or Ohana3DS. The current implementation uses the template textures as-is.

For custom cartridge labels and footer text, use an external tool like NSUI (New Super Ultimate Injector) to patch the textures before using this template.

## Credits

- Original template from GBATemp community
- Compatible with NSUI (New Super Ultimate Injector)

## File Structure

```
universal_vc_template/
├── banner.cgfx      # 3D model with embedded textures (172 KB)
├── banner.bcwav     # Audio (384 KB)
├── banner.cbmd      # Header template (136 bytes)
├── COMMON1.png      # Cartridge label reference (128×128)
├── COMMON1_2.png    # Mipmap 64×64
├── COMMON1_3.png    # Mipmap 32×32
├── COMMON1_4.png    # Mipmap 16×16
├── COMMON1_5.png    # Mipmap 8×8
├── COMMON2.png      # Footer text reference (256×64)
├── COMMON3.png      # Background reference (128×128)
└── README.md        # This file
```
