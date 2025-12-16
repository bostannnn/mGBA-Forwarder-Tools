"""
3DS Banner Tools

Tools for creating and editing Nintendo 3DS banners, boot splashes, and related files.

Modules:
- banner_editor: Edit banner.bnr files (extract, fix locales, enable color)
- boot_splash: Create custom boot splash screens
- gba_vc_banner: GBA Virtual Console specific banner handling
"""

from .banner_editor import BannerEditor, BCMDLEditor
from .boot_splash import create_boot_splash, create_gba_vc_splash

__all__ = [
    'BannerEditor',
    'BCMDLEditor', 
    'create_boot_splash',
    'create_gba_vc_splash',
]
