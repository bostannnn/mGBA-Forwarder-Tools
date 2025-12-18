#!/usr/bin/env python3
"""
mGBA 3DS Forwarder Creator - GTK4/Adwaita Edition with Docker Support
Supports multiple banner templates: NSUI GBA VC and Universal VC
Uses zenity for file dialogs to avoid GTK4 FileChooser schema issues
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf

import subprocess
import tempfile
import shutil
import os
from pathlib import Path
from threading import Thread


# =============================================================================
# TEMPLATE DEFINITIONS
# =============================================================================

TEMPLATES = {
    'gba_vc': {
        'name': 'GBA VC',
        'path': 'templates/gba_vc/nsui_template',
        'description': 'Official GBA Virtual Console style with spinning cartridge',
        'patcher': 'gba_vc_banner_patcher.py',
        'required_files': ['banner_common.cgfx', 'banner.bcwav', 'region_01_USA_EN.cgfx'],
    },
    'universal_vc': {
        'name': 'Universal VC',
        'path': 'templates/gba_vc/universal_vc_template',
        'description': 'Custom Virtual Console style - community template',
        'patcher': 'universal_vc_banner_patcher.py',
        'required_files': ['banner.cgfx', 'banner.bcwav', 'banner.cbmd'],
    },
}

DEFAULT_TEMPLATE = 'gba_vc'


def check_docker():
    """Check if Docker is available and image is built."""
    try:
        result = subprocess.run(['docker', 'images', '-q', 'mgba-forwarder'],
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return 'ready' if result.stdout.strip() else 'no_image'
        return 'not_found'
    except FileNotFoundError:
        return 'not_found'
    except Exception:
        return 'error'


def pick_file_zenity_async(title="Select File", filters=None, callback=None):
    """Use zenity for file selection (non-blocking)."""
    def run_dialog():
        cmd = ['zenity', '--file-selection', '--title', title]
        if filters:
            for name, patterns in filters:
                cmd.extend(['--file-filter', f"{name} | {' '.join(patterns)}"])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and callback:
                GLib.idle_add(callback, result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    Thread(target=run_dialog, daemon=True).start()


def pick_folder_zenity_async(title="Select Folder", callback=None):
    """Use zenity for folder selection (non-blocking)."""
    def run_dialog():
        cmd = ['zenity', '--file-selection', '--directory', '--title', title]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and callback:
                GLib.idle_add(callback, result.stdout.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    Thread(target=run_dialog, daemon=True).start()


class ForwarderWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="mGBA Forwarder Creator")
        self.set_default_size(550, 950)
        
        self.script_dir = Path(__file__).parent.absolute()
        self.banner_tools_dir = self.script_dir / "banner_tools"
        
        # Template state
        self.current_template_key = DEFAULT_TEMPLATE
        self.template_dir = self._get_template_path(DEFAULT_TEMPLATE)
        
        self.docker_status = check_docker()
        
        # Paths
        self.icon_path = None
        self.cartridge_path = None
        self.output_path = Path.home() / "3ds-forwarders"

        # Cartridge background color (default: transparent/black)
        self.cartridge_bg_color = None  # None means transparent

        # Track if user manually edited footer title
        self._footer_title_manually_edited = False

        self._setup_ui()
    
    def _get_template_path(self, template_key):
        if template_key not in TEMPLATES:
            template_key = DEFAULT_TEMPLATE
        template_path = TEMPLATES[template_key]['path']
        if os.path.isabs(template_path):
            return Path(template_path)
        return self.script_dir / template_path
    
    def _get_patcher_script(self, template_key):
        if template_key not in TEMPLATES:
            template_key = DEFAULT_TEMPLATE
        patcher = TEMPLATES[template_key]['patcher']
        return self.banner_tools_dir / patcher
    
    def _setup_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        header = Adw.HeaderBar()
        main_box.append(header)
        
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        main_box.append(scrolled)
        
        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        scrolled.set_child(clamp)
        
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        clamp.set_child(content)
        
        # === BASIC SETTINGS ===
        basic_group = Adw.PreferencesGroup(
            title="Basic Settings",
            description="Configure your forwarder"
        )
        content.append(basic_group)
        
        self.game_name_row = Adw.EntryRow(title="Game Name")
        self.game_name_row.set_text("")
        basic_group.add(self.game_name_row)
        
        self.rom_path_row = Adw.EntryRow(title="ROM Path on SD")
        self.rom_path_row.set_text("/roms/gba/")
        basic_group.add(self.rom_path_row)
        
        self.output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=str(self.output_path)
        )
        output_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        output_btn.connect("clicked", self.on_browse_output)
        self.output_row.add_suffix(output_btn)
        basic_group.add(self.output_row)
        
        # === ICON ===
        icon_group = Adw.PreferencesGroup(
            title="Home Menu Icon",
            description="Icon shown on 3DS home screen"
        )
        content.append(icon_group)
        
        self.icon_row = Adw.ActionRow(
            title="Icon Image",
            subtitle="48x48 PNG (any size, auto-resized)"
        )
        icon_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        icon_clear_btn.add_css_class("flat")
        icon_clear_btn.connect("clicked", lambda b: self._clear_path("icon"))
        icon_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        icon_btn.connect("clicked", self.on_browse_icon)
        self.icon_row.add_suffix(icon_clear_btn)
        self.icon_row.add_suffix(icon_btn)
        icon_group.add(self.icon_row)
        
        # === 3D BANNER ===
        banner_group = Adw.PreferencesGroup(
            title="3D Banner",
            description="Animated banner shown when hovering over the icon"
        )
        content.append(banner_group)
        
        # Template Selection
        self.template_combo_row = Adw.ComboRow(title="Banner Template")
        template_model = Gtk.StringList()
        self.template_keys = []
        for key, info in TEMPLATES.items():
            template_model.append(info['name'])
            self.template_keys.append(key)
        self.template_combo_row.set_model(template_model)
        
        if DEFAULT_TEMPLATE in self.template_keys:
            idx = self.template_keys.index(DEFAULT_TEMPLATE)
            self.template_combo_row.set_selected(idx)
            self.template_combo_row.set_subtitle(TEMPLATES[DEFAULT_TEMPLATE]['description'])
        
        self.template_combo_row.connect("notify::selected", self._on_template_changed)
        banner_group.add(self.template_combo_row)
        
        # Template status
        self.template_status_row = Adw.ActionRow(title="Template Status")
        self._check_template()
        banner_group.add(self.template_status_row)
        
        # Cartridge Label
        self.cartridge_row = Adw.ActionRow(
            title="Cartridge Label",
            subtitle="128x128 box art (fit mode, no cropping)"
        )
        cart_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        cart_clear_btn.add_css_class("flat")
        cart_clear_btn.connect("clicked", lambda b: self._clear_path("cartridge"))
        cart_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        cart_btn.connect("clicked", self.on_browse_cartridge)
        self.cartridge_row.add_suffix(cart_clear_btn)
        self.cartridge_row.add_suffix(cart_btn)
        banner_group.add(self.cartridge_row)

        # Cartridge Background Color
        self.cartridge_color_row = Adw.ActionRow(
            title="Cartridge Background",
            subtitle="Background color for label (click to change)"
        )
        # Color preview box
        self.color_preview_box = Gtk.DrawingArea()
        self.color_preview_box.set_size_request(32, 32)
        self.color_preview_box.set_valign(Gtk.Align.CENTER)
        self.color_preview_box.set_draw_func(self._draw_color_preview)

        color_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        color_clear_btn.add_css_class("flat")
        color_clear_btn.set_tooltip_text("Reset to transparent")
        color_clear_btn.connect("clicked", self._clear_cartridge_color)

        color_btn = Gtk.Button(label="Pick Color", valign=Gtk.Align.CENTER)
        color_btn.connect("clicked", self.on_pick_cartridge_color)

        self.cartridge_color_row.add_suffix(self.color_preview_box)
        self.cartridge_color_row.add_suffix(color_clear_btn)
        self.cartridge_color_row.add_suffix(color_btn)
        banner_group.add(self.cartridge_color_row)

        # Footer Title
        self.footer_title_row = Adw.EntryRow(title="Footer Title")
        self.footer_title_row.set_text("")
        banner_group.add(self.footer_title_row)
        
        # Footer Subtitle (with "Released: " prefix)
        self.footer_subtitle_row = Adw.EntryRow(title="Release Year (optional)")
        self.footer_subtitle_row.set_text("")
        banner_group.add(self.footer_subtitle_row)
        
        self.game_name_row.connect("changed", self._on_game_name_changed)
        self.footer_title_row.connect("changed", self._on_footer_title_changed)
        self.footer_subtitle_row.connect("changed", self._update_preview)

        # === BANNER PREVIEW ===
        preview_group = Adw.PreferencesGroup(
            title="Banner Preview",
            description="Preview of cartridge label and footer text"
        )
        content.append(preview_group)

        # Preview container
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        preview_box.set_halign(Gtk.Align.CENTER)
        preview_box.set_margin_top(8)
        preview_box.set_margin_bottom(8)

        # Icon preview frame
        icon_frame = Gtk.Frame()
        icon_frame.set_size_request(48, 48)
        self.icon_preview = Gtk.Picture()
        self.icon_preview.set_size_request(48, 48)
        self.icon_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        icon_frame.set_child(self.icon_preview)

        icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        icon_box.append(icon_frame)
        icon_label = Gtk.Label(label="Icon")
        icon_label.add_css_class("dim-label")
        icon_label.add_css_class("caption")
        icon_box.append(icon_label)
        preview_box.append(icon_box)

        # Cartridge preview frame
        cartridge_frame = Gtk.Frame()
        cartridge_frame.set_size_request(128, 128)
        self.cartridge_preview = Gtk.Picture()
        self.cartridge_preview.set_size_request(128, 128)
        self.cartridge_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        cartridge_frame.set_child(self.cartridge_preview)

        cartridge_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        cartridge_box.append(cartridge_frame)
        cartridge_label = Gtk.Label(label="Cartridge Label")
        cartridge_label.add_css_class("dim-label")
        cartridge_label.add_css_class("caption")
        cartridge_box.append(cartridge_label)
        preview_box.append(cartridge_box)

        # Footer preview frame
        footer_frame = Gtk.Frame()
        footer_frame.set_size_request(256, 64)
        self.footer_preview = Gtk.Picture()
        self.footer_preview.set_size_request(256, 64)
        self.footer_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        footer_frame.set_child(self.footer_preview)

        footer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer_box.append(footer_frame)
        footer_label = Gtk.Label(label="Footer Text")
        footer_label.add_css_class("dim-label")
        footer_label.add_css_class("caption")
        footer_box.append(footer_label)
        preview_box.append(footer_box)

        # Wrap in an ActionRow for consistent styling
        preview_row = Adw.ActionRow()
        preview_row.set_child(preview_box)
        preview_group.add(preview_row)

        # Initialize preview with placeholder
        self._update_preview()

        # === DOCKER STATUS ===
        docker_group = Adw.PreferencesGroup(
            title="Docker Build System",
            description="CIA building requires Docker"
        )
        content.append(docker_group)
        
        self.docker_row = Adw.ActionRow(title="Docker Status")
        docker_group.add(self.docker_row)

        self.docker_rebuild_btn = Gtk.Button(label="Rebuild", valign=Gtk.Align.CENTER)
        self.docker_rebuild_btn.connect("clicked", self.on_rebuild_docker)
        self.docker_rebuild_btn.set_tooltip_text("Rebuild Docker image from scratch")
        self.docker_row.add_suffix(self.docker_rebuild_btn)

        self.docker_build_btn = Gtk.Button(label="Build", valign=Gtk.Align.CENTER)
        self.docker_build_btn.connect("clicked", self.on_build_docker)
        self.docker_row.add_suffix(self.docker_build_btn)

        self._update_docker_status()
        
        # === BOTTOM SECTION ===
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom_box.set_margin_top(12)
        content.append(bottom_box)
        
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_visible(False)
        bottom_box.append(self.progress_bar)
        
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("dim-label")
        bottom_box.append(self.status_label)
        
        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.CENTER)
        bottom_box.append(button_box)

        self.preview_btn = Gtk.Button(label="Preview Banner")
        self.preview_btn.connect("clicked", self.on_preview_banner)
        self.preview_btn.set_tooltip_text("Generate and preview banner textures")
        button_box.append(self.preview_btn)

        self.compare_btn = Gtk.Button(label="Compare")
        self.compare_btn.connect("clicked", self.on_compare_banner)
        self.compare_btn.set_tooltip_text("Compare with template and working reference")
        button_box.append(self.compare_btn)

        self.banner_btn = Gtk.Button(label="Create Banner Only")
        self.banner_btn.connect("clicked", self.on_create_banner_only)
        button_box.append(self.banner_btn)

        self.create_btn = Gtk.Button(label="Create CIA Forwarder")
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.connect("clicked", self.on_create)
        button_box.append(self.create_btn)

        # Second row for testing buttons
        test_button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        test_button_box.set_halign(Gtk.Align.CENTER)
        test_button_box.set_margin_top(6)
        bottom_box.append(test_button_box)

        self.emulator_btn = Gtk.Button(label="Test in Emulator")
        self.emulator_btn.connect("clicked", self.on_test_emulator)
        self.emulator_btn.set_tooltip_text("Open last created CIA in Citra/Lime3DS")
        test_button_box.append(self.emulator_btn)
    
    def _on_game_name_changed(self, entry):
        """Sync footer title with game name unless user manually edited it."""
        if not self._footer_title_manually_edited:
            # Block the footer changed signal temporarily to avoid marking as manual edit
            self.footer_title_row.handler_block_by_func(self._on_footer_title_changed)
            self.footer_title_row.set_text(entry.get_text())
            self.footer_title_row.handler_unblock_by_func(self._on_footer_title_changed)
            self._update_preview()

    def _on_footer_title_changed(self, entry):
        """Track manual edits to footer title."""
        # If footer title differs from game name, user manually edited it
        game_name = self.game_name_row.get_text()
        footer_title = entry.get_text()
        if footer_title != game_name:
            self._footer_title_manually_edited = True
        elif not footer_title:
            # If cleared, allow syncing again
            self._footer_title_manually_edited = False
        self._update_preview()
    
    def _on_template_changed(self, combo, param):
        idx = combo.get_selected()
        if idx < len(self.template_keys):
            self.current_template_key = self.template_keys[idx]
            self.template_dir = self._get_template_path(self.current_template_key)
            combo.set_subtitle(TEMPLATES[self.current_template_key]['description'])
            self._check_template()
    
    def _check_template(self):
        template_info = TEMPLATES.get(self.current_template_key)
        if not template_info:
            self.template_status_row.set_subtitle("Unknown template")
            return False
        
        if not self.template_dir.exists():
            self.template_status_row.set_subtitle("Template folder not found")
            return False
        
        missing = []
        for f in template_info['required_files']:
            if not (self.template_dir / f).exists():
                missing.append(f)
        
        if missing:
            self.template_status_row.set_subtitle(f"Missing: {', '.join(missing)}")
            return False
        
        self.template_status_row.set_subtitle("Template ready")
        return True
    
    def _update_docker_status(self):
        if self.docker_status == 'ready':
            self.docker_row.set_subtitle("Ready")
            self.docker_build_btn.set_sensitive(False)
            self.docker_rebuild_btn.set_sensitive(True)
        elif self.docker_status == 'no_image':
            self.docker_row.set_subtitle("Image not built")
            self.docker_build_btn.set_sensitive(True)
            self.docker_rebuild_btn.set_sensitive(False)
        else:
            self.docker_row.set_subtitle("Docker not found")
            self.docker_build_btn.set_sensitive(True)
            self.docker_rebuild_btn.set_sensitive(False)
    
    def _clear_path(self, path_type):
        if path_type == "icon":
            self.icon_path = None
            self.icon_row.set_subtitle("48x48 PNG (any size, auto-resized)")
            self._update_preview()
        elif path_type == "cartridge":
            self.cartridge_path = None
            self.cartridge_row.set_subtitle("128x128 box art (fit mode, no cropping)")
            self._update_preview()

    def _update_preview(self, *args):
        """Update the banner preview images."""
        # Update icon preview
        if self.icon_path and self.icon_path.exists():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(self.icon_path), 48, 48, True
                )
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                self.icon_preview.set_paintable(texture)
            except Exception:
                self.icon_preview.set_paintable(None)
        else:
            self.icon_preview.set_paintable(None)

        # Update cartridge preview
        if self.cartridge_path and self.cartridge_path.exists():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    str(self.cartridge_path), 128, 128, True
                )
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                self.cartridge_preview.set_paintable(texture)
            except Exception:
                self.cartridge_preview.set_paintable(None)
        else:
            self.cartridge_preview.set_paintable(None)

        # Update footer preview (generate text image)
        self._generate_footer_preview()

    def _generate_footer_preview(self):
        """Generate footer text preview image."""
        title = self.footer_title_row.get_text().strip()
        subtitle = self.footer_subtitle_row.get_text().strip()

        if not title:
            self.footer_preview.set_paintable(None)
            return

        # Add "Released: " prefix to subtitle if present
        if subtitle:
            subtitle = f"Released: {subtitle}"

        # Generate preview using Python/PIL in background
        Thread(target=self._do_generate_footer_preview, args=(title, subtitle), daemon=True).start()

    def _do_generate_footer_preview(self, title, subtitle):
        """Generate footer preview in background thread."""
        try:
            import tempfile
            preview_path = Path(tempfile.gettempdir()) / "footer_preview.png"

            # Try to use the banner patcher to generate footer preview
            if self.current_template_key == 'gba_vc':
                patcher_script = self._get_patcher_script('gba_vc')
                cmd = [
                    'python3', '-c', f'''
import sys
sys.path.insert(0, "{self.banner_tools_dir}")
from gba_vc_banner_patcher import GBAVCBannerPatcher
patcher = GBAVCBannerPatcher("{self.template_dir}")
img = patcher.create_footer_image("{title}", "{subtitle}")
img.save("{preview_path}")
'''
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and preview_path.exists():
                    GLib.idle_add(self._load_footer_preview, str(preview_path))
                    return

            # Fallback: simple text preview
            cmd = [
                'python3', '-c', f'''
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGBA", (256, 64), (40, 40, 40, 255))
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
except:
    font = ImageFont.load_default()
    font_small = font
title = """{title}"""
subtitle = """{subtitle}"""
# Draw title
bbox = draw.textbbox((0, 0), title, font=font)
tw = bbox[2] - bbox[0]
x = (256 - tw) // 2
y = 18 if subtitle else 24
draw.text((x, y), title, fill=(255, 255, 255, 255), font=font)
# Draw subtitle if present
if subtitle:
    bbox = draw.textbbox((0, 0), subtitle, font=font_small)
    sw = bbox[2] - bbox[0]
    x = (256 - sw) // 2
    draw.text((x, y + 20), subtitle, fill=(180, 180, 180, 255), font=font_small)
img.save("{preview_path}")
'''
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if preview_path.exists():
                GLib.idle_add(self._load_footer_preview, str(preview_path))
        except Exception:
            pass

    def _load_footer_preview(self, path):
        """Load footer preview image (called on main thread)."""
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self.footer_preview.set_paintable(texture)
        except Exception:
            self.footer_preview.set_paintable(None)
    
    def on_browse_output(self, button):
        def on_selected(path):
            self.output_path = Path(path)
            self.output_row.set_subtitle(str(self.output_path))
        pick_folder_zenity_async("Select Output Folder", callback=on_selected)

    def on_browse_icon(self, button):
        def on_selected(path):
            self.icon_path = Path(path)
            self.icon_row.set_subtitle(self.icon_path.name)
            self._update_preview()
        pick_file_zenity_async("Select Icon Image", [("Images", ["*.png", "*.jpg", "*.jpeg"])], callback=on_selected)

    def on_browse_cartridge(self, button):
        def on_selected(path):
            self.cartridge_path = Path(path)
            self.cartridge_row.set_subtitle(self.cartridge_path.name)
            self._update_preview()
        pick_file_zenity_async("Select Cartridge Image", [("Images", ["*.png", "*.jpg", "*.jpeg"])], callback=on_selected)

    def _draw_color_preview(self, area, cr, width, height):
        """Draw the color preview box."""
        if self.cartridge_bg_color:
            r, g, b = self.cartridge_bg_color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
            cr.rectangle(0, 0, width, height)
            cr.fill()
        else:
            # Draw checkerboard for transparent
            for y in range(0, height, 8):
                for x in range(0, width, 8):
                    if (x // 8 + y // 8) % 2 == 0:
                        cr.set_source_rgb(0.8, 0.8, 0.8)
                    else:
                        cr.set_source_rgb(0.6, 0.6, 0.6)
                    cr.rectangle(x, y, 8, 8)
                    cr.fill()

    def _clear_cartridge_color(self, button):
        """Clear the cartridge background color (reset to transparent)."""
        self.cartridge_bg_color = None
        self.cartridge_color_row.set_subtitle("Background color for label (transparent)")
        self.color_preview_box.queue_draw()

    def on_pick_cartridge_color(self, button):
        """Open color picker for cartridge background."""
        def run_color_picker():
            cmd = ['zenity', '--color-selection', '--title', 'Select Cartridge Background Color']
            if self.cartridge_bg_color:
                r, g, b = self.cartridge_bg_color
                cmd.extend(['--color', f'rgb({r},{g},{b})'])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    color_str = result.stdout.strip()
                    # Parse color from zenity output (formats: rgb(r,g,b) or #rrggbb)
                    if color_str.startswith('rgb('):
                        parts = color_str[4:-1].split(',')
                        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    elif color_str.startswith('#'):
                        r = int(color_str[1:3], 16)
                        g = int(color_str[3:5], 16)
                        b = int(color_str[5:7], 16)
                    else:
                        return
                    GLib.idle_add(self._set_cartridge_color, r, g, b)
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass
        Thread(target=run_color_picker, daemon=True).start()

    def _set_cartridge_color(self, r, g, b):
        """Set the cartridge background color."""
        self.cartridge_bg_color = (r, g, b)
        self.cartridge_color_row.set_subtitle(f"RGB({r}, {g}, {b})")
        self.color_preview_box.queue_draw()

    def set_status(self, message, error=False):
        def update():
            self.status_label.set_text(message)
            if error:
                self.status_label.remove_css_class("dim-label")
                self.status_label.add_css_class("error")
            else:
                self.status_label.remove_css_class("error")
                self.status_label.add_css_class("dim-label")
        GLib.idle_add(update)
    
    def set_progress(self, fraction):
        GLib.idle_add(lambda: self.progress_bar.set_fraction(fraction))
    
    def on_build_docker(self, button):
        self.docker_build_btn.set_sensitive(False)
        self.docker_rebuild_btn.set_sensitive(False)
        self.set_status("Building Docker image (this may take 5-10 minutes)...")
        self.progress_bar.set_visible(True)
        self.progress_bar.pulse()
        Thread(target=self._do_build_docker, daemon=True).start()

    def on_rebuild_docker(self, button):
        self.docker_build_btn.set_sensitive(False)
        self.docker_rebuild_btn.set_sensitive(False)
        self.set_status("Rebuilding Docker image (this may take 5-10 minutes)...")
        self.progress_bar.set_visible(True)
        self.progress_bar.pulse()
        Thread(target=self._do_build_docker, args=(True,), daemon=True).start()
    
    def _do_build_docker(self, no_cache=False):
        try:
            dockerfile_dir = self.script_dir
            if not (dockerfile_dir / "Dockerfile").exists():
                self.set_status("Dockerfile not found", error=True)
                GLib.idle_add(self._update_docker_status)
                return

            cmd = ['docker', 'build', '--network=host', '-t', 'mgba-forwarder']
            if no_cache:
                cmd.append('--no-cache')
            cmd.append('.')

            result = subprocess.run(
                cmd,
                cwd=str(dockerfile_dir),
                capture_output=True,
                text=True,
                timeout=600
            )
            
            if result.returncode == 0:
                self.docker_status = 'ready'
                self.set_status("Docker image built successfully")
            else:
                self.set_status(f"Docker build failed: {result.stderr[-200:]}", error=True)
        except subprocess.TimeoutExpired:
            self.set_status("Docker build timed out", error=True)
        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)
        finally:
            GLib.idle_add(self._update_docker_status)
            GLib.idle_add(lambda: self.progress_bar.set_visible(False))
    
    def on_preview_banner(self, button):
        """Generate a temporary banner and open it for preview."""
        game_name = self.game_name_row.get_text().strip() or "Preview"

        if not self._check_template():
            self.set_status("Template validation failed", error=True)
            return

        self.preview_btn.set_sensitive(False)
        self.set_status("Generating preview...")
        Thread(target=self._do_preview_banner, args=(game_name,), daemon=True).start()

    def _do_preview_banner(self, game_name):
        """Generate preview in background thread."""
        try:
            import struct

            footer_title = self.footer_title_row.get_text().strip() or game_name
            footer_subtitle = self.footer_subtitle_row.get_text().strip()
            if footer_subtitle:
                footer_subtitle = f"Released: {footer_subtitle}"

            # Create temp banner
            preview_dir = Path(tempfile.gettempdir()) / "banner_preview"
            preview_dir.mkdir(exist_ok=True)
            temp_banner = preview_dir / "preview.bnr"

            patcher_script = self._get_patcher_script(self.current_template_key)

            cmd = [
                'python3', str(patcher_script),
                '-t', str(self.template_dir),
                '-o', str(temp_banner),
                '--title', footer_title
            ]

            if footer_subtitle:
                cmd.extend(['--subtitle', footer_subtitle])

            if self.cartridge_path and self.cartridge_path.exists():
                cmd.extend(['--cartridge', str(self.cartridge_path)])

            if self.cartridge_bg_color:
                r, g, b = self.cartridge_bg_color
                cmd.extend(['--bg-color', f'{r},{g},{b}'])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0 or not temp_banner.exists():
                self.set_status(f"Preview failed: {result.stderr[:100]}", error=True)
                GLib.idle_add(lambda: self.preview_btn.set_sensitive(True))
                return

            # Extract cartridge texture from banner
            with open(temp_banner, 'rb') as f:
                banner = f.read()

            if banner[:4] != b'CBMD':
                self.set_status("Invalid banner format", error=True)
                GLib.idle_add(lambda: self.preview_btn.set_sensitive(True))
                return

            cgfx_offset = struct.unpack('<I', banner[0x08:0x0C])[0]

            # Decompress LZ11
            cgfx = self._decompress_lz11(banner, cgfx_offset)

            # Detect template and extract texture
            if len(cgfx) == 172416:  # Universal VC
                # Universal VC COMMON1 (cartridge label) base texture lives at 0x5880.
                cartridge_offset = 0x5880
            else:  # GBA VC
                cartridge_offset = 0x38F80

            # Decode RGBA8 Morton texture
            cartridge_img = self._decode_rgba8_texture(cgfx, cartridge_offset, 128, 128)
            cartridge_path = preview_dir / "cartridge_preview.png"
            cartridge_img.save(str(cartridge_path))

            # Open image viewer
            subprocess.Popen(['xdg-open', str(cartridge_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            self.set_status("Preview opened in image viewer")

        except Exception as e:
            self.set_status(f"Preview error: {str(e)}", error=True)
        finally:
            GLib.idle_add(lambda: self.preview_btn.set_sensitive(True))

    def _decompress_lz11(self, data, offset=0):
        """Decompress LZ11 data."""
        if data[offset] != 0x11:
            raise ValueError('Not LZ11 compressed')

        size = data[offset+1] | (data[offset+2] << 8) | (data[offset+3] << 16)
        result = bytearray(size)
        src = offset + 4
        dst = 0

        while dst < size and src < len(data):
            flags = data[src]
            src += 1

            for bit in range(8):
                if dst >= size or src >= len(data):
                    break

                if flags & (0x80 >> bit):
                    byte1 = data[src]
                    src += 1

                    if (byte1 >> 4) == 0:
                        byte2 = data[src]; src += 1
                        length = ((byte1 << 4) | (byte2 >> 4)) + 0x11
                        disp = ((byte2 & 0x0F) << 8) | data[src]; src += 1
                    elif (byte1 >> 4) == 1:
                        byte2, byte3, byte4 = data[src], data[src+1], data[src+2]; src += 3
                        length = (((byte1 & 0x0F) << 12) | (byte2 << 4) | (byte3 >> 4)) + 0x111
                        disp = ((byte3 & 0x0F) << 8) | byte4
                    else:
                        length = (byte1 >> 4) + 1
                        byte2 = data[src]; src += 1
                        disp = ((byte1 & 0x0F) << 8) | byte2

                    disp += 1
                    for i in range(length):
                        if dst >= size: break
                        result[dst] = result[dst - disp]
                        dst += 1
                else:
                    result[dst] = data[src]
                    src += 1
                    dst += 1

        return bytes(result)

    def _decode_rgba8_texture(self, data, offset, width, height):
        """Decode RGBA8 Morton-tiled texture to PIL Image."""
        from PIL import Image

        img = Image.new('RGBA', (width, height))
        pixels = img.load()

        pos = offset
        for ty in range(height // 8):
            for tx in range(width // 8):
                for t in range(64):
                    px = (t & 1) | ((t >> 1) & 2) | ((t >> 2) & 4)
                    py = ((t >> 1) & 1) | ((t >> 2) & 2) | ((t >> 3) & 4)

                    x = tx * 8 + px
                    y = ty * 8 + py

                    if pos + 4 <= len(data):
                        a, b, g, r = data[pos:pos+4]
                        if x < width and y < height:
                            pixels[x, y] = (r, g, b, a)
                    pos += 4

        return img

    def on_create_banner_only(self, button):
        game_name = self.game_name_row.get_text().strip()
        if not game_name:
            self.set_status("Please enter a game name", error=True)
            return

        if not self._check_template():
            self.set_status("Template validation failed", error=True)
            return

        self.create_btn.set_sensitive(False)
        self.banner_btn.set_sensitive(False)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        Thread(target=self._do_create_banner_only, daemon=True).start()
    
    def _do_create_banner_only(self):
        try:
            game_name = self.game_name_row.get_text().strip()
            footer_title = self.footer_title_row.get_text().strip() or game_name
            footer_subtitle = self.footer_subtitle_row.get_text().strip()
            if footer_subtitle:
                footer_subtitle = f"Released: {footer_subtitle}"
            
            self.output_path.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in game_name)
            
            self.set_status("Creating 3D banner...")
            self.set_progress(0.3)
            
            patcher_script = self._get_patcher_script(self.current_template_key)
            output_banner = self.output_path / f"{safe_name}.bnr"
            
            cmd = [
                'python3', str(patcher_script),
                '-t', str(self.template_dir),
                '-o', str(output_banner),
                '--title', footer_title
            ]
            
            if footer_subtitle:
                cmd.extend(['--subtitle', footer_subtitle])
            
            if self.cartridge_path and self.cartridge_path.exists():
                cmd.extend(['--cartridge', str(self.cartridge_path)])

            if self.cartridge_bg_color:
                r, g, b = self.cartridge_bg_color
                cmd.extend(['--bg-color', f'{r},{g},{b}'])

            self.set_progress(0.5)
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.set_progress(1.0)

            if result.returncode == 0 and output_banner.exists():
                self.set_status(f"Created: {output_banner.name}")
            else:
                error_msg = result.stderr or result.stdout or "Unknown error"
                self.set_status(f"Banner failed: {error_msg[:200]}", error=True)
                
        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)
        finally:
            GLib.idle_add(self._finish_create)
    
    def on_create(self, button):
        game_name = self.game_name_row.get_text().strip()
        rom_path = self.rom_path_row.get_text().strip()
        
        if not game_name:
            self.set_status("Please enter a game name", error=True)
            return
        
        if not rom_path:
            self.set_status("Please enter ROM path", error=True)
            return
        
        if self.docker_status != 'ready':
            self.set_status("Please build Docker image first", error=True)
            return
        
        if not self._check_template():
            self.set_status("Template validation failed", error=True)
            return
        
        self.create_btn.set_sensitive(False)
        self.banner_btn.set_sensitive(False)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        Thread(target=self._do_create, daemon=True).start()
    
    def _do_create(self):
        try:
            game_name = self.game_name_row.get_text().strip()
            rom_path = self.rom_path_row.get_text().strip()
            footer_title = self.footer_title_row.get_text().strip() or game_name
            footer_subtitle = self.footer_subtitle_row.get_text().strip()
            if footer_subtitle:
                footer_subtitle = f"Released: {footer_subtitle}"
            
            work_dir = Path(tempfile.mkdtemp())
            self.output_path.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in game_name)
            
            # Step 1: Create banner
            self.set_status("Creating 3D banner...")
            self.set_progress(0.2)
            
            patcher_script = self._get_patcher_script(self.current_template_key)
            
            cmd = [
                'python3', str(patcher_script),
                '-t', str(self.template_dir),
                '-o', str(work_dir / "banner.bnr"),
                '--title', footer_title
            ]
            
            if footer_subtitle:
                cmd.extend(['--subtitle', footer_subtitle])
            
            if self.cartridge_path and self.cartridge_path.exists():
                cmd.extend(['--cartridge', str(self.cartridge_path)])

            if self.cartridge_bg_color:
                r, g, b = self.cartridge_bg_color
                cmd.extend(['--bg-color', f'{r},{g},{b}'])

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                self.set_status(f"Banner failed: {result.stderr[:200]}", error=True)
                return

            self.set_progress(0.4)

            # Step 2: Prepare icon
            if self.icon_path and self.icon_path.exists():
                shutil.copy(self.icon_path, work_dir / "icon.png")
            
            # Step 3: Run Docker build
            self.set_status("Building CIA with Docker...")
            self.set_progress(0.5)
            
            docker_cmd = [
                'docker', 'run', '--rm',
                '-v', f'{work_dir}:/work',
                '-v', f'{self.template_dir}:/opt/forwarder/templates/gba_vc/nsui_template',
                '-v', f'{self.banner_tools_dir}:/opt/forwarder/banner_tools',
                'mgba-forwarder',
                game_name,
                rom_path
            ]
            
            result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300)
            
            self.set_progress(0.9)
            
            if result.returncode == 0 and (work_dir / 'output.cia').exists():
                output_file = self.output_path / f"{safe_name}.cia"
                shutil.copy2(work_dir / 'output.cia', output_file)
                self.set_status(f"Created: {output_file.name}")
                self.set_progress(1.0)
            else:
                error_msg = result.stderr or result.stdout or "Unknown error"
                self.set_status(f"Error: {error_msg[-200:]}", error=True)
            
            shutil.rmtree(work_dir, ignore_errors=True)
            
        except subprocess.TimeoutExpired:
            self.set_status("Build timed out", error=True)
        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)
        finally:
            GLib.idle_add(self._finish_create)
    
    def _finish_create(self):
        self.create_btn.set_sensitive(True)
        self.banner_btn.set_sensitive(True)
        self.progress_bar.set_visible(False)

    def on_compare_banner(self, button):
        """Compare generated banner with template and reference."""
        game_name = self.game_name_row.get_text().strip()
        if not game_name:
            self.set_status("Please enter a game name", error=True)
            return

        self.set_status("Generating comparison...")
        Thread(target=self._do_compare_banner, daemon=True).start()

    def _do_compare_banner(self):
        """Generate banner and compare with template."""
        try:
            game_name = self.game_name_row.get_text().strip()
            footer_title = self.footer_title_row.get_text().strip() or game_name

            # Create temp banner
            preview_dir = Path(tempfile.gettempdir()) / "banner_compare"
            preview_dir.mkdir(exist_ok=True)
            temp_banner = preview_dir / "test.bnr"

            patcher_script = self._get_patcher_script(self.current_template_key)

            cmd = [
                'python3', str(patcher_script),
                '-t', str(self.template_dir),
                '-o', str(temp_banner),
                '--title', footer_title
            ]

            if self.cartridge_path and self.cartridge_path.exists():
                cmd.extend(['--cartridge', str(self.cartridge_path)])

            if self.cartridge_bg_color:
                r, g, b = self.cartridge_bg_color
                cmd.extend(['--bg-color', f'{r},{g},{b}'])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.returncode != 0 or not temp_banner.exists():
                self.set_status(f"Banner generation failed", error=True)
                return

            # Run comparison tool
            compare_script = self.banner_tools_dir / "banner_compare.py"
            template_cgfx = self.template_dir / "banner.cgfx"
            comparison_output = preview_dir / "comparison.png"

            compare_cmd = [
                'python3', str(compare_script),
                '-t', str(template_cgfx),
                str(temp_banner),
                '-o', str(comparison_output),
                '--open'
            ]

            # Check if there's a reference CIA
            reference_cia = self.output_path / "Metal Slug Advance.cia"
            if reference_cia.exists():
                compare_cmd.insert(-2, str(reference_cia))

            result = subprocess.run(compare_cmd, capture_output=True, text=True, timeout=30)

            if result.returncode == 0:
                self.set_status(f"Comparison saved: {comparison_output}")
            else:
                self.set_status(f"Comparison failed: {result.stderr[:100]}", error=True)

        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)

    def on_test_emulator(self, button):
        """Open last created CIA in emulator."""
        game_name = self.game_name_row.get_text().strip()
        if not game_name:
            self.set_status("Please enter a game name first", error=True)
            return

        safe_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in game_name)
        cia_path = self.output_path / f"{safe_name}.cia"

        if not cia_path.exists():
            # Try to find any CIA in output directory
            cias = list(self.output_path.glob("*.cia"))
            if cias:
                cia_path = cias[0]
            else:
                self.set_status("No CIA file found. Create one first.", error=True)
                return

        # Try to find emulator
        emulators = [
            'lime3ds',      # Lime3DS (Citra fork)
            'citra',        # Original Citra
            'citra-qt',     # Citra Qt version
            'lime3ds-gui',  # Lime3DS GUI
        ]

        emulator_found = None
        for emu in emulators:
            try:
                result = subprocess.run(['which', emu], capture_output=True, text=True)
                if result.returncode == 0:
                    emulator_found = emu
                    break
            except:
                pass

        if emulator_found:
            self.set_status(f"Opening {cia_path.name} in {emulator_found}...")
            try:
                subprocess.Popen([emulator_found, str(cia_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.set_status(f"Launched {emulator_found}")
            except Exception as e:
                self.set_status(f"Failed to launch emulator: {e}", error=True)
        else:
            # No emulator found, try xdg-open as fallback
            self.set_status("No emulator found, opening file location...")
            try:
                subprocess.Popen(['xdg-open', str(self.output_path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except:
                self.set_status("No emulator found. Install Citra or Lime3DS.", error=True)


class ForwarderApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.github.mgba-forwarder")
    
    def do_activate(self):
        win = ForwarderWindow(self)
        win.present()


def main():
    app = ForwarderApp()
    app.run([])


if __name__ == '__main__':
    main()
