#!/usr/bin/env python3
"""
mGBA 3DS Forwarder Creator - GTK4/Adwaita Edition with Docker Support
Supports multiple banner templates: NSUI GBA VC and Universal VC
Uses zenity for file dialogs to avoid GTK4 FileChooser schema issues
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf, Pango

import subprocess
import tempfile
import shutil
import os
import json
import urllib.parse
from pathlib import Path
from threading import Thread, Event

CLAMP_MAX_WIDTH = 820

from batch_tools import BatchItem, title_from_rom_filename


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

        # Cartridge shell/background tint (Universal VC only)
        self.shell_color = None  # None means use template default

        # Batch mode
        self.batch_rom_dir: Path | None = None
        self.batch_items: list[BatchItem] = []
        self.batch_show_only_problems: bool = False

        # Batch banner settings (separate from Single)
        self.batch_template_key = DEFAULT_TEMPLATE
        self.batch_template_dir = self._get_template_path(self.batch_template_key)
        self.batch_icon_path: Path | None = None
        self.batch_cartridge_path: Path | None = None
        self.batch_cartridge_bg_color: tuple[int, int, int] | None = None
        self.batch_shell_color: tuple[int, int, int] | None = None
        self._batch_expanded_keys: set[str] = set()

        # SteamGridDB
        self.sgdb_api_key: str = ""

        # Single mode (ROM-driven, same logic as batch)
        self.single_rom_path: Path | None = None
        self.single_sd_path: str | None = None
        self.single_item: BatchItem | None = None
        self._single_title_manually_edited: bool = False

        # Track if user manually edited footer title
        self._load_user_config()
        self._install_css()
        self._setup_ui()

    def _install_css(self) -> None:
        css = """
        .batch-needs-title {
            border-left: 4px solid rgba(192, 28, 40, 0.95);
            background-color: rgba(192, 28, 40, 0.08);
        }
        .batch-needs-art {
            border-left: 4px solid rgba(192, 28, 40, 0.65);
            background-color: rgba(192, 28, 40, 0.05);
        }
        """
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception:
            pass
    
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

    @staticmethod
    def _safe_title(title: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in title).strip()
        return safe or "Forwarder"

    @staticmethod
    def _apply_uniform_row_margins(rows: list[Gtk.Widget], margin: int = 6) -> None:
        """Normalize vertical spacing for ad-hoc rows (e.g., button rows)."""
        for row in rows:
            try:
                row.set_margin_top(margin)
                row.set_margin_bottom(margin)
            except Exception:
                continue

    def _refresh_single_shell_row(self) -> None:
        """Show or remove the single-tab shell row to avoid blank dividers."""
        row = getattr(self, "shell_color_row", None)
        group = getattr(self, "single_banner_group", None)
        if row is None or group is None:
            return

        try:
            parent = row.get_parent()
        except Exception:
            parent = None

        if self.current_template_key == "universal_vc":
            if parent is None:
                group.add(row)
            row.set_visible(True)
        else:
            if parent is not None:
                try:
                    parent.remove(row)
                except Exception:
                    row.set_visible(False)
            else:
                row.set_visible(False)

    def _refresh_batch_shell_row(self) -> None:
        """Show or remove the batch shell row to avoid blank dividers."""
        row = getattr(self, "batch_shell_color_row", None)
        group = getattr(self, "batch_settings_group", None)
        if row is None or group is None:
            return

        try:
            parent = row.get_parent()
        except Exception:
            parent = None

        if self.batch_template_key == "universal_vc":
            if parent is None:
                group.add(row)
            row.set_visible(True)
        else:
            if parent is not None:
                try:
                    parent.remove(row)
                except Exception:
                    row.set_visible(False)
            else:
                row.set_visible(False)
    
    def _setup_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        header = Adw.HeaderBar()
        main_box.append(header)

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        main_box.append(self.view_stack)

        switcher = Adw.ViewSwitcherTitle()
        # Prefer property assignment to avoid deprecated setter warnings.
        switcher.set_property("stack", self.view_stack)
        header.set_title_widget(switcher)

        # --- Single tab ---
        single_scrolled = Gtk.ScrolledWindow()
        single_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        single_scrolled.set_vexpand(True)

        single_clamp = Adw.Clamp()
        single_clamp.set_maximum_size(CLAMP_MAX_WIDTH)
        single_clamp.set_tightening_threshold(CLAMP_MAX_WIDTH)
        single_clamp.set_hexpand(True)
        single_scrolled.set_child(single_clamp)

        single_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        single_content.set_margin_top(12)
        single_content.set_margin_bottom(12)
        single_content.set_margin_start(12)
        single_content.set_margin_end(12)
        single_clamp.set_child(single_content)

        self._build_single_tab(single_content)
        self.view_stack.add_titled(single_scrolled, "single", "Single")

        # --- Batch tab ---
        batch_scrolled = Gtk.ScrolledWindow()
        batch_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        batch_scrolled.set_vexpand(True)

        batch_clamp = Adw.Clamp()
        batch_clamp.set_maximum_size(CLAMP_MAX_WIDTH)
        batch_clamp.set_tightening_threshold(CLAMP_MAX_WIDTH)
        batch_clamp.set_hexpand(True)
        batch_scrolled.set_child(batch_clamp)

        batch_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        batch_content.set_margin_top(12)
        batch_content.set_margin_bottom(12)
        batch_content.set_margin_start(12)
        batch_content.set_margin_end(12)
        batch_clamp.set_child(batch_content)

        self._build_batch_tab(batch_content)
        self.view_stack.add_titled(batch_scrolled, "batch", "Batch")

        # --- Settings tab ---
        settings_scrolled = Gtk.ScrolledWindow()
        settings_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        settings_scrolled.set_vexpand(True)

        settings_clamp = Adw.Clamp()
        settings_clamp.set_maximum_size(CLAMP_MAX_WIDTH)
        settings_clamp.set_tightening_threshold(CLAMP_MAX_WIDTH)
        settings_clamp.set_hexpand(True)
        settings_scrolled.set_child(settings_clamp)

        settings_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        settings_content.set_margin_top(12)
        settings_content.set_margin_bottom(12)
        settings_content.set_margin_start(12)
        settings_content.set_margin_end(12)
        settings_clamp.set_child(settings_content)

        self._build_settings_tab(settings_content)
        self.view_stack.add_titled(settings_scrolled, "settings", "Settings")

        # --- Global status/progress footer (visible on both tabs) ---
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        footer.set_margin_top(12)
        footer.set_margin_bottom(12)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        main_box.append(footer)

        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_visible(False)
        footer.append(self.progress_bar)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("dim-label")
        footer.append(self.status_label)

    def _build_single_tab(self, content: Gtk.Box) -> None:
        basic_group = Adw.PreferencesGroup()
        content.append(basic_group)

        self.single_rom_row = Adw.ActionRow(title="ROM File", subtitle="Pick a .gba file")
        rom_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        rom_clear_btn.add_css_class("flat")
        rom_clear_btn.connect("clicked", self.on_clear_single_rom)
        rom_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        rom_btn.connect("clicked", self.on_browse_single_rom)
        self.single_rom_row.add_suffix(rom_clear_btn)
        self.single_rom_row.add_suffix(rom_btn)
        basic_group.add(self.single_rom_row)

        self.output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=str(self.output_path)
        )
        output_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        output_btn.connect("clicked", self.on_browse_output)
        self.output_row.add_suffix(output_btn)
        basic_group.add(self.output_row)

        self.game_name_row = Adw.EntryRow(title="Title")
        self.game_name_row.set_text("")
        self.game_name_row.connect("changed", self._on_single_title_changed)
        basic_group.add(self.game_name_row)

        self.footer_subtitle_row = Adw.EntryRow(title="Release Year (optional)")
        self.footer_subtitle_row.set_text("")
        self.footer_subtitle_row.connect("changed", self._update_preview)
        basic_group.add(self.footer_subtitle_row)

        self.sd_path_row = Adw.ActionRow(title="ROM Path on SD", subtitle="Select a ROM to set this automatically")
        basic_group.add(self.sd_path_row)

        icon_group = Adw.PreferencesGroup()
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
        icon_fetch_btn = Gtk.Button(label="SGDB", valign=Gtk.Align.CENTER)
        icon_fetch_btn.set_tooltip_text("Pick and download an icon from SteamGridDB for this title")
        icon_fetch_btn.connect("clicked", self.on_fetch_single_icon)
        self.icon_row.add_suffix(icon_clear_btn)
        self.icon_row.add_suffix(icon_btn)
        self.icon_row.add_suffix(icon_fetch_btn)
        icon_group.add(self.icon_row)

        banner_group = Adw.PreferencesGroup()
        self.single_banner_group = banner_group
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
            subtitle="Logo/label image (fit mode, no cropping)"
        )
        cart_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        cart_clear_btn.add_css_class("flat")
        cart_clear_btn.connect("clicked", lambda b: self._clear_path("cartridge"))
        cart_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        cart_btn.connect("clicked", self.on_browse_cartridge)
        self.cartridge_row.add_suffix(cart_clear_btn)
        self.cartridge_row.add_suffix(cart_btn)
        cart_fetch_btn = Gtk.Button(label="SGDB", valign=Gtk.Align.CENTER)
        cart_fetch_btn.set_tooltip_text("Pick and download a logo/label from SteamGridDB for this title")
        cart_fetch_btn.connect("clicked", self.on_fetch_single_label)
        self.cartridge_row.add_suffix(cart_fetch_btn)
        banner_group.add(self.cartridge_row)

        # Label Background Color
        self.cartridge_color_row = Adw.ActionRow(
            title="Label Background",
            subtitle="Background behind logo (transparent by default)"
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

        # Cartridge Shell Color (Universal VC)
        self.shell_color_row = Adw.ActionRow(
            title="Cartridge Shell Color",
            subtitle="Tint cartridge/body (template default if unset)"
        )
        self.shell_color_preview_box = Gtk.DrawingArea()
        self.shell_color_preview_box.set_size_request(32, 32)
        self.shell_color_preview_box.set_valign(Gtk.Align.CENTER)
        self.shell_color_preview_box.set_draw_func(self._draw_shell_color_preview)

        shell_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        shell_clear_btn.add_css_class("flat")
        shell_clear_btn.set_tooltip_text("Reset to template default")
        shell_clear_btn.connect("clicked", self._clear_shell_color)

        shell_btn = Gtk.Button(label="Pick Color", valign=Gtk.Align.CENTER)
        shell_btn.connect("clicked", self.on_pick_shell_color)

        self.shell_color_row.add_suffix(self.shell_color_preview_box)
        self.shell_color_row.add_suffix(shell_clear_btn)
        self.shell_color_row.add_suffix(shell_btn)
        self._refresh_single_shell_row()

        preview_group = Adw.PreferencesGroup()
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
        preview_row = Adw.ActionRow(title="Preview")
        preview_row.set_child(preview_box)
        preview_group.add(preview_row)

        # Initialize preview with placeholder
        self._update_preview()

        # Primary action
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        actions.set_halign(Gtk.Align.CENTER)
        actions.set_margin_top(12)
        actions.set_margin_bottom(12)
        self.create_btn = Gtk.Button(label="Build CIA", valign=Gtk.Align.CENTER)
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.connect("clicked", self.on_create)
        actions.append(self.create_btn)
        actions_row = Adw.PreferencesRow()
        actions_row.set_child(actions)
        content.append(actions_row)

    def _build_batch_tab(self, content: Gtk.Box) -> None:
        folders_group = Adw.PreferencesGroup()
        content.append(folders_group)

        self.batch_folder_row = Adw.ActionRow(
            title="ROM Folder",
            subtitle="Pick a folder to scan for .gba files"
        )
        batch_pick_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        batch_pick_btn.connect("clicked", self.on_browse_batch_rom_dir)
        batch_scan_btn = Gtk.Button(label="Rescan", valign=Gtk.Align.CENTER)
        batch_scan_btn.connect("clicked", self.on_scan_batch_roms)
        self.batch_folder_row.add_suffix(batch_pick_btn)
        self.batch_folder_row.add_suffix(batch_scan_btn)
        folders_group.add(self.batch_folder_row)

        self.batch_output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=str(self.output_path)
        )
        out_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        out_btn.connect("clicked", self.on_browse_output)
        self.batch_output_row.add_suffix(out_btn)
        folders_group.add(self.batch_output_row)

        settings_group = Adw.PreferencesGroup()
        self.batch_settings_group = settings_group
        content.append(settings_group)

        self.batch_template_combo_row = Adw.ComboRow(title="Banner Template")
        batch_model = Gtk.StringList()
        self.batch_template_keys = []
        for key, info in TEMPLATES.items():
            batch_model.append(info["name"])
            self.batch_template_keys.append(key)
        self.batch_template_combo_row.set_model(batch_model)
        if self.batch_template_key in self.batch_template_keys:
            self.batch_template_combo_row.set_selected(self.batch_template_keys.index(self.batch_template_key))
            self.batch_template_combo_row.set_subtitle(TEMPLATES[self.batch_template_key]["description"])
        self.batch_template_combo_row.connect("notify::selected", self._on_batch_template_changed)
        settings_group.add(self.batch_template_combo_row)

        self.batch_template_status_row = Adw.ActionRow(title="Template Status")
        self._check_batch_template()
        settings_group.add(self.batch_template_status_row)

        self.batch_default_icon_row = Adw.ActionRow(
            title="Default Icon",
            subtitle="Optional: used when a ROM has no per-game icon"
        )
        batch_icon_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        batch_icon_clear_btn.add_css_class("flat")
        batch_icon_clear_btn.connect("clicked", lambda *_: self._clear_batch_path("icon"))
        batch_icon_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        batch_icon_btn.connect("clicked", self.on_browse_batch_icon)
        self.batch_default_icon_row.add_suffix(batch_icon_clear_btn)
        self.batch_default_icon_row.add_suffix(batch_icon_btn)
        settings_group.add(self.batch_default_icon_row)

        self.batch_default_label_row = Adw.ActionRow(
            title="Default Cartridge Label",
            subtitle="Optional: used when a ROM has no per-game label/logo"
        )
        batch_label_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        batch_label_clear_btn.add_css_class("flat")
        batch_label_clear_btn.connect("clicked", lambda *_: self._clear_batch_path("cartridge"))
        batch_label_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        batch_label_btn.connect("clicked", self.on_browse_batch_cartridge)
        self.batch_default_label_row.add_suffix(batch_label_clear_btn)
        self.batch_default_label_row.add_suffix(batch_label_btn)
        settings_group.add(self.batch_default_label_row)

        self.batch_label_bg_row = Adw.ActionRow(
            title="Label Background",
            subtitle="Background behind logo (transparent by default)"
        )
        self.batch_label_bg_preview = Gtk.DrawingArea()
        self.batch_label_bg_preview.set_size_request(32, 32)
        self.batch_label_bg_preview.set_valign(Gtk.Align.CENTER)
        self.batch_label_bg_preview.set_draw_func(self._draw_batch_label_bg_preview)
        batch_bg_clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        batch_bg_clear.add_css_class("flat")
        batch_bg_clear.set_tooltip_text("Reset to transparent")
        batch_bg_clear.connect("clicked", lambda *_: self._set_batch_label_bg(None))
        batch_bg_pick = Gtk.Button(label="Pick Color", valign=Gtk.Align.CENTER)
        batch_bg_pick.connect("clicked", self.on_pick_batch_label_bg)
        self.batch_label_bg_row.add_suffix(self.batch_label_bg_preview)
        self.batch_label_bg_row.add_suffix(batch_bg_clear)
        self.batch_label_bg_row.add_suffix(batch_bg_pick)
        settings_group.add(self.batch_label_bg_row)

        self.batch_shell_color_row = Adw.ActionRow(
            title="Cartridge Shell Color",
            subtitle="Tint cartridge/body (template default if unset)"
        )
        self.batch_shell_color_preview = Gtk.DrawingArea()
        self.batch_shell_color_preview.set_size_request(32, 32)
        self.batch_shell_color_preview.set_valign(Gtk.Align.CENTER)
        self.batch_shell_color_preview.set_draw_func(self._draw_batch_shell_preview)
        batch_shell_clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        batch_shell_clear.add_css_class("flat")
        batch_shell_clear.set_tooltip_text("Reset to template default")
        batch_shell_clear.connect("clicked", lambda *_: self._set_batch_shell_color(None))
        batch_shell_pick = Gtk.Button(label="Pick Color", valign=Gtk.Align.CENTER)
        batch_shell_pick.connect("clicked", self.on_pick_batch_shell_color)
        self.batch_shell_color_row.add_suffix(self.batch_shell_color_preview)
        self.batch_shell_color_row.add_suffix(batch_shell_clear)
        self.batch_shell_color_row.add_suffix(batch_shell_pick)
        self._refresh_batch_shell_row()

        roms_group = Adw.PreferencesGroup()
        content.append(roms_group)

        self.batch_fetch_progress = Gtk.ProgressBar()
        self.batch_fetch_progress.set_visible(False)
        self.batch_fetch_progress_row = Adw.PreferencesRow()
        self.batch_fetch_progress_row.set_child(self.batch_fetch_progress)
        self.batch_fetch_progress_row.set_visible(False)
        roms_group.add(self.batch_fetch_progress_row)

        self.batch_status_row = Adw.ActionRow(title="Status", subtitle="No ROMs scanned yet")
        roms_group.add(self.batch_status_row)

        batch_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        batch_actions.set_halign(Gtk.Align.FILL)

        self.batch_fetch_btn = Gtk.Button(label="Fetch Art (All)", valign=Gtk.Align.CENTER)
        self.batch_fetch_btn.connect("clicked", self.on_fetch_batch_art)
        self.batch_fetch_btn.set_sensitive(False)

        batch_actions.append(self.batch_fetch_btn)
        batch_actions_row = Adw.PreferencesRow()
        batch_actions_row.set_child(batch_actions)
        roms_group.add(batch_actions_row)

        filter_row = Adw.ActionRow(
            title="Show Only Problems",
            subtitle="Hide rows that already have title and art"
        )
        self.batch_filter_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.batch_filter_switch.set_active(self.batch_show_only_problems)
        self.batch_filter_switch.connect("notify::active", self._on_batch_filter_toggle)
        filter_row.add_suffix(self.batch_filter_switch)
        roms_group.add(filter_row)

        self.batch_listbox = Gtk.ListBox()
        self.batch_listbox.add_css_class("boxed-list")
        self.batch_listbox.set_hexpand(True)
        batch_list_row = Adw.PreferencesRow()
        batch_list_row.set_child(self.batch_listbox)
        roms_group.add(batch_list_row)

        build_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        build_box.set_halign(Gtk.Align.CENTER)
        build_box.set_margin_top(12)
        build_box.set_margin_bottom(12)
        self.batch_build_btn = Gtk.Button(label="Build All CIAs", valign=Gtk.Align.CENTER)
        self.batch_build_btn.add_css_class("suggested-action")
        self.batch_build_btn.connect("clicked", self.on_build_batch_all)
        self.batch_build_btn.set_sensitive(False)
        build_box.append(self.batch_build_btn)
        build_row = Adw.PreferencesRow()
        build_row.set_child(build_box)
        content.append(build_row)

    def _build_settings_tab(self, content: Gtk.Box) -> None:
        # SteamGridDB
        sgdb_group = Adw.PreferencesGroup()
        content.append(sgdb_group)

        if hasattr(Adw, "PasswordEntryRow"):
            self.sgdb_key_row = Adw.PasswordEntryRow(title="SteamGridDB API Key")
        else:
            self.sgdb_key_row = Adw.EntryRow(title="SteamGridDB API Key")
            try:
                self.sgdb_key_row.set_visibility(False)
            except Exception:
                pass
        self.sgdb_key_row.set_text(self.sgdb_api_key or "")
        sgdb_group.add(self.sgdb_key_row)

        sgdb_key_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sgdb_key_actions.set_halign(Gtk.Align.FILL)
        sgdb_save_btn = Gtk.Button(label="Save Key", valign=Gtk.Align.CENTER)
        sgdb_save_btn.connect("clicked", self.on_save_sgdb_key)
        sgdb_clear_btn = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        sgdb_clear_btn.connect("clicked", self.on_clear_sgdb_key)
        sgdb_key_actions.append(sgdb_save_btn)
        sgdb_key_actions.append(sgdb_clear_btn)
        sgdb_key_actions_row = Adw.PreferencesRow()
        sgdb_key_actions_row.set_child(sgdb_key_actions)
        sgdb_group.add(sgdb_key_actions_row)

        # Docker
        docker_group = Adw.PreferencesGroup()
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

        self._apply_uniform_row_margins(
            [
                self.sgdb_key_row,
                sgdb_key_actions_row,
                self.docker_row,
            ]
        )
        self._update_docker_status()
    
    # =============================================================================
    # SINGLE MODE (same logic as batch)
    # =============================================================================

    def _on_single_title_changed(self, entry):
        self._single_title_manually_edited = True
        if self.single_item:
            self.single_item.title = entry.get_text().strip()
            if self.single_item.title:
                self.single_item.confidence = 1.0
        self._update_preview()

    def on_browse_single_rom(self, button):
        def on_selected(path):
            try:
                p = Path(path)
                self.single_rom_path = p
                self.single_rom_row.set_subtitle(p.name)
                self.single_sd_path = f"/roms/gba/{p.name}"
                self.sd_path_row.set_subtitle(self.single_sd_path)

                title, confidence = title_from_rom_filename(p.name)
                if not self._single_title_manually_edited:
                    try:
                        self.game_name_row.handler_block_by_func(self._on_single_title_changed)
                        self.game_name_row.set_text(title)
                    finally:
                        self.game_name_row.handler_unblock_by_func(self._on_single_title_changed)
                    self.game_name_row.remove_css_class("batch-needs-title")
                    if confidence < 0.5:
                        self.game_name_row.add_css_class("batch-needs-title")

                year = self.footer_subtitle_row.get_text().strip()
                self.single_item = BatchItem(
                    rom_path=str(p),
                    sd_path=self.single_sd_path,
                    title=self.game_name_row.get_text().strip(),
                    confidence=confidence,
                    year=year,
                )
                self._sync_single_from_item()
            except Exception:
                pass

        pick_file_zenity_async("Select GBA ROM", [("GBA ROMs", ["*.gba"])], callback=on_selected)

    def on_clear_single_rom(self, button):
        self.single_rom_path = None
        self.single_sd_path = None
        self.single_item = None
        self._single_title_manually_edited = False
        self.single_rom_row.set_subtitle("Pick a .gba file")
        self.sd_path_row.set_subtitle("Select a ROM to set this automatically")
        try:
            self.game_name_row.remove_css_class("batch-needs-title")
            self.game_name_row.set_text("")
        except Exception:
            pass
        self._update_preview()

    def _sync_single_from_item(self):
        """Apply `single_item` state to UI fields/previews."""
        it = self.single_item
        if not it:
            return
        # Keep icon/cartridge paths in sync with existing code paths.
        if it.icon_file:
            self.icon_path = Path(it.icon_file)
            self.icon_row.set_subtitle(self.icon_path.name)
        if it.label_file:
            self.cartridge_path = Path(it.label_file)
            self.cartridge_row.set_subtitle(self.cartridge_path.name)
        # Ensure SD path stays fixed.
        if self.single_sd_path:
            self.sd_path_row.set_subtitle(self.single_sd_path)
        self._update_preview()

    def _sync_single_item_from_ui(self) -> BatchItem | None:
        """Ensure single_item mirrors current UI fields before building."""
        if not (self.single_rom_path and self.single_sd_path):
            return None

        title = self.game_name_row.get_text().strip()
        year = self.footer_subtitle_row.get_text().strip()

        if self.single_item is None:
            self.single_item = BatchItem(
                rom_path=str(self.single_rom_path),
                sd_path=self.single_sd_path,
                title=title,
                confidence=1.0 if title else 0.0,
                year=year,
            )
        else:
            self.single_item.rom_path = str(self.single_rom_path)
            self.single_item.sd_path = self.single_sd_path
            self.single_item.title = title
            self.single_item.year = year

        self.single_item.icon_file = str(self.icon_path) if self.icon_path else None
        self.single_item.label_file = str(self.cartridge_path) if self.cartridge_path else None
        return self.single_item

    def on_fetch_single_icon(self, button):
        if not (self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")):
            self.set_status("Set SteamGridDB API key in Settings", error=True)
            return
        if not self.single_item or not self.game_name_row.get_text().strip():
            self.set_status("Select a ROM and set a title first", error=True)
            return
        self.single_item.title = self.game_name_row.get_text().strip()
        Thread(target=self._do_fetch_item_art_with_prompt, args=(self.single_item, "icon"), daemon=True).start()

    def on_fetch_single_label(self, button):
        if not (self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")):
            self.set_status("Set SteamGridDB API key in Settings", error=True)
            return
        if not self.single_item or not self.game_name_row.get_text().strip():
            self.set_status("Select a ROM and set a title first", error=True)
            return
        self.single_item.title = self.game_name_row.get_text().strip()
        Thread(target=self._do_fetch_item_art_with_prompt, args=(self.single_item, "logo"), daemon=True).start()
    
    def _on_template_changed(self, combo, param):
        idx = combo.get_selected()
        if idx < len(self.template_keys):
            self.current_template_key = self.template_keys[idx]
            self.template_dir = self._get_template_path(self.current_template_key)
            combo.set_subtitle(TEMPLATES[self.current_template_key]['description'])
            self._refresh_single_shell_row()
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
            if self.single_item:
                self.single_item.icon_file = None
            self._update_preview()
        elif path_type == "cartridge":
            self.cartridge_path = None
            self.cartridge_row.set_subtitle("Logo/label image (fit mode, no cropping)")
            if self.single_item:
                self.single_item.label_file = None
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
        title = self.game_name_row.get_text().strip()
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

            # Use in-process patchers so preview matches the selected template.
            try:
                if self.current_template_key == "gba_vc":
                    from banner_tools.gba_vc_banner_patcher import GBAVCBannerPatcher

                    patcher = GBAVCBannerPatcher(str(self.template_dir))
                    img = patcher.create_footer_image(str(title), str(subtitle))
                    img.save(str(preview_path))
                elif self.current_template_key == "universal_vc":
                    from banner_tools.universal_vc_banner_patcher import UniversalVCBannerPatcher

                    patcher = UniversalVCBannerPatcher(str(self.template_dir))
                    img = patcher.create_footer_image(str(title), str(subtitle))
                    if img is None:
                        simple = self._simple_footer_preview(title, subtitle)
                        if simple:
                            GLib.idle_add(self._load_footer_preview, str(simple))
                        return
                    img.save(str(preview_path))
                else:
                    return

                if preview_path.exists():
                    GLib.idle_add(self._load_footer_preview, str(preview_path))
                    return
            except Exception:
                simple = self._simple_footer_preview(title, subtitle)
                if simple:
                    GLib.idle_add(self._load_footer_preview, str(simple))
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
            return
    
    def on_browse_output(self, button):
        def on_selected(path):
            self.output_path = Path(path)
            self.output_row.set_subtitle(str(self.output_path))
            if hasattr(self, "batch_output_row") and self.batch_output_row is not None:
                self.batch_output_row.set_subtitle(str(self.output_path))
        pick_folder_zenity_async("Select Output Folder", callback=on_selected)

    def on_browse_icon(self, button):
        def on_selected(path):
            self.icon_path = Path(path)
            self.icon_row.set_subtitle(self.icon_path.name)
            if self.single_item:
                self.single_item.icon_file = str(self.icon_path)
            self._update_preview()
        pick_file_zenity_async("Select Icon Image", [("Images", ["*.png", "*.jpg", "*.jpeg"])], callback=on_selected)

    def on_browse_cartridge(self, button):
        def on_selected(path):
            self.cartridge_path = Path(path)
            self.cartridge_row.set_subtitle(self.cartridge_path.name)
            if self.single_item:
                self.single_item.label_file = str(self.cartridge_path)
            self._update_preview()
        pick_file_zenity_async("Select Cartridge Image", [("Images", ["*.png", "*.jpg", "*.jpeg"])], callback=on_selected)

    # =============================================================================
    # BATCH MODE
    # =============================================================================

    def _on_batch_template_changed(self, combo, param):
        idx = combo.get_selected()
        if idx < len(self.batch_template_keys):
            self.batch_template_key = self.batch_template_keys[idx]
            self.batch_template_dir = self._get_template_path(self.batch_template_key)
            combo.set_subtitle(TEMPLATES[self.batch_template_key]["description"])
            self._refresh_batch_shell_row()
            self._check_batch_template()
            self._render_batch_items()

    def _check_batch_template(self) -> bool:
        template_info = TEMPLATES.get(self.batch_template_key)
        if not template_info:
            self.batch_template_status_row.set_subtitle("Unknown template")
            return False
        if not self.batch_template_dir.exists():
            self.batch_template_status_row.set_subtitle("Template folder not found")
            return False
        missing = []
        for f in template_info["required_files"]:
            if not (self.batch_template_dir / f).exists():
                missing.append(f)
        if missing:
            self.batch_template_status_row.set_subtitle(f"Missing: {', '.join(missing)}")
            return False
        self.batch_template_status_row.set_subtitle("Template ready")
        return True

    def _draw_batch_label_bg_preview(self, area, cr, width, height):
        if self.batch_cartridge_bg_color:
            r, g, b = self.batch_cartridge_bg_color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
            cr.rectangle(0, 0, width, height)
            cr.fill()
        else:
            for y in range(0, height, 8):
                for x in range(0, width, 8):
                    if (x // 8 + y // 8) % 2 == 0:
                        cr.set_source_rgb(0.8, 0.8, 0.8)
                    else:
                        cr.set_source_rgb(0.6, 0.6, 0.6)
                    cr.rectangle(x, y, 8, 8)
                    cr.fill()

    def _draw_batch_shell_preview(self, area, cr, width, height):
        if self.batch_shell_color:
            r, g, b = self.batch_shell_color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
        else:
            cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.rectangle(0, 0, width, height)
        cr.fill()

    def _set_batch_label_bg(self, rgb: tuple[int, int, int] | None):
        self.batch_cartridge_bg_color = rgb
        if rgb:
            r, g, b = rgb
            self.batch_label_bg_row.set_subtitle(f"RGB({r}, {g}, {b})")
        else:
            self.batch_label_bg_row.set_subtitle("Background behind logo (transparent by default)")
        self.batch_label_bg_preview.queue_draw()

    def _set_batch_shell_color(self, rgb: tuple[int, int, int] | None):
        self.batch_shell_color = rgb
        if rgb:
            r, g, b = rgb
            self.batch_shell_color_row.set_subtitle(f"Universal VC: RGB({r}, {g}, {b})")
        else:
            self.batch_shell_color_row.set_subtitle("Universal VC only: tint cartridge/body (template default if unset)")
        self.batch_shell_color_preview.queue_draw()

    def on_pick_batch_label_bg(self, button):
        def run_color_picker():
            cmd = ["zenity", "--color-selection", "--title", "Select Label Background Color"]
            if self.batch_cartridge_bg_color:
                r, g, b = self.batch_cartridge_bg_color
                cmd.extend(["--color", f"rgb({r},{g},{b})"])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    color_str = result.stdout.strip()
                    if color_str.startswith("rgb("):
                        parts = color_str[4:-1].split(",")
                        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    elif color_str.startswith("#"):
                        r = int(color_str[1:3], 16)
                        g = int(color_str[3:5], 16)
                        b = int(color_str[5:7], 16)
                    else:
                        return
                    GLib.idle_add(lambda: self._set_batch_label_bg((r, g, b)))
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass
        Thread(target=run_color_picker, daemon=True).start()

    def on_pick_batch_shell_color(self, button):
        def run_color_picker():
            cmd = ["zenity", "--color-selection", "--title", "Select Cartridge Shell Color"]
            if self.batch_shell_color:
                r, g, b = self.batch_shell_color
                cmd.extend(["--color", f"rgb({r},{g},{b})"])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    color_str = result.stdout.strip()
                    if color_str.startswith("rgb("):
                        parts = color_str[4:-1].split(",")
                        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    elif color_str.startswith("#"):
                        r = int(color_str[1:3], 16)
                        g = int(color_str[3:5], 16)
                        b = int(color_str[5:7], 16)
                    else:
                        return
                    GLib.idle_add(lambda: self._set_batch_shell_color((r, g, b)))
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass
        Thread(target=run_color_picker, daemon=True).start()

    def on_browse_batch_icon(self, button):
        def on_selected(path):
            self.batch_icon_path = Path(path)
            self.batch_default_icon_row.set_subtitle(self.batch_icon_path.name)
            self._render_batch_items()
        pick_file_zenity_async(
            "Select Default Icon Image",
            [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
            callback=on_selected,
        )

    def on_browse_batch_cartridge(self, button):
        def on_selected(path):
            self.batch_cartridge_path = Path(path)
            self.batch_default_label_row.set_subtitle(self.batch_cartridge_path.name)
            self._render_batch_items()
        pick_file_zenity_async(
            "Select Default Cartridge Label Image",
            [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
            callback=on_selected,
        )

    def _clear_batch_path(self, path_type: str) -> None:
        if path_type == "icon":
            self.batch_icon_path = None
            self.batch_default_icon_row.set_subtitle("Optional: used when a ROM has no per-game icon")
        elif path_type == "cartridge":
            self.batch_cartridge_path = None
            self.batch_default_label_row.set_subtitle("Optional: used when a ROM has no per-game label/logo")
        self._render_batch_items()

    def _on_batch_filter_toggle(self, switch, param):
        self.batch_show_only_problems = bool(switch.get_active())
        self._render_batch_items()

    def _config_path(self) -> Path:
        return Path.home() / ".config" / "mgba-forwarder-tools" / "config.json"

    def _load_user_config(self) -> None:
        try:
            p = self._config_path()
            if not p.exists():
                return
            cfg = json.loads(p.read_text(encoding="utf-8"))
            self.sgdb_api_key = str(cfg.get("steamgriddb_api_key") or "")
        except Exception:
            self.sgdb_api_key = ""

    def _save_user_config(self) -> None:
        p = self._config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        cfg = {"steamgriddb_api_key": self.sgdb_api_key}
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def on_save_sgdb_key(self, button):
        key = (self.sgdb_key_row.get_text() or "").strip()
        self.sgdb_api_key = key
        try:
            self._save_user_config()
            self.set_status("Saved SteamGridDB API key")
            self._render_batch_items()
        except Exception as e:
            self.set_status(f"Failed to save key: {e}", error=True)

    def on_clear_sgdb_key(self, button):
        self.sgdb_api_key = ""
        self.sgdb_key_row.set_text("")
        try:
            self._save_user_config()
        except Exception:
            pass
        self.set_status("Cleared SteamGridDB API key")
        self._render_batch_items()

    def on_browse_batch_rom_dir(self, button):
        def on_selected(path):
            self.batch_rom_dir = Path(path)
            self.batch_folder_row.set_subtitle(str(self.batch_rom_dir))
            # Auto-scan on folder selection (rescan button remains for refresh).
            self.on_scan_batch_roms(None)
        pick_folder_zenity_async("Select GBA ROM Folder", callback=on_selected)

    def on_scan_batch_roms(self, button):
        if not self.batch_rom_dir or not self.batch_rom_dir.exists():
            self.set_status("Pick a ROM folder first", error=True)
            return
        self.batch_status_row.set_subtitle("Scanning...")
        self.batch_fetch_btn.set_sensitive(False)
        self.batch_build_btn.set_sensitive(False)
        Thread(target=self._do_scan_batch_roms, daemon=True).start()

    def _do_scan_batch_roms(self):
        roms = sorted(self.batch_rom_dir.rglob("*.gba"))
        items: list[BatchItem] = []
        for rom in roms:
            title, confidence = title_from_rom_filename(rom.name)
            sd_path = f"/roms/gba/{rom.name}"
            items.append(BatchItem(rom_path=str(rom), sd_path=sd_path, title=title, confidence=confidence))
        self.batch_items = items
        GLib.idle_add(self._render_batch_items)

    def _set_picture_from_file(self, picture: Gtk.Picture, path: str | None, width: int, height: int) -> None:
        try:
            if not path:
                picture.set_paintable(None)
                return
            p = Path(path)
            if not p.exists():
                picture.set_paintable(None)
                return
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(p), width, height, True)
            picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception:
            picture.set_paintable(None)

    def _write_footer_preview_png(self, template_key: str, template_dir: Path, title: str, subtitle: str) -> str | None:
        """Create a footer preview PNG using a specific template; returns file path."""
        try:
            import tempfile
            import hashlib

            key = f"{template_key}\n{title}\n{subtitle}".encode("utf-8", errors="replace")
            digest = hashlib.sha1(key).hexdigest()[:12]
            out_dir = Path(tempfile.gettempdir()) / "mgba_forwarder_previews"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"footer_{digest}.png"
            if out_path.exists():
                return str(out_path)

            if template_key == "gba_vc":
                from banner_tools.gba_vc_banner_patcher import GBAVCBannerPatcher

                patcher = GBAVCBannerPatcher(str(template_dir))
                img = patcher.create_footer_image(title, subtitle)
                img.save(str(out_path))
                return str(out_path)

            if template_key == "universal_vc":
                from banner_tools.universal_vc_banner_patcher import UniversalVCBannerPatcher

                patcher = UniversalVCBannerPatcher(str(template_dir))
                img = patcher.create_footer_image(title, subtitle)
                if img is None:
                    return self._simple_footer_preview(title, subtitle)
                img.save(str(out_path))
                return str(out_path)

            return self._simple_footer_preview(title, subtitle)
        except Exception:
            return self._simple_footer_preview(title, subtitle)

    def _simple_footer_preview(self, title: str, subtitle: str) -> str | None:
        """Fallback footer preview using Pillow only."""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import tempfile

            title = title or "Preview"
            subtitle = subtitle or ""

            out_dir = Path(tempfile.gettempdir()) / "mgba_forwarder_previews"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"footer_fallback_{hash((title, subtitle)) & 0xFFFFFFFF:08x}.png"

            img = Image.new("RGBA", (256, 64), (40, 40, 40, 255))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
                font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
            except Exception:
                font = ImageFont.load_default()
                font_small = font

            bbox = draw.textbbox((0, 0), title, font=font)
            tw = bbox[2] - bbox[0]
            x = (256 - tw) // 2
            y = 18 if subtitle else 24
            draw.text((x, y), title, fill=(255, 255, 255, 255), font=font)
            if subtitle:
                bbox = draw.textbbox((0, 0), subtitle, font=font_small)
                sw = bbox[2] - bbox[0]
                x = (256 - sw) // 2
                draw.text((x, y + 20), subtitle, fill=(180, 180, 180, 255), font=font_small)

            img.save(out_path)
            return str(out_path)
        except Exception:
            return None

    def _build_forwarder_item(
        self,
        item: BatchItem,
        *,
        template_key: str,
        template_dir: Path,
        output_dir: Path,
        default_icon: Path | None = None,
        default_label: Path | None = None,
        bg_color: tuple[int, int, int] | None = None,
        shell_color: tuple[int, int, int] | None = None,
        progress_cb=None,
        status_cb=None,
    ) -> tuple[bool, Path | None, str | None]:
        """
        Build a single forwarder CIA for the given item.

        Returns (success, output_path, error_message).
        """
        work_dir = Path(tempfile.mkdtemp())
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if not item.sd_path:
                return False, None, "Missing SD path"

            footer_title = item.title or Path(item.rom_path).stem
            footer_subtitle = (item.year or "").strip()
            if footer_subtitle:
                footer_subtitle = f"Released: {footer_subtitle}"

            patcher_script = self._get_patcher_script(template_key)
            banner_out = work_dir / "banner.bnr"

            if status_cb:
                status_cb("Creating 3D banner...")
            if progress_cb:
                progress_cb(0.2)

            cmd = [
                "python3",
                str(patcher_script),
                "-t",
                str(template_dir),
                "-o",
                str(banner_out),
                "--title",
                footer_title,
            ]
            if footer_subtitle:
                cmd.extend(["--subtitle", footer_subtitle])

            label_path = item.label_file
            if not label_path and default_label and default_label.exists():
                label_path = str(default_label)
            if label_path:
                cmd.extend(["--cartridge", label_path])

            if bg_color:
                r, g, b = bg_color
                cmd.extend(["--bg-color", f"{r},{g},{b}"])

            if template_key == "universal_vc" and shell_color:
                r, g, b = shell_color
                cmd.extend(["--shell-color", f"{r},{g},{b}"])

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not banner_out.exists():
                err = (result.stderr or result.stdout or "Banner failed")[:200]
                return False, None, f"Banner failed for {footer_title}: {err}"

            if progress_cb:
                progress_cb(0.4)

            icon_src = item.icon_file
            if not icon_src and default_icon and default_icon.exists():
                icon_src = str(default_icon)
            if icon_src and Path(icon_src).exists():
                shutil.copy(icon_src, work_dir / "icon.png")

            if status_cb:
                status_cb("Building CIA with Docker...")
            if progress_cb:
                progress_cb(0.5)

            docker_cmd = [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{work_dir}:/work",
                "-v",
                f"{template_dir}:/opt/forwarder/templates/gba_vc/nsui_template",
                "-v",
                f"{self.banner_tools_dir}:/opt/forwarder/banner_tools",
                "mgba-forwarder",
                footer_title,
                item.sd_path,
            ]
            result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or not (work_dir / "output.cia").exists():
                err = (result.stderr or result.stdout or "CIA failed")[-200:]
                return False, None, f"CIA failed for {footer_title}: {err}"

            safe_name = self._safe_title(footer_title)
            out_path = output_dir / f"{safe_name}.cia"
            shutil.copy2(work_dir / "output.cia", out_path)

            if progress_cb:
                progress_cb(1.0)
            return True, out_path, None
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _update_batch_summary(self) -> None:
        unresolved = sum(1 for it in self.batch_items if it.needs_user_input)
        missing_art = sum(1 for it in self.batch_items if it.needs_assets)
        self.batch_status_row.set_subtitle(
            f"{len(self.batch_items)} ROM(s) | {unresolved} need title | {missing_art} missing art"
        )

        if hasattr(self, "batch_fetch_btn"):
            have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
            self.batch_fetch_btn.set_sensitive(have_key and missing_art > 0)
        if hasattr(self, "batch_build_btn"):
            self.batch_build_btn.set_sensitive(bool(self.batch_items))

    def _update_batch_row_style(self, row: "Adw.ExpanderRow", item: BatchItem, rom_name: str) -> None:
        art_status = "OK" if not item.needs_assets else "Missing"
        title_status = "OK" if not item.needs_user_input else "Needs review"
        row.set_title(item.title or rom_name)
        row.set_subtitle(f"{rom_name} → {item.sd_path} | Title: {title_status} | Art: {art_status}")

        row.remove_css_class("batch-needs-title")
        row.remove_css_class("batch-needs-art")
        if item.needs_user_input:
            row.add_css_class("batch-needs-title")
        elif item.needs_assets:
            row.add_css_class("batch-needs-art")

    def _render_batch_items(self):
        # Remember which rows were expanded so fetches don't collapse them.
        prev_expanded: set[str] = set()
        row = self.batch_listbox.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            key = getattr(row, "batch_key", None)
            try:
                if key and row.get_expanded():
                    prev_expanded.add(str(key))
            except Exception:
                pass
            self.batch_listbox.remove(row)
            row = nxt
        self._batch_expanded_keys = prev_expanded

        def sort_key(it: BatchItem):
            # Problems first, then by title/filename
            return (
                0 if (it.needs_user_input or it.needs_assets) else 1,
                (it.title or Path(it.rom_path).name).lower(),
            )

        for item in sorted(self.batch_items, key=sort_key):
            if self.batch_show_only_problems and not (item.needs_user_input or item.needs_assets):
                continue

            rom_name = Path(item.rom_path).name
            art_status = "OK" if not item.needs_assets else "Missing"
            title_status = "OK" if not item.needs_user_input else "Needs review"

            expander = Adw.ExpanderRow(
                title=item.title or rom_name,
                subtitle=f"{rom_name} → {item.sd_path} | Title: {title_status} | Art: {art_status}",
            )
            expander.batch_key = item.rom_path
            if item.rom_path in self._batch_expanded_keys:
                expander.set_expanded(True)
            if item.needs_user_input:
                expander.add_css_class("batch-needs-title")
            elif item.needs_assets:
                expander.add_css_class("batch-needs-art")

            # ROM paths
            rom_row = Adw.ActionRow(title="ROM File", subtitle=item.rom_path)
            sd_row = Adw.ActionRow(title="SD Path", subtitle=item.sd_path)
            expander.add_row(rom_row)
            expander.add_row(sd_row)

            # Quick previews (icon, label, footer)
            preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            preview_box.set_margin_top(8)
            preview_box.set_margin_bottom(8)

            # Icon preview
            icon_preview = Gtk.Picture()
            icon_preview.set_size_request(64, 64)
            icon_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
            icon_preview_path = item.icon_file
            if icon_preview_path is None and self.batch_icon_path and self.batch_icon_path.exists():
                icon_preview_path = str(self.batch_icon_path)
            self._set_picture_from_file(icon_preview, icon_preview_path, 64, 64)
            preview_box.append(icon_preview)

            # Label preview
            label_preview = Gtk.Picture()
            label_preview.set_size_request(96, 96)
            label_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
            label_preview_path = item.label_file
            if label_preview_path is None and self.batch_cartridge_path and self.batch_cartridge_path.exists():
                label_preview_path = str(self.batch_cartridge_path)
            self._set_picture_from_file(label_preview, label_preview_path, 128, 128)
            preview_box.append(label_preview)

            # Footer preview (generated)
            footer_preview = Gtk.Picture()
            footer_preview.set_size_request(256, 64)
            footer_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
            sub = f"Released: {item.year.strip()}" if item.year.strip() else ""
            footer_path = self._write_footer_preview_png(
                self.batch_template_key, self.batch_template_dir, item.title or rom_name, sub
            )
            self._set_picture_from_file(footer_preview, footer_path, 256, 64)
            preview_box.append(footer_preview)

            preview_row = Adw.ActionRow(title="Preview")
            preview_row.set_child(preview_box)
            expander.add_row(preview_row)

            # Editable metadata
            title_row = Adw.EntryRow(title="Title")
            title_row.set_text(item.title or "")
            year_row = Adw.EntryRow(title="Release Year (optional)")
            year_row.set_text(item.year or "")

            def _on_title_changed(entry, it=item, ex=expander, rn=rom_name, footer_pic=footer_preview):
                it.title = entry.get_text().strip()
                if it.title:
                    it.confidence = 1.0
                self._update_batch_row_style(ex, it, rn)
                self._update_batch_summary()
                sub = f"Released: {it.year.strip()}" if it.year.strip() else ""
                footer_path = self._write_footer_preview_png(
                    self.batch_template_key, self.batch_template_dir, it.title or rn, sub
                )
                self._set_picture_from_file(footer_pic, footer_path, 256, 64)

            def _on_year_changed(entry, it=item, rn=rom_name, footer_pic=footer_preview):
                it.year = entry.get_text().strip()
                sub = f"Released: {it.year.strip()}" if it.year.strip() else ""
                footer_path = self._write_footer_preview_png(
                    self.batch_template_key, self.batch_template_dir, it.title or rn, sub
                )
                self._set_picture_from_file(footer_pic, footer_path, 256, 64)

            title_row.connect("changed", _on_title_changed)
            year_row.connect("changed", _on_year_changed)
            expander.add_row(title_row)
            expander.add_row(year_row)

            # Icon picker + preview
            icon_row = Adw.ActionRow(title="Icon (Home Menu)")
            icon_pic = Gtk.Picture()
            icon_pic.set_size_request(48, 48)
            icon_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            self._set_picture_from_file(icon_pic, item.icon_file, 48, 48)

            if item.icon_file:
                icon_row.set_subtitle(item.icon_file)
            elif self.batch_icon_path and self.batch_icon_path.exists():
                icon_row.set_subtitle(f"Not set (using batch default: {self.batch_icon_path.name})")
            else:
                icon_row.set_subtitle("Not set (no batch default; CIA will use template icon)")
            icon_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            icon_suffix.append(icon_pic)

            icon_browse = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
            icon_fetch = Gtk.Button(label="SGDB", valign=Gtk.Align.CENTER)
            icon_fetch.set_tooltip_text("Pick and download an icon from SteamGridDB")
            icon_fetch.set_sensitive(bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")))
            icon_open = Gtk.Button(icon_name="image-x-generic-symbolic", valign=Gtk.Align.CENTER)
            icon_open.add_css_class("flat")
            icon_open.set_tooltip_text("Open current icon")
            icon_clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
            icon_clear.add_css_class("flat")

            def _set_icon(path: str, it=item, roww=icon_row, pic=icon_pic, ex=expander, rn=rom_name, preview=icon_preview):
                it.icon_file = path
                roww.set_subtitle(path)
                self._set_picture_from_file(pic, path, 48, 48)
                self._set_picture_from_file(preview, path, 64, 64)
                self._update_batch_row_style(ex, it, rn)
                self._update_batch_summary()

            def _clear_icon(_btn, it=item, roww=icon_row, pic=icon_pic, ex=expander, rn=rom_name, preview=icon_preview):
                it.icon_file = None
                if self.batch_icon_path and self.batch_icon_path.exists():
                    roww.set_subtitle(f"Not set (using batch default: {self.batch_icon_path.name})")
                else:
                    roww.set_subtitle("Not set (no batch default; CIA will use template icon)")
                self._set_picture_from_file(pic, None, 48, 48)
                self._set_picture_from_file(preview, None, 64, 64)
                self._update_batch_row_style(ex, it, rn)
                self._update_batch_summary()

            def _open_icon(_btn, it=item):
                if it.icon_file:
                    subprocess.Popen(["xdg-open", it.icon_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            icon_browse.connect(
                "clicked",
                lambda *_: pick_file_zenity_async(
                    "Select Icon Image",
                    [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
                    callback=lambda p: _set_icon(p),
                ),
            )
            icon_open.connect("clicked", _open_icon)
            icon_clear.connect("clicked", _clear_icon)

            icon_fetch.connect(
                "clicked",
                lambda *_,
                it=item: Thread(
                    target=self._do_fetch_item_art_with_prompt, args=(it, "icon"), daemon=True
                ).start(),
            )
            icon_suffix.append(icon_browse)
            icon_suffix.append(icon_fetch)
            icon_suffix.append(icon_open)
            icon_suffix.append(icon_clear)
            icon_row.add_suffix(icon_suffix)
            expander.add_row(icon_row)

            # Label/logo picker + preview
            label_row = Adw.ActionRow(title="Cartridge Label / Logo")
            label_pic = Gtk.Picture()
            label_pic.set_size_request(64, 64)
            label_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            self._set_picture_from_file(label_pic, item.label_file, 64, 64)
            if item.label_file:
                label_row.set_subtitle(item.label_file)
            elif self.batch_cartridge_path and self.batch_cartridge_path.exists():
                label_row.set_subtitle(f"Not set (using batch default: {self.batch_cartridge_path.name})")
            else:
                label_row.set_subtitle("Not set (no batch default; banner will use template label)")

            label_suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            label_suffix.append(label_pic)
            label_browse = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
            label_fetch = Gtk.Button(label="SGDB", valign=Gtk.Align.CENTER)
            label_fetch.set_tooltip_text("Pick and download a logo/label from SteamGridDB")
            label_fetch.set_sensitive(bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")))
            label_open = Gtk.Button(icon_name="image-x-generic-symbolic", valign=Gtk.Align.CENTER)
            label_open.add_css_class("flat")
            label_open.set_tooltip_text("Open current label")
            label_clear = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
            label_clear.add_css_class("flat")

            def _set_label(path: str, it=item, roww=label_row, pic=label_pic, ex=expander, rn=rom_name, preview=label_preview):
                it.label_file = path
                roww.set_subtitle(path)
                self._set_picture_from_file(pic, path, 64, 64)
                self._set_picture_from_file(preview, path, 128, 128)
                self._update_batch_row_style(ex, it, rn)
                self._update_batch_summary()

            def _clear_label(_btn, it=item, roww=label_row, pic=label_pic, ex=expander, rn=rom_name, preview=label_preview):
                it.label_file = None
                if self.batch_cartridge_path and self.batch_cartridge_path.exists():
                    roww.set_subtitle(f"Not set (using batch default: {self.batch_cartridge_path.name})")
                else:
                    roww.set_subtitle("Not set (no batch default; banner will use template label)")
                self._set_picture_from_file(pic, None, 64, 64)
                self._set_picture_from_file(preview, None, 128, 128)
                self._update_batch_row_style(ex, it, rn)
                self._update_batch_summary()

            def _open_label(_btn, it=item):
                if it.label_file:
                    subprocess.Popen(["xdg-open", it.label_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            label_browse.connect(
                "clicked",
                lambda *_: pick_file_zenity_async(
                    "Select Label/Logo Image",
                    [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
                    callback=lambda p: _set_label(p),
                ),
            )
            label_open.connect("clicked", _open_label)
            label_clear.connect("clicked", _clear_label)

            label_fetch.connect(
                "clicked",
                lambda *_,
                it=item: Thread(
                    target=self._do_fetch_item_art_with_prompt, args=(it, "logo"), daemon=True
                ).start(),
            )
            label_suffix.append(label_browse)
            label_suffix.append(label_fetch)
            label_suffix.append(label_open)
            label_suffix.append(label_clear)
            label_row.add_suffix(label_suffix)
            expander.add_row(label_row)

            self.batch_listbox.append(expander)

        self._update_batch_summary()

    def on_fix_unresolved_batch(self, button):
        if not self.batch_items:
            self.set_status("Scan ROMs first", error=True)
            return
        Thread(target=self._do_fix_unresolved_batch, daemon=True).start()

    def _do_fix_unresolved_batch(self):
        for item in self.batch_items:
            if not item.needs_user_input:
                continue
            rom_name = Path(item.rom_path).name
            try:
                result = subprocess.run(
                    [
                        "zenity",
                        "--entry",
                        "--title",
                        "Fix Game Title",
                        "--text",
                        f"Game title for: {rom_name}",
                        "--entry-text",
                        item.title or rom_name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    continue
                title = (result.stdout or "").strip()
                if title:
                    item.title = title
                    item.confidence = 1.0
            except Exception:
                continue

        GLib.idle_add(self._render_batch_items)

    def on_fetch_batch_art(self, button):
        if not self.batch_items:
            self.set_status("Scan ROMs first", error=True)
            return
        if not (self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")):
            self.set_status("Set SteamGridDB API key first", error=True)
            return
        self.batch_fetch_btn.set_sensitive(False)
        self.batch_build_btn.set_sensitive(False)
        self.batch_fetch_progress.set_fraction(0)
        try:
            self.batch_fetch_progress_row.set_visible(True)
        except Exception:
            pass
        self.batch_fetch_progress.set_visible(True)
        Thread(target=self._do_fetch_batch_art, daemon=True).start()

    def _sgdb_select_game_id(self, query: str) -> int | None:
        """Ask user to pick a SteamGridDB game for a title; returns game_id or None."""
        try:
            from steamgriddb import SteamGridDBClient

            client = SteamGridDBClient(api_key=self.sgdb_api_key or None)
            results = client.search_autocomplete(query)[:12]
            if not results:
                return None

            # zenity list: show ID + Name
            cmd = [
                "zenity",
                "--list",
                "--title",
                "Select SteamGridDB Game",
                "--text",
                f"Pick the correct match for: {query}",
                "--column",
                "ID",
                "--column",
                "Name",
            ]
            for g in results:
                cmd.extend([str(g.id), g.name])

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode != 0:
                return None
            selected = (res.stdout or "").strip()
            if not selected:
                return None
            # zenity returns the first column by default (ID)
            return int(selected)
        except Exception:
            return None

    @staticmethod
    def _sgdb_score(asset) -> int:
        for key in ("score", "votes", "upvotes"):
            try:
                v = getattr(asset, key, None)
            except Exception:
                v = None
            if isinstance(v, int):
                return v
            try:
                v = asset.get(key)
            except Exception:
                v = None
            if isinstance(v, int):
                return v
        try:
            up = getattr(asset, "upvotes", None)
            down = getattr(asset, "downvotes", None)
        except Exception:
            up = down = None
        if not isinstance(up, int) or not isinstance(down, int):
            try:
                up = asset.get("upvotes")
                down = asset.get("downvotes")
            except Exception:
                up = down = None
        if isinstance(up, int) and isinstance(down, int):
            return up - down
        return 0

    def _sgdb_pick_asset(self, assets, title: str, kind: str) -> str | None:
        """Let the user pick a specific asset URL from a list; returns URL or None."""
        rows = []
        for asset in assets or []:
            try:
                url = getattr(asset, "url", None) or asset.get("url")
            except Exception:
                url = None
            if not url:
                continue
            try:
                width = getattr(asset, "width", None) or asset.get("width")
                height = getattr(asset, "height", None) or asset.get("height")
                res = f"{width}x{height}" if width and height else "?"
            except Exception:
                res = "?"
            score = self._sgdb_score(asset)
            rows.append((url, res, str(score)))

        if not rows:
            return None

        cmd = [
            "zenity",
            "--list",
            "--title",
            f"Pick {kind} for {title}",
            "--text",
            "Select an asset to download",
            "--column",
            "URL",
            "--column",
            "Resolution",
            "--column",
            "Score",
        ]
        for url, res, score in rows:
            cmd.extend([url, res, score])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if res.returncode != 0:
                return None
            selected = (res.stdout or "").strip()
            return selected or None
        except Exception:
            return None

    def _sgdb_list_assets(self, client, game_id: int, kind: str):
        """Try to list SGDB assets (icon/logo)."""
        try:
            fetcher = getattr(client, f"{kind}s", None)
            if callable(fetcher):
                assets = fetcher(game_id)
                if assets:
                    return assets
        except Exception:
            pass
        # Fallback: try HTTP if available in bundled client
        try:
            from steamgriddb import SteamGridDBClient as LocalClient
        except Exception:
            LocalClient = None
        if LocalClient:
            try:
                alt_client = LocalClient(api_key=self.sgdb_api_key or None)
                if hasattr(alt_client, f"{kind}s"):
                    assets = getattr(alt_client, f"{kind}s")(game_id)
                    return assets or []
            except Exception:
                return []
        return []

    def _prepare_asset_previews(self, client, assets, max_items: int = 8):
        """Download thumbnails (or scaled full assets) for a few items to show in the picker."""
        import tempfile
        from PIL import Image
        import io

        out_assets = []
        for idx, asset in enumerate(assets or []):
            if idx >= max_items:
                break
            url = None
            try:
                url = getattr(asset, "thumb", None) or asset.get("thumb")
                if not url:
                    url = getattr(asset, "url", None) or asset.get("url")
            except Exception:
                url = None
            if not url:
                continue

            thumb_path = None
            try:
                data = client.download(url)
                img = Image.open(io.BytesIO(data))
                img.thumbnail((160, 160))
                out_dir = Path(tempfile.gettempdir()) / "mgba_sgdb_previews"
                out_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(urllib.parse.urlparse(url).path).suffix or ".png"
                thumb_path = out_dir / f"preview_{hash(url) & 0xFFFFFFFF:08x}{ext}"
                img.save(thumb_path)
            except Exception:
                thumb_path = None
            try:
                asset["thumb_path"] = str(thumb_path) if thumb_path else None
            except Exception:
                try:
                    setattr(asset, "thumb_path", str(thumb_path) if thumb_path else None)
                except Exception:
                    pass
            out_assets.append(asset)
        return out_assets

    def _pick_asset_with_preview(self, assets, game_title: str, kind: str) -> str | None:
        """
        Show a small GTK dialog with thumbnails so the user can pick an asset.
        Runs on the main thread; caller blocks on an Event.
        """
        picked = {"url": None}
        done = Event()

        def _run_dialog():
            dialog = Gtk.Dialog(title=f"Select {kind} for {game_title}", transient_for=self, modal=True)
            dialog.set_default_size(520, 420)
            content = dialog.get_content_area()

            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            listbox.add_css_class("boxed-list")
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_child(listbox)
            scrolled.set_vexpand(True)
            content.append(scrolled)

            for asset in assets:
                try:
                    url = getattr(asset, "url", None) or asset.get("url")
                    res = f"{getattr(asset, 'width', None) or asset.get('width') or '?'}x{getattr(asset, 'height', None) or asset.get('height') or '?'}"
                    score = str(self._sgdb_score(asset))
                    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    pic = Gtk.Picture()
                    pic.set_size_request(80, 80)
                    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                    thumb = getattr(asset, "thumb_path", None) or getattr(asset, "thumb", None) or asset.get("thumb_path") if isinstance(asset, dict) else None
                    if thumb and Path(thumb).exists():
                        try:
                            pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(thumb), 80, 80, True)
                            pic.set_paintable(Gdk.Texture.new_for_pixbuf(pix))
                        except Exception:
                            pic.set_paintable(None)
                    row_box.append(pic)

                    labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                    lbl_res = Gtk.Label(label=f"{res}  |  Score {score}")
                    lbl_res.set_xalign(0.0)
                    lbl_url = Gtk.Label(label=url or "")
                    lbl_url.set_xalign(0.0)
                    lbl_url.add_css_class("monospace")
                    lbl_url.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                    labels.append(lbl_res)
                    labels.append(lbl_url)
                    row_box.append(labels)

                    row = Gtk.ListBoxRow()
                    row.asset_url = url
                    row.set_child(row_box)
                    listbox.append(row)
                except Exception:
                    continue

            dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
            dialog.add_button("_Select", Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.OK)

            def _on_response(dlg, resp):
                if resp == Gtk.ResponseType.OK:
                    row = listbox.get_selected_row()
                    if row and getattr(row, "asset_url", None):
                        picked["url"] = row.asset_url
                dialog.destroy()
                done.set()

            dialog.connect("response", _on_response)
            dialog.show()
            return False

        GLib.idle_add(_run_dialog)
        def _timeout():
            done.set()
            return False
        GLib.timeout_add_seconds(30, _timeout)
        done.wait(timeout=35)
        return picked["url"]

    def _do_fetch_item_art(self, item: BatchItem, which: str = "both") -> None:
        """
        Fetch art for a single item from SteamGridDB.
        which: 'icon', 'logo', or 'both'
        """
        self._do_fetch_item_art_internal(item, which=which, force_prompt=False, reset_assets=False)

    def _do_fetch_item_art_with_prompt(self, item: BatchItem, which: str = "both") -> None:
        """Fetch art but always prompt for game selection to override defaults."""
        self._do_fetch_item_art_internal(item, which=which, force_prompt=True, reset_assets=False)

    def _do_fetch_item_art_internal(
        self,
        item: BatchItem,
        *,
        which: str = "both",
        force_prompt: bool,
        reset_assets: bool,
    ) -> None:
        try:
            from steamgriddb import SteamGridDBClient, SteamGridDBError

            if not item.title:
                return

            client = SteamGridDBClient(api_key=self.sgdb_api_key or None)
            cache_dir = self.output_path / ".sgdb_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            game_id = item.sgdb_game_id
            if force_prompt or game_id is None:
                game_id = self._sgdb_select_game_id(item.title)
                if game_id is None:
                    return
                item.sgdb_game_id = game_id
                if reset_assets:
                    if which in ("icon", "both"):
                        item.icon_url = None
                        item.icon_file = None
                    if which in ("logo", "both"):
                        item.logo_url = None
                        item.label_file = None

            if which in ("icon", "both"):
                if force_prompt:
                    assets = self._prepare_asset_previews(client, self._sgdb_list_assets(client, game_id, "icon"))
                    chosen = self._pick_asset_with_preview(assets, item.title, "Icon")
                    if chosen:
                        item.icon_url = chosen
                    elif item.icon_url is None:
                        item.icon_url = client.best_icon_url(game_id)
                elif item.icon_url is None:
                    item.icon_url = client.best_icon_url(game_id)

            if which in ("logo", "both"):
                if force_prompt:
                    assets = self._prepare_asset_previews(client, self._sgdb_list_assets(client, game_id, "logo"))
                    chosen = self._pick_asset_with_preview(assets, item.title, "Logo/Label")
                    if chosen:
                        item.logo_url = chosen
                    elif item.logo_url is None:
                        item.logo_url = client.best_logo_url(game_id)
                elif item.logo_url is None:
                    item.logo_url = client.best_logo_url(game_id)

            game_dir = cache_dir / str(game_id)
            game_dir.mkdir(parents=True, exist_ok=True)

            if which in ("icon", "both") and item.icon_url and item.icon_file is None:
                data = client.download(item.icon_url)
                icon_ext = Path(urllib.parse.urlparse(item.icon_url).path).suffix or ".png"
                icon_path = game_dir / f"icon{icon_ext}"
                icon_path.write_bytes(data)
                item.icon_file = str(icon_path)

            if which in ("logo", "both") and item.logo_url and item.label_file is None:
                data = client.download(item.logo_url)
                logo_ext = Path(urllib.parse.urlparse(item.logo_url).path).suffix or ".png"
                logo_path = game_dir / f"logo{logo_ext}"
                logo_path.write_bytes(data)
                item.label_file = str(logo_path)

        except Exception as e:
            GLib.idle_add(lambda: self.set_status(f"SteamGridDB fetch failed: {e}", error=True))
        finally:
            GLib.idle_add(self._render_batch_items)
            if item is self.single_item:
                GLib.idle_add(self._sync_single_from_item)

    def _do_fetch_batch_art(self):
        try:
            from difflib import SequenceMatcher
            from steamgriddb import SteamGridDBClient, SteamGridDBError

            client = SteamGridDBClient(api_key=self.sgdb_api_key or None)
            cache_dir = self.output_path / ".sgdb_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            candidates = [it for it in self.batch_items if it.title and it.needs_assets]
            total = max(1, len(candidates))
            for idx, item in enumerate(candidates, start=1):
                if not item.title:
                    continue

                if item.sgdb_game_id is None:
                    results = client.search_autocomplete(item.title)
                    if not results:
                        continue
                    best = max(
                        results,
                        key=lambda g: SequenceMatcher(None, item.title.lower(), g.name.lower()).ratio(),
                    )
                    ratio = SequenceMatcher(None, item.title.lower(), best.name.lower()).ratio()
                    if ratio < 0.6:
                        continue
                    item.sgdb_game_id = best.id

                game_id = item.sgdb_game_id
                if game_id is None:
                    continue

                if item.icon_url is None:
                    item.icon_url = client.best_icon_url(game_id)
                if item.logo_url is None:
                    item.logo_url = client.best_logo_url(game_id)

                game_dir = cache_dir / str(game_id)
                game_dir.mkdir(parents=True, exist_ok=True)

                if item.icon_url and item.icon_file is None:
                    data = client.download(item.icon_url)
                    icon_ext = Path(urllib.parse.urlparse(item.icon_url).path).suffix or ".png"
                    icon_path = game_dir / f"icon{icon_ext}"
                    icon_path.write_bytes(data)
                    item.icon_file = str(icon_path)

                if item.logo_url and item.label_file is None:
                    data = client.download(item.logo_url)
                    logo_ext = Path(urllib.parse.urlparse(item.logo_url).path).suffix or ".png"
                    logo_path = game_dir / f"logo{logo_ext}"
                    logo_path.write_bytes(data)
                    item.label_file = str(logo_path)

                frac = idx / total
                GLib.idle_add(lambda f=frac, t=item.title: self._set_batch_fetch_progress(f, t))

        except SteamGridDBError as e:
            GLib.idle_add(lambda: self.set_status(f"SteamGridDB fetch failed: {e}", error=True))
        except Exception as e:
            GLib.idle_add(lambda: self.set_status(f"SteamGridDB fetch failed: {e}", error=True))
        finally:
            GLib.idle_add(self._finish_batch_fetch)

    def _set_batch_fetch_progress(self, fraction: float, title: str) -> bool:
        self.batch_fetch_progress.set_fraction(fraction)
        self.set_status(f"Fetching art… {int(fraction*100)}% ({title})")
        return False

    def _finish_batch_fetch(self) -> bool:
        try:
            self.batch_fetch_progress.set_visible(False)
            if hasattr(self, "batch_fetch_progress_row"):
                self.batch_fetch_progress_row.set_visible(False)
        except Exception:
            pass
        self._render_batch_items()
        if hasattr(self, "batch_build_btn"):
            self.batch_build_btn.set_sensitive(bool(self.batch_items))
        self.set_status("Art fetch complete")
        return False

    def on_build_batch_all(self, button):
        if not self.batch_items:
            self.set_status("Scan ROMs first", error=True)
            return
        if self.docker_status != 'ready':
            self.set_status("Please build Docker image first", error=True)
            return
        if not self._check_batch_template():
            self.set_status("Batch template validation failed", error=True)
            return
        self.batch_fetch_btn.set_sensitive(False)
        self.batch_build_btn.set_sensitive(False)
        Thread(target=self._do_build_batch_all, daemon=True).start()

    def _do_build_batch_all(self):
        try:
            if not self._check_batch_template():
                GLib.idle_add(lambda: self.set_status("Batch template validation failed", error=True))
                return
            default_icon = self.batch_icon_path if self.batch_icon_path and self.batch_icon_path.exists() else None
            default_label = (
                self.batch_cartridge_path if self.batch_cartridge_path and self.batch_cartridge_path.exists() else None
            )
            shell_color = self.batch_shell_color if self.batch_template_key == "universal_vc" else None

            total = max(1, len(self.batch_items))
            for idx, item in enumerate(self.batch_items, start=1):
                title = item.title or Path(item.rom_path).stem
                success, _, error = self._build_forwarder_item(
                    item,
                    template_key=self.batch_template_key,
                    template_dir=self.batch_template_dir,
                    output_dir=self.output_path,
                    default_icon=default_icon,
                    default_label=default_label,
                    bg_color=self.batch_cartridge_bg_color,
                    shell_color=shell_color,
                )

                if not success:
                    GLib.idle_add(
                        lambda msg=error or f"CIA failed for {title}": self.set_status(msg, error=True)
                    )
                    continue

                frac = idx / total

                def _update(f=frac, t=title, i=idx, tot=total):
                    self.set_progress(f)
                    self.set_status(f"Built {i}/{tot}: {t}")
                    return False

                GLib.idle_add(_update)

            GLib.idle_add(lambda: self.set_status(f"Batch complete: {len(self.batch_items)} item(s) processed"))
        except Exception as e:
            GLib.idle_add(lambda: self.set_status(f"Batch build error: {e}", error=True))
        finally:
            GLib.idle_add(self._render_batch_items)

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

    def _draw_shell_color_preview(self, area, cr, width, height):
        """Draw the shell tint preview box."""
        if self.shell_color:
            r, g, b = self.shell_color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
        else:
            cr.set_source_rgb(0.2, 0.2, 0.2)
        cr.rectangle(0, 0, width, height)
        cr.fill()

    def _clear_cartridge_color(self, button):
        """Clear the label background color (reset to transparent)."""
        self.cartridge_bg_color = None
        self.cartridge_color_row.set_subtitle("Background behind logo (transparent by default)")
        self.color_preview_box.queue_draw()

    def _clear_shell_color(self, button):
        """Clear the cartridge shell tint (reset to template default)."""
        self.shell_color = None
        self.shell_color_row.set_subtitle("Tint cartridge/body (template default if unset)")
        self.shell_color_preview_box.queue_draw()

    def on_pick_cartridge_color(self, button):
        """Open color picker for label background."""
        def run_color_picker():
            cmd = ['zenity', '--color-selection', '--title', 'Select Label Background Color']
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
        """Set the label background color."""
        self.cartridge_bg_color = (r, g, b)
        self.cartridge_color_row.set_subtitle(f"RGB({r}, {g}, {b})")
        self.color_preview_box.queue_draw()

    def on_pick_shell_color(self, button):
        """Open color picker for Universal VC cartridge shell tint."""
        def run_color_picker():
            cmd = ['zenity', '--color-selection', '--title', 'Select Cartridge Shell Color']
            if self.shell_color:
                r, g, b = self.shell_color
                cmd.extend(['--color', f'rgb({r},{g},{b})'])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0 and result.stdout.strip():
                    color_str = result.stdout.strip()
                    if color_str.startswith('rgb('):
                        parts = color_str[4:-1].split(',')
                        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
                    elif color_str.startswith('#'):
                        r = int(color_str[1:3], 16)
                        g = int(color_str[3:5], 16)
                        b = int(color_str[5:7], 16)
                    else:
                        return
                    GLib.idle_add(self._set_shell_color, r, g, b)
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass
        Thread(target=run_color_picker, daemon=True).start()

    def _set_shell_color(self, r, g, b):
        """Set the Universal VC cartridge shell tint."""
        self.shell_color = (r, g, b)
        self.shell_color_row.set_subtitle(f"RGB({r}, {g}, {b})")
        self.shell_color_preview_box.queue_draw()

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
    
    def on_create(self, button):
        game_name = self.game_name_row.get_text().strip()
        rom_path = (self.single_sd_path or "").strip()
        
        if not game_name:
            self.set_status("Please enter a title", error=True)
            return
        
        if not rom_path:
            self.set_status("Please select a ROM file", error=True)
            return
        
        if self.docker_status != 'ready':
            self.set_status("Please build Docker image first", error=True)
            return
        
        if not self._check_template():
            self.set_status("Template validation failed", error=True)
            return
        
        self.create_btn.set_sensitive(False)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        Thread(target=self._do_create, daemon=True).start()
    
    def _do_create(self):
        try:
            item = self._sync_single_item_from_ui()
            if not item:
                self.set_status("Please select a ROM file", error=True)
                return

            icon_path = self.icon_path if self.icon_path and self.icon_path.exists() else None
            label_path = self.cartridge_path if self.cartridge_path and self.cartridge_path.exists() else None
            shell_color = self.shell_color if self.current_template_key == "universal_vc" else None

            success, output_file, error = self._build_forwarder_item(
                item,
                template_key=self.current_template_key,
                template_dir=self.template_dir,
                output_dir=self.output_path,
                default_icon=icon_path,
                default_label=label_path,
                bg_color=self.cartridge_bg_color,
                shell_color=shell_color,
                progress_cb=self.set_progress,
                status_cb=self.set_status,
            )

            if success and output_file:
                self.set_status(f"Created: {output_file.name}")
            else:
                self.set_status(error or "Build failed", error=True)
        except subprocess.TimeoutExpired:
            self.set_status("Build timed out", error=True)
        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)
        finally:
            GLib.idle_add(self._finish_create)
    
    def _finish_create(self):
        self.create_btn.set_sensitive(True)
        self.progress_bar.set_visible(False)

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
