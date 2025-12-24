#!/usr/bin/env python3
"""
mGBA 3DS Forwarder Creator - GTK4/Adwaita Edition with Docker Support
Supports multiple banner templates: NSUI GBA VC and Universal VC
Uses zenity for file dialogs to avoid GTK4 FileChooser schema issues
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gdk, GdkPixbuf, Pango

import subprocess
import tempfile
import shutil
import os
import json
import hashlib
import io
import urllib.parse
import logging
from pathlib import Path
from threading import Thread, Event

# Setup logging - errors go to stderr for debugging
logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s: %(message)s'
)

CLAMP_MAX_WIDTH = 820

from batch_tools import BatchItem, title_from_rom_filename, get_rom_type


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
    """Check if Docker is available, accessible, and image is built."""
    try:
        result = subprocess.run(
            ['docker', 'images', '-q', 'mgba-forwarder'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return 'ready' if result.stdout.strip() else 'no_image'
        err = (result.stderr or result.stdout or "").lower()
        if "permission denied" in err or "docker.sock" in err:
            return 'no_access'
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
                path = result.stdout.strip()
                GLib.idle_add(callback, path)
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
        self._template_toggle_updating = False

        self.docker_status = check_docker()

        # Output path
        self.output_path = Path.home() / "3ds-forwarders"

        # ROM lists (separate for GBA and NDS)
        self.gba_batch_items: list[BatchItem] = []
        self.nds_batch_items: list[BatchItem] = []
        self.gba_show_only_problems: bool = False

        # Debounce timers for preview updates (keyed by rom_path)
        self._preview_debounce_timers: dict[str, int] = {}
        self.nds_show_only_problems: bool = False
        self._gba_expanded_keys: set[str] = set()
        self._nds_expanded_keys: set[str] = set()
        self.nds_batch_fit_mode: str = "fit"  # Default fit mode for NDS ROMs

        # SteamGridDB
        self.sgdb_api_key: str = ""

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

    def _warn_template_issues(self, template_key: str, template_dir: Path, context: str) -> None:
        """Non-blocking warning if a template folder or required files are missing."""
        info = TEMPLATES.get(template_key)
        if not info:
            msg = f"{context}: unknown template '{template_key}' (continuing anyway)"
            print(msg, flush=True)
            self.set_status(msg, error=True)
            return
        if not template_dir.exists():
            msg = f"{context}: template folder missing at {template_dir} (continuing anyway)"
            print(msg, flush=True)
            self.set_status(msg, error=True)
            return
        missing = [f for f in info.get("required_files", []) if not (template_dir / f).exists()]
        if missing:
            msg = f"{context}: template missing files: {', '.join(missing)} (continuing anyway)"
            print(msg, flush=True)
            self.set_status(msg, error=True)

    def _setup_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        header = Adw.HeaderBar()
        main_box.append(header)

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)
        main_box.append(self.view_stack)

        switcher = Adw.ViewSwitcherTitle()
        switcher.set_property("stack", self.view_stack)
        header.set_title_widget(switcher)

        # --- GBA ROMs tab ---
        gba_scrolled = Gtk.ScrolledWindow()
        gba_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        gba_scrolled.set_vexpand(True)

        gba_clamp = Adw.Clamp()
        gba_clamp.set_maximum_size(CLAMP_MAX_WIDTH)
        gba_clamp.set_tightening_threshold(CLAMP_MAX_WIDTH)
        gba_clamp.set_hexpand(True)
        gba_scrolled.set_child(gba_clamp)

        gba_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        gba_content.set_margin_top(12)
        gba_content.set_margin_bottom(12)
        gba_content.set_margin_start(12)
        gba_content.set_margin_end(12)
        gba_clamp.set_child(gba_content)

        self._build_gba_tab(gba_content)
        self.view_stack.add_titled(gba_scrolled, "gba", "GBA ROMs")

        # Setup drag & drop for GBA ROM files
        self._setup_drag_drop(gba_scrolled, "gba")

        # --- NDS ROMs tab ---
        nds_scrolled = Gtk.ScrolledWindow()
        nds_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        nds_scrolled.set_vexpand(True)

        nds_clamp = Adw.Clamp()
        nds_clamp.set_maximum_size(CLAMP_MAX_WIDTH)
        nds_clamp.set_tightening_threshold(CLAMP_MAX_WIDTH)
        nds_clamp.set_hexpand(True)
        nds_scrolled.set_child(nds_clamp)

        nds_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        nds_content.set_margin_top(12)
        nds_content.set_margin_bottom(12)
        nds_content.set_margin_start(12)
        nds_content.set_margin_end(12)
        nds_clamp.set_child(nds_content)

        self._build_nds_tab(nds_content)
        self.view_stack.add_titled(nds_scrolled, "nds", "NDS ROMs")

        # Setup drag & drop for NDS ROM files
        self._setup_drag_drop(nds_scrolled, "nds")

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

    def _setup_drag_drop(self, widget, rom_type: str = "gba"):
        """Setup drag & drop support for ROM files."""
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_drop, rom_type)
        drop_target.connect("enter", self._on_drag_enter, rom_type)
        drop_target.connect("leave", self._on_drag_leave)
        widget.add_controller(drop_target)

    def _on_drag_enter(self, drop_target, x, y, rom_type: str = "gba"):
        """Handle drag enter event."""
        ext = ".gba" if rom_type == "gba" else ".nds"
        self.set_status(f"Drop {ext.upper()} ROM files here...")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, drop_target):
        """Handle drag leave event."""
        self.set_status("Ready")

    def _on_drop(self, drop_target, value, x, y, rom_type: str = "gba"):
        """Handle file drop."""
        if not isinstance(value, Gdk.FileList):
            return False

        files = value.get_files()
        added = 0
        ext = ".gba" if rom_type == "gba" else ".nds"
        batch_items = self.gba_batch_items if rom_type == "gba" else self.nds_batch_items
        existing = {it.rom_path for it in batch_items}
        fit_mode = self.gba_batch_fit_mode if rom_type == "gba" else self.nds_batch_fit_mode

        for gfile in files:
            path = gfile.get_path()
            if not path:
                continue

            p = Path(path)

            # Handle directories - scan for ROM files
            if p.is_dir():
                for rom in p.rglob(f"*{ext}"):
                    if str(rom) in existing:
                        continue
                    title, confidence = title_from_rom_filename(rom.name)
                    sd_path = f"/roms/{rom_type}/{rom.name}"
                    batch_items.append(BatchItem(
                        rom_path=str(rom),
                        sd_path=sd_path,
                        title=title,
                        confidence=confidence,
                        fit_mode=fit_mode,
                    ))
                    existing.add(str(rom))
                    added += 1
            # Handle individual ROM files
            elif p.suffix.lower() == ext:
                if str(p) in existing:
                    continue
                title, confidence = title_from_rom_filename(p.name)
                sd_path = f"/roms/{rom_type}/{p.name}"
                batch_items.append(BatchItem(
                    rom_path=str(p),
                    sd_path=sd_path,
                    title=title,
                    confidence=confidence,
                    fit_mode=fit_mode,
                ))
                existing.add(str(p))
                added += 1

        if added > 0:
            if rom_type == "gba":
                self._render_gba_batch_items()
            else:
                self._render_nds_batch_items()
            self.set_status(f"Added {added} {rom_type.upper()} ROM(s) via drag & drop")
        else:
            self.set_status(f"No new {rom_type.upper()} ROMs found in dropped files")

        return True

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

    def _build_gba_tab(self, content: Gtk.Box) -> None:
        """Build GBA ROM handling tab."""
        # --- ROM Selection Group ---
        rom_group = Adw.PreferencesGroup()
        content.append(rom_group)

        # ROM selection buttons row
        rom_buttons_row = Adw.ActionRow(title="Add ROMs", subtitle="Select individual ROMs or scan a folder")
        rom_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        rom_buttons.set_valign(Gtk.Align.CENTER)

        add_rom_btn = Gtk.Button(label="Add ROM", valign=Gtk.Align.CENTER)
        add_rom_btn.connect("clicked", self.on_add_gba_rom)

        add_folder_btn = Gtk.Button(label="Add Folder", valign=Gtk.Align.CENTER)
        add_folder_btn.connect("clicked", self.on_add_gba_folder)

        clear_btn = Gtk.Button(label="Clear All", valign=Gtk.Align.CENTER)
        clear_btn.add_css_class("destructive-action")
        clear_btn.connect("clicked", self.on_clear_all_gba_roms)

        rom_buttons.append(add_rom_btn)
        rom_buttons.append(add_folder_btn)
        rom_buttons.append(clear_btn)
        rom_buttons_row.add_suffix(rom_buttons)
        rom_group.add(rom_buttons_row)

        # Output folder (escape path for markup)
        self.output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=GLib.markup_escape_text(str(self.output_path))
        )
        output_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        output_btn.connect("clicked", self.on_browse_output)
        self.output_row.add_suffix(output_btn)
        rom_group.add(self.output_row)

        # Template Selection (shared for all ROMs)
        self.template_row = Adw.ActionRow(title="Banner Template")
        self.template_row.set_subtitle(TEMPLATES[self.current_template_key]["description"])
        self.template_buttons: dict[str, Gtk.ToggleButton] = {}
        template_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        template_box.set_halign(Gtk.Align.END)
        for key, info in TEMPLATES.items():
            btn = Gtk.ToggleButton(label=info["name"])
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("toggled", self._on_template_button_toggled, key)
            self.template_buttons[key] = btn
            template_box.append(btn)
        if self.current_template_key in self.template_buttons:
            self.template_buttons[self.current_template_key].set_active(True)
        self.template_row.add_suffix(template_box)
        rom_group.add(self.template_row)

        # Batch Fit Mode (apply to all ROMs)
        fit_mode_row = Adw.ActionRow(
            title="Default Label Fit Mode",
            subtitle="Apply to all ROMs when building"
        )
        fit_mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fit_mode_box.set_valign(Gtk.Align.CENTER)

        self.gba_batch_fit_mode = "fit"  # Default fit mode for new GBA ROMs

        def _on_gba_fit_toggled(btn, mode):
            if not btn.get_active():
                return
            self.gba_batch_fit_mode = mode

        self.gba_fit_buttons = {}
        first_btn = None
        for mode, label, tooltip in [
            ("fit", "Fit", "Scale to fit, center (entire image visible)"),
            ("fill", "Fill", "Scale to cover, crop edges if needed"),
            ("stretch", "Stretch", "Stretch to exact size (may distort)"),
        ]:
            btn = Gtk.ToggleButton(label=label)
            btn.set_tooltip_text(tooltip)
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", _on_gba_fit_toggled, mode)
            self.gba_fit_buttons[mode] = btn
            fit_mode_box.append(btn)

        self.gba_fit_buttons["fit"].set_active(True)

        apply_btn = Gtk.Button(label="Apply to All", valign=Gtk.Align.CENTER)
        apply_btn.set_tooltip_text("Apply this fit mode to all ROMs in the list")
        apply_btn.connect("clicked", self._on_apply_gba_fit_mode_all)
        fit_mode_box.append(apply_btn)

        fit_mode_row.add_suffix(fit_mode_box)
        rom_group.add(fit_mode_row)

        # --- ROM List/Status Group ---
        list_group = Adw.PreferencesGroup()
        content.append(list_group)

        # Fetch progress bar
        self.gba_fetch_progress = Gtk.ProgressBar()
        self.gba_fetch_progress.set_visible(False)
        self.gba_fetch_progress_row = Adw.PreferencesRow()
        self.gba_fetch_progress_row.set_child(self.gba_fetch_progress)
        self.gba_fetch_progress_row.set_visible(False)
        list_group.add(self.gba_fetch_progress_row)

        # Status row
        self.gba_status_row = Adw.ActionRow(title="Status", subtitle="No ROMs added yet")
        list_group.add(self.gba_status_row)

        # Fetch Art row with description
        fetch_row = Adw.ActionRow(
            title="Fetch Art",
            subtitle="Download icons and labels from SteamGridDB for all ROMs"
        )
        self.gba_fetch_btn = Gtk.Button(label="Fetch All", valign=Gtk.Align.CENTER)
        self.gba_fetch_btn.connect("clicked", self.on_fetch_gba_batch_art)
        self.gba_fetch_btn.set_sensitive(False)
        fetch_row.add_suffix(self.gba_fetch_btn)
        list_group.add(fetch_row)

        # Filter switch
        filter_row = Adw.ActionRow(
            title="Show Only Problems",
            subtitle="Hide rows that already have title and art"
        )
        self.gba_filter_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.gba_filter_switch.set_active(self.gba_show_only_problems)
        self.gba_filter_switch.connect("notify::active", self._on_gba_filter_toggle)
        filter_row.add_suffix(self.gba_filter_switch)
        list_group.add(filter_row)

        # ROM list box
        self.gba_listbox = Gtk.ListBox()
        self.gba_listbox.add_css_class("boxed-list")
        self.gba_listbox.set_hexpand(True)
        gba_list_row = Adw.PreferencesRow()
        gba_list_row.set_child(self.gba_listbox)
        list_group.add(gba_list_row)

        # Build row with description
        build_row = Adw.ActionRow(
            title="Build",
            subtitle="Generate CIA forwarder files for all ROMs (requires Docker)"
        )
        self.gba_build_btn = Gtk.Button(label="Build All CIAs", valign=Gtk.Align.CENTER)
        self.gba_build_btn.add_css_class("suggested-action")
        self.gba_build_btn.connect("clicked", self.on_build_all_gba)
        self.gba_build_btn.set_sensitive(False)
        build_row.add_suffix(self.gba_build_btn)
        list_group.add(build_row)

    def _build_nds_tab(self, content: Gtk.Box) -> None:
        """Build NDS ROM handling tab."""
        # --- ROM Selection Group ---
        rom_group = Adw.PreferencesGroup()
        content.append(rom_group)

        # ROM selection buttons row
        rom_buttons_row = Adw.ActionRow(title="Add ROMs", subtitle="Select individual ROMs or scan a folder")
        rom_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        rom_buttons.set_valign(Gtk.Align.CENTER)

        add_rom_btn = Gtk.Button(label="Add ROM", valign=Gtk.Align.CENTER)
        add_rom_btn.connect("clicked", self.on_add_nds_rom)

        add_folder_btn = Gtk.Button(label="Add Folder", valign=Gtk.Align.CENTER)
        add_folder_btn.connect("clicked", self.on_add_nds_folder)

        clear_btn = Gtk.Button(label="Clear All", valign=Gtk.Align.CENTER)
        clear_btn.add_css_class("destructive-action")
        clear_btn.connect("clicked", self.on_clear_all_nds_roms)

        rom_buttons.append(add_rom_btn)
        rom_buttons.append(add_folder_btn)
        rom_buttons.append(clear_btn)
        rom_buttons_row.add_suffix(rom_buttons)
        rom_group.add(rom_buttons_row)

        # Output folder (escape path for markup)
        self.nds_output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=GLib.markup_escape_text(str(self.output_path))
        )
        output_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        output_btn.connect("clicked", self.on_browse_nds_output)
        self.nds_output_row.add_suffix(output_btn)
        rom_group.add(self.nds_output_row)

        # Batch Fit Mode (apply to all ROMs)
        fit_mode_row = Adw.ActionRow(
            title="Default Label Fit Mode",
            subtitle="Apply to all ROMs when building"
        )
        fit_mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fit_mode_box.set_valign(Gtk.Align.CENTER)

        def _on_nds_fit_toggled(btn, mode):
            if not btn.get_active():
                return
            self.nds_batch_fit_mode = mode

        self.nds_fit_buttons = {}
        first_btn = None
        for mode, label, tooltip in [
            ("fit", "Fit", "Scale to fit, center (entire image visible)"),
            ("fill", "Fill", "Scale to cover, crop edges if needed"),
            ("stretch", "Stretch", "Stretch to exact size (may distort)"),
        ]:
            btn = Gtk.ToggleButton(label=label)
            btn.set_tooltip_text(tooltip)
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", _on_nds_fit_toggled, mode)
            self.nds_fit_buttons[mode] = btn
            fit_mode_box.append(btn)

        self.nds_fit_buttons["fit"].set_active(True)

        apply_btn = Gtk.Button(label="Apply to All", valign=Gtk.Align.CENTER)
        apply_btn.set_tooltip_text("Apply this fit mode to all ROMs in the list")
        apply_btn.connect("clicked", self._on_apply_nds_fit_mode_all)
        fit_mode_box.append(apply_btn)

        fit_mode_row.add_suffix(fit_mode_box)
        rom_group.add(fit_mode_row)

        # --- ROM List/Status Group ---
        list_group = Adw.PreferencesGroup()
        content.append(list_group)

        # Fetch progress bar
        self.nds_fetch_progress = Gtk.ProgressBar()
        self.nds_fetch_progress.set_visible(False)
        self.nds_fetch_progress_row = Adw.PreferencesRow()
        self.nds_fetch_progress_row.set_child(self.nds_fetch_progress)
        self.nds_fetch_progress_row.set_visible(False)
        list_group.add(self.nds_fetch_progress_row)

        # Status row
        self.nds_status_row = Adw.ActionRow(title="Status", subtitle="No ROMs added yet")
        list_group.add(self.nds_status_row)

        # Fetch Art row with description
        fetch_row = Adw.ActionRow(
            title="Fetch Art",
            subtitle="Download labels from SteamGridDB for all ROMs (icons extracted from ROM)"
        )
        self.nds_fetch_btn = Gtk.Button(label="Fetch All", valign=Gtk.Align.CENTER)
        self.nds_fetch_btn.connect("clicked", self.on_fetch_nds_batch_art)
        self.nds_fetch_btn.set_sensitive(False)
        fetch_row.add_suffix(self.nds_fetch_btn)
        list_group.add(fetch_row)

        # Filter switch
        filter_row = Adw.ActionRow(
            title="Show Only Problems",
            subtitle="Hide rows that already have title and art"
        )
        self.nds_filter_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.nds_filter_switch.set_active(self.nds_show_only_problems)
        self.nds_filter_switch.connect("notify::active", self._on_nds_filter_toggle)
        filter_row.add_suffix(self.nds_filter_switch)
        list_group.add(filter_row)

        # ROM list box
        self.nds_listbox = Gtk.ListBox()
        self.nds_listbox.add_css_class("boxed-list")
        self.nds_listbox.set_hexpand(True)
        nds_list_row = Adw.PreferencesRow()
        nds_list_row.set_child(self.nds_listbox)
        list_group.add(nds_list_row)

        # Build row with description
        build_row = Adw.ActionRow(
            title="Build",
            subtitle="Generate CIA forwarder files for all ROMs"
        )
        self.nds_build_btn = Gtk.Button(label="Build All CIAs", valign=Gtk.Align.CENTER)
        self.nds_build_btn.add_css_class("suggested-action")
        self.nds_build_btn.connect("clicked", self.on_build_all_nds)
        self.nds_build_btn.set_sensitive(False)
        build_row.add_suffix(self.nds_build_btn)
        list_group.add(build_row)

    # =========================================================================
    # NDS ROM Handlers
    # =========================================================================

    def on_add_nds_rom(self, button):
        """Add a single NDS ROM file to the list."""
        def on_selected(path):
            try:
                p = Path(path)
                if any(it.rom_path == str(p) for it in self.nds_batch_items):
                    self.set_status(f"ROM already added: {p.name}")
                    return

                title, confidence = title_from_rom_filename(p.name)
                sd_path = f"/roms/nds/{p.name}"
                item = BatchItem(
                    rom_path=str(p),
                    sd_path=sd_path,
                    title=title,
                    confidence=confidence,
                    fit_mode=self.nds_batch_fit_mode,
                )
                self.nds_batch_items.append(item)
                self._render_nds_batch_items()
                self.set_status(f"Added: {p.name}")
            except Exception as e:
                self.set_status(f"Failed to add ROM: {e}", error=True)

        pick_file_zenity_async("Select NDS ROM", [("NDS ROMs", ["*.nds"])], callback=on_selected)

    def on_add_nds_folder(self, button):
        """Scan a folder for NDS ROMs and add them to the list."""
        def on_selected(path):
            try:
                folder = Path(path)
                if not folder.exists() or not folder.is_dir():
                    self.set_status("Invalid folder", error=True)
                    return
                self.set_status("Scanning folder...")
                Thread(target=self._do_scan_nds_folder, args=(folder,), daemon=True).start()
            except Exception as e:
                self.set_status(f"Failed to scan folder: {e}", error=True)

        pick_folder_zenity_async("Select NDS ROM Folder", callback=on_selected)

    def _do_scan_nds_folder(self, folder: Path):
        """Scan folder for NDS ROMs in background thread."""
        roms = sorted(folder.rglob("*.nds"))
        existing = {it.rom_path for it in self.nds_batch_items}
        new_count = 0

        for rom in roms:
            if str(rom) in existing:
                continue
            title, confidence = title_from_rom_filename(rom.name)
            sd_path = f"/roms/nds/{rom.name}"
            self.nds_batch_items.append(BatchItem(
                rom_path=str(rom),
                sd_path=sd_path,
                title=title,
                confidence=confidence,
                fit_mode=self.nds_batch_fit_mode,
            ))
            new_count += 1

        def update():
            self._render_nds_batch_items()
            self.set_status(f"Added {new_count} NDS ROM(s) from folder")
        GLib.idle_add(update)

    def on_clear_all_nds_roms(self, button):
        """Clear all NDS ROMs from the list."""
        self.nds_batch_items.clear()
        self._render_nds_batch_items()
        self.set_status("Cleared all NDS ROMs")

    def _on_apply_nds_fit_mode_all(self, button):
        """Apply the current batch fit mode to all NDS ROMs."""
        if not self.nds_batch_items:
            self.set_status("No ROMs to update", error=True)
            return
        for item in self.nds_batch_items:
            item.fit_mode = self.nds_batch_fit_mode
        self._render_nds_batch_items()
        self.set_status(f"Applied '{self.nds_batch_fit_mode}' fit mode to {len(self.nds_batch_items)} ROM(s)")

    def _on_nds_filter_toggle(self, switch, param):
        self.nds_show_only_problems = bool(switch.get_active())
        self._render_nds_batch_items()

    def on_browse_nds_output(self, button):
        """Browse for NDS output folder."""
        self.on_browse_output(button)  # Reuse the same output folder

    def on_fetch_nds_batch_art(self, button):
        """Fetch art for all NDS ROMs."""
        if not self.nds_batch_items:
            self.set_status("Add ROMs first", error=True)
            return
        if not (self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")):
            self.set_status("Set SteamGridDB API key first", error=True)
            return
        self.nds_fetch_btn.set_sensitive(False)
        self.nds_build_btn.set_sensitive(False)
        self.nds_fetch_progress.set_fraction(0)
        try:
            self.nds_fetch_progress.set_visible(True)
            self.nds_fetch_progress_row.set_visible(True)
        except Exception:
            pass
        # For NDS, we only fetch labels (icon comes from ROM)
        Thread(target=self._do_fetch_nds_batch_art, daemon=True).start()

    def _do_fetch_nds_batch_art(self):
        """Fetch art for NDS ROMs (label only, icon from ROM)."""
        try:
            candidates = [it for it in self.nds_batch_items if it.title and it.needs_assets]
            if not candidates:
                GLib.idle_add(lambda: self.set_status("No NDS ROMs need art"))
                return

            self._do_fetch_batch_art_for_items(
                self.nds_batch_items,
                fetch_icon=False,  # NDS icons come from ROM
                fetch_logo=True,
                progress_callback=self._set_nds_fetch_progress,
            )
        except Exception as e:
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"Fetch error: {err}", error=True))
        finally:
            GLib.idle_add(self._finish_nds_batch_fetch)

    def _set_nds_fetch_progress(self, fraction: float, title: str) -> bool:
        self.nds_fetch_progress.set_fraction(fraction)
        self.set_status(f"[NDS] Fetching art… {int(fraction*100)}% ({title})")
        return False

    def _finish_nds_batch_fetch(self) -> bool:
        try:
            self.nds_fetch_progress.set_visible(False)
            self.nds_fetch_progress_row.set_visible(False)
        except Exception:
            pass
        self._render_nds_batch_items()
        have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
        missing_art = sum(1 for it in self.nds_batch_items if it.needs_assets)
        self.nds_fetch_btn.set_sensitive(have_key and missing_art > 0)
        self.nds_build_btn.set_sensitive(bool(self.nds_batch_items))
        self.set_status("[NDS] Fetch complete")
        return False

    def on_build_all_nds(self, button):
        """Build CIAs for all NDS ROMs in the list."""
        if not self.nds_batch_items:
            self.set_status("Add ROMs first", error=True)
            return
        # Check for NDS build tools
        generator_script = self.script_dir / "generator" / "generator.py"
        if not generator_script.exists():
            self.set_status("NDS generator script not found", error=True)
            return
        self.nds_build_btn.set_sensitive(False)
        self.nds_fetch_btn.set_sensitive(False)
        Thread(target=self._do_build_all_nds, daemon=True).start()

    def _do_build_all_nds(self):
        """Build all NDS forwarders in background thread."""
        import sys
        try:
            for item in self.nds_batch_items:
                item.build_status = "pending"
            GLib.idle_add(self._render_nds_batch_items)

            total = max(1, len(self.nds_batch_items))
            success_count = 0
            fail_count = 0
            generator_script = self.script_dir / "generator" / "generator.py"

            for idx, item in enumerate(self.nds_batch_items, start=1):
                title = item.title or Path(item.rom_path).stem
                item.build_status = "building"
                GLib.idle_add(self._render_nds_batch_items)

                rom_name = Path(item.rom_path).stem
                out_path = self.output_path / f"{rom_name}.cia"
                year = (item.subtitle or item.year or "").strip()
                subtitle = f"Released: {year}" if year else ""

                args = [
                    sys.executable,
                    str(generator_script),
                    item.rom_path,
                    "--banner-style", "universal",
                    "-o", str(out_path),
                    "--title", title,
                ]
                if item.sd_path:
                    args.extend(["-p", item.sd_path])
                if subtitle:
                    args.extend(["--subtitle", subtitle])
                if item.label_file:
                    args.extend(["-b", item.label_file])
                    args.extend(["--fit-mode", item.fit_mode])

                last_error = None
                generator_dir = self.script_dir / "generator"
                try:
                    result = subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=str(generator_dir))
                    if result.returncode == 0 and out_path.exists():
                        item.build_status = "success"
                        success_count += 1
                    else:
                        item.build_status = "failed"
                        fail_count += 1
                        last_error = (result.stderr or result.stdout or f"Exit code {result.returncode}, output missing: {out_path}").strip()[:300]
                except Exception as e:
                    item.build_status = "failed"
                    fail_count += 1
                    last_error = str(e)

                frac = idx / total
                def _update(f=frac, t=title, i=idx, tot=total, s=success_count, fl=fail_count, err=last_error):
                    self.set_progress(f)
                    if err:
                        self.set_status(f"[NDS] {i}/{tot} FAILED: {err}", error=True)
                    else:
                        self.set_status(f"[NDS] Building {i}/{tot}: {t} (✓{s} ✗{fl})")
                    self._render_nds_batch_items()
                    return False
                GLib.idle_add(_update)

            def _final(s=success_count, f=fail_count):
                if f > 0:
                    self.set_status(f"[NDS] Build complete: {s} succeeded, {f} failed", error=True)
                else:
                    self.set_status(f"[NDS] Build complete: {s} succeeded")
                self._render_nds_batch_items()
            GLib.idle_add(_final)
        except Exception as e:
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"Build error: {err}", error=True))
        finally:
            def finish():
                self.nds_build_btn.set_sensitive(bool(self.nds_batch_items))
                have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
                missing_art = sum(1 for it in self.nds_batch_items if it.needs_assets)
                self.nds_fetch_btn.set_sensitive(have_key and missing_art > 0)
                self.progress_bar.set_visible(False)
            GLib.idle_add(finish)

    def _update_nds_batch_summary(self) -> None:
        """Update the NDS batch status row summary."""
        if not self.nds_batch_items:
            self.nds_status_row.set_subtitle("No ROMs added yet")
        else:
            unresolved = sum(1 for it in self.nds_batch_items if it.needs_user_input)
            missing_art = sum(1 for it in self.nds_batch_items if it.needs_assets)
            count = len(self.nds_batch_items)
            self.nds_status_row.set_subtitle(
                f"{count} ROM(s) | {unresolved} need review | {missing_art} need art"
            )
        have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
        missing_art = sum(1 for it in self.nds_batch_items if it.needs_assets)
        self.nds_fetch_btn.set_sensitive(have_key and missing_art > 0)
        self.nds_build_btn.set_sensitive(bool(self.nds_batch_items))

    def _render_nds_batch_items(self):
        """Rebuild the NDS batch item list UI."""
        # Remember which rows were expanded so fetches don't collapse them.
        prev_expanded: set[str] = set()
        scroll_to_key = getattr(self, '_nds_scroll_to_key', None)
        row = self.nds_listbox.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            key = getattr(row, "batch_key", None)
            try:
                if key and row.get_expanded():
                    prev_expanded.add(str(key))
            except Exception:
                pass
            self.nds_listbox.remove(row)
            row = nxt
        self._nds_expanded_keys = prev_expanded

        def sort_key(it: BatchItem):
            return (it.title or Path(it.rom_path).name).lower()

        for item in sorted(self.nds_batch_items, key=sort_key):
            if self.nds_show_only_problems and not (item.needs_user_input or item.needs_assets):
                continue

            rom_name = Path(item.rom_path).name

            # Build status indicator
            build_status = getattr(item, 'build_status', 'pending')
            if build_status == "building":
                build_indicator = "🔨 Building..."
            elif build_status == "success":
                build_indicator = "✅ Built"
            elif build_status == "failed":
                build_indicator = "❌ Failed"
            else:
                build_indicator = ""

            # Title/art status indicators
            title_status = "✓ Title OK" if not item.needs_user_input else "⚠️ Needs review"
            art_status = "✓ Art OK" if not item.needs_assets else "⚠️ No label"

            # Build subtitle
            safe_rom_name = GLib.markup_escape_text(rom_name)
            safe_title = GLib.markup_escape_text(item.title) if item.title else safe_rom_name
            subtitle_parts = [safe_rom_name, title_status, art_status]
            if build_indicator:
                subtitle_parts.append(build_indicator)

            expander = Adw.ExpanderRow(
                title=safe_title,
                subtitle=" | ".join(subtitle_parts),
            )
            expander.batch_key = item.rom_path
            if item.rom_path in self._nds_expanded_keys:
                expander.set_expanded(True)

            # Add prefix icon based on status
            if build_status == "building":
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
                prefix_icon.add_css_class("accent")
            elif build_status == "success":
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                prefix_icon.add_css_class("success")
            elif build_status == "failed":
                prefix_icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
                prefix_icon.add_css_class("error")
            elif item.needs_user_input or item.needs_assets:
                prefix_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                prefix_icon.add_css_class("warning")
            else:
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                prefix_icon.add_css_class("success")
            expander.add_prefix(prefix_icon)

            # Add remove button
            def _remove_rom(_btn, rom_path=item.rom_path):
                self.nds_batch_items = [it for it in self.nds_batch_items if it.rom_path != rom_path]
                self._render_nds_batch_items()
                self._update_nds_batch_summary()

            remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove_btn.add_css_class("flat")
            remove_btn.add_css_class("destructive-action")
            remove_btn.set_tooltip_text("Remove this ROM")
            remove_btn.connect("clicked", _remove_rom)
            expander.add_suffix(remove_btn)

            # CSS class for styling
            if item.needs_user_input:
                expander.add_css_class("batch-needs-title")
            elif item.needs_assets:
                expander.add_css_class("batch-needs-art")
            else:
                expander.add_css_class("batch-complete")

            # ROM paths
            safe_rom_path = GLib.markup_escape_text(item.rom_path)
            safe_sd_path = GLib.markup_escape_text(item.sd_path)
            rom_row = Adw.ActionRow(title="ROM File", subtitle=safe_rom_path)
            sd_row = Adw.ActionRow(title="SD Path", subtitle=safe_sd_path)
            expander.add_row(rom_row)
            expander.add_row(sd_row)

            # Preview section (icon from ROM, label, footer)
            icon_path = self._extract_nds_icon(item.rom_path)
            preview_box, footer_preview = self._create_preview_section(
                item, rom_name, icon_path, icon_label="Icon (ROM)"
            )
            preview_row = Adw.ActionRow(title="Preview")
            preview_row.set_child(preview_box)
            expander.add_row(preview_row)

            # Editable metadata with live footer updates
            title_row = Adw.EntryRow(title="Title")
            title_row.set_text(item.title or "")
            year_row = Adw.EntryRow(title="Release Year (optional)")
            year_row.set_text(item.year or "")

            def _on_title_changed(entry, it=item, ex=expander, rn=rom_name, footer_pic=footer_preview):
                it.title = entry.get_text().strip()
                if it.title:
                    it.confidence = 1.0
                self._update_nds_batch_row_style(ex, it, rn)
                self._update_nds_batch_summary()
                # Debounce the expensive preview update
                def update_preview():
                    sub = f"Released: {it.year.strip()}" if it.year and it.year.strip() else ""
                    footer_path = self._write_footer_preview_png(
                        self.current_template_key, self.template_dir, it.title or rn, sub
                    )
                    self._set_picture_from_file(footer_pic, footer_path, 256, 64)
                self._debounced_preview_update(f"nds_title_{it.rom_path}", update_preview)

            def _on_year_changed(entry, it=item, rn=rom_name, footer_pic=footer_preview):
                it.year = entry.get_text().strip()
                it.subtitle = it.year
                # Debounce the expensive preview update
                def update_preview():
                    sub = f"Released: {it.year.strip()}" if it.year and it.year.strip() else ""
                    footer_path = self._write_footer_preview_png(
                        self.current_template_key, self.template_dir, it.title or rn, sub
                    )
                    self._set_picture_from_file(footer_pic, footer_path, 256, 64)
                self._debounced_preview_update(f"nds_year_{it.rom_path}", update_preview)

            title_row.connect("changed", _on_title_changed)
            year_row.connect("changed", _on_year_changed)
            expander.add_row(title_row)
            expander.add_row(year_row)

            # Label picker using shared helper
            label_row = self._create_label_picker_row(
                item,
                self.nds_batch_items,
                self._render_nds_batch_items,
                self._update_nds_batch_summary,
                "_nds_scroll_to_key",
                sgdb_fetch_callback=self._do_fetch_nds_item_art_with_prompt,
            )
            expander.add_row(label_row)

            # Fit mode selector using shared helper
            fit_row = self._create_fit_mode_selector(
                item,
                self.nds_batch_items,
                self._render_nds_batch_items,
                "_nds_scroll_to_key",
                "_nds_batch_fit_updating",
            )
            expander.add_row(fit_row)

            self.nds_listbox.append(expander)

            # Track row for scroll-to
            if scroll_to_key and item.rom_path == scroll_to_key:
                def scroll_to_row(widget=expander):
                    widget.grab_focus()
                    return False
                GLib.idle_add(scroll_to_row)

        self._update_nds_batch_summary()
        self._nds_scroll_to_key = None  # Clear after use

    # =========================================================================
    # GBA ROM Handlers
    # =========================================================================

    def on_add_gba_rom(self, button):
        """Add a single GBA ROM file to the list."""
        def on_selected(path):
            try:
                p = Path(path)
                # Check if already added
                if any(it.rom_path == str(p) for it in self.gba_batch_items):
                    self.set_status(f"ROM already added: {p.name}")
                    return

                title, confidence = title_from_rom_filename(p.name)
                sd_path = f"/roms/gba/{p.name}"
                item = BatchItem(
                    rom_path=str(p),
                    sd_path=sd_path,
                    title=title,
                    confidence=confidence,
                    fit_mode=self.gba_batch_fit_mode,
                )
                self.gba_batch_items.append(item)
                self._render_gba_batch_items()
                self.set_status(f"Added: {p.name}")
            except Exception as e:
                self.set_status(f"Failed to add ROM: {e}", error=True)

        pick_file_zenity_async("Select GBA ROM", [("GBA ROMs", ["*.gba"])], callback=on_selected)

    def on_add_gba_folder(self, button):
        """Scan a folder for GBA ROMs and add them to the list."""
        def on_selected(path):
            try:
                folder = Path(path)
                if not folder.exists() or not folder.is_dir():
                    self.set_status("Invalid folder", error=True)
                    return

                self.set_status("Scanning folder...")
                Thread(target=self._do_scan_gba_folder, args=(folder,), daemon=True).start()
            except Exception as e:
                self.set_status(f"Failed to scan folder: {e}", error=True)

        pick_folder_zenity_async("Select GBA ROM Folder", callback=on_selected)

    def _do_scan_gba_folder(self, folder: Path):
        """Scan folder for GBA ROMs in background thread."""
        roms = sorted(folder.rglob("*.gba"))
        existing = {it.rom_path for it in self.gba_batch_items}
        new_count = 0

        for rom in roms:
            if str(rom) in existing:
                continue
            title, confidence = title_from_rom_filename(rom.name)
            sd_path = f"/roms/gba/{rom.name}"
            self.gba_batch_items.append(BatchItem(
                rom_path=str(rom),
                sd_path=sd_path,
                title=title,
                confidence=confidence,
                fit_mode=self.gba_batch_fit_mode,
            ))
            new_count += 1

        def update():
            self._render_gba_batch_items()
            self.set_status(f"Added {new_count} GBA ROM(s) from folder")
        GLib.idle_add(update)

    def on_clear_all_gba_roms(self, button):
        """Clear all GBA ROMs from the list."""
        self.gba_batch_items.clear()
        self._render_gba_batch_items()
        self.set_status("Cleared all GBA ROMs")

    def _on_apply_gba_fit_mode_all(self, button):
        """Apply the current batch fit mode to all GBA ROMs."""
        if not self.gba_batch_items:
            self.set_status("No ROMs to update", error=True)
            return
        for item in self.gba_batch_items:
            item.fit_mode = self.gba_batch_fit_mode
        self._render_gba_batch_items()
        self.set_status(f"Applied '{self.gba_batch_fit_mode}' fit mode to {len(self.gba_batch_items)} ROM(s)")

    def on_build_all_gba(self, button):
        """Build CIAs for all GBA ROMs in the list."""
        if not self.gba_batch_items:
            self.set_status("Add ROMs first", error=True)
            return
        if self.docker_status != 'ready':
            self.set_status("Please build Docker image first", error=True)
            return
        self._warn_template_issues(self.current_template_key, self.template_dir, "Build")
        self.gba_build_btn.set_sensitive(False)
        self.gba_fetch_btn.set_sensitive(False)
        Thread(target=self._do_build_all_gba, daemon=True).start()

    def _do_build_all_gba(self):
        """Build all GBA forwarders in background thread."""
        try:
            # Reset all build statuses
            for item in self.gba_batch_items:
                item.build_status = "pending"
            GLib.idle_add(self._render_gba_batch_items)

            total = max(1, len(self.gba_batch_items))
            success_count = 0
            fail_count = 0

            for idx, item in enumerate(self.gba_batch_items, start=1):
                title = item.title or Path(item.rom_path).stem

                # Mark as building and update UI
                item.build_status = "building"
                GLib.idle_add(self._render_gba_batch_items)

                success, _, error = self._build_forwarder_item(
                    item,
                    template_key=self.current_template_key,
                    template_dir=self.template_dir,
                    output_dir=self.output_path,
                )

                if success:
                    item.build_status = "success"
                    success_count += 1
                else:
                    item.build_status = "failed"
                    fail_count += 1
                    GLib.idle_add(
                        lambda msg=error or f"CIA failed for {title}": self.set_status(msg, error=True)
                    )

                frac = idx / total
                def _update(f=frac, t=title, i=idx, tot=total, s=success_count, fl=fail_count):
                    self.set_progress(f)
                    self.set_status(f"[GBA] Building {i}/{tot}: {t} (✓{s} ✗{fl})")
                    self._render_gba_batch_items()
                    return False
                GLib.idle_add(_update)

            def _final():
                self.set_status(f"[GBA] Build complete: {success_count} succeeded, {fail_count} failed")
                self._render_gba_batch_items()
            GLib.idle_add(_final)
        except Exception as e:
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"Build error: {err}", error=True))
        finally:
            def finish():
                self.gba_build_btn.set_sensitive(bool(self.gba_batch_items))
                have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
                missing_art = sum(1 for it in self.gba_batch_items if it.needs_assets)
                self.gba_fetch_btn.set_sensitive(have_key and missing_art > 0)
                self.progress_bar.set_visible(False)
            GLib.idle_add(finish)

    # =============================================================================
    # TEMPLATE HANDLING
    # =============================================================================

    def _on_template_button_toggled(self, button: Gtk.ToggleButton, template_key: str) -> None:
        if self._template_toggle_updating:
            return
        if not button.get_active():
            if self.current_template_key == template_key:
                self._template_toggle_updating = True
                button.set_active(True)
                self._template_toggle_updating = False
            return
        if self.current_template_key == template_key:
            return

        self._template_toggle_updating = True
        self.current_template_key = template_key
        self.template_dir = self._get_template_path(self.current_template_key)
        self.template_row.set_subtitle(TEMPLATES[self.current_template_key]["description"])
        for key, btn in self.template_buttons.items():
            if key != template_key:
                btn.set_active(False)
        self._template_toggle_updating = False
        self._warn_template_issues(self.current_template_key, self.template_dir, "Template")
        # Re-render batch items to update footer previews with new template
        if hasattr(self, "batch_listbox") and self.gba_batch_items:
            self._render_gba_batch_items()

    def _update_docker_status(self):
        if self.docker_status == 'ready':
            self.docker_row.set_subtitle("Ready")
            self.docker_build_btn.set_sensitive(False)
            self.docker_rebuild_btn.set_sensitive(True)
        elif self.docker_status == 'no_image':
            self.docker_row.set_subtitle("Image not built")
            self.docker_build_btn.set_sensitive(True)
            self.docker_rebuild_btn.set_sensitive(False)
        elif self.docker_status == 'no_access':
            self.docker_row.set_subtitle("Docker permission denied (check docker group)")
            self.docker_build_btn.set_sensitive(False)
            self.docker_rebuild_btn.set_sensitive(False)
        else:
            self.docker_row.set_subtitle("Docker not found")
            self.docker_build_btn.set_sensitive(True)
            self.docker_rebuild_btn.set_sensitive(False)

    def on_browse_output(self, button):
        def on_selected(path):
            self.output_path = Path(path)
            safe_path = GLib.markup_escape_text(str(self.output_path))
            self.output_row.set_subtitle(safe_path)
        pick_folder_zenity_async("Select Output Folder", callback=on_selected)

    # =============================================================================
    # LABEL PREVIEW GENERATION
    # =============================================================================

    def _generate_label_preview(self, image_path: str, fit_mode: str) -> str | None:
        """Generate a preview image with the specified fit mode applied."""
        try:
            from PIL import Image

            # Create cache key
            key = f"{image_path}\n{fit_mode}".encode("utf-8")
            digest = hashlib.sha1(key).hexdigest()[:12]
            out_dir = Path(tempfile.gettempdir()) / "mgba_forwarder_previews"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"label_{digest}.png"

            if out_path.exists():
                return str(out_path)

            img = Image.open(image_path).convert("RGBA")
            size = 128

            if fit_mode == "fill":
                # Cover mode - scale and crop center
                img_ratio = img.width / img.height
                if img_ratio > 1:
                    new_h = size
                    new_w = int(img.width * (size / img.height))
                else:
                    new_w = size
                    new_h = int(img.height * (size / img.width))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - size) // 2
                top = (new_h - size) // 2
                img = img.crop((left, top, left + size, top + size))
            elif fit_mode == "stretch":
                # Stretch to exact size
                img = img.resize((size, size), Image.Resampling.LANCZOS)
            else:  # fit
                # Fit within bounds, center on background
                img_ratio = img.width / img.height
                if img_ratio > 1:
                    new_w = size
                    new_h = max(1, int(size / img_ratio))
                else:
                    new_h = size
                    new_w = max(1, int(size * img_ratio))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (size, size), (50, 50, 70, 255))
                x = (size - new_w) // 2
                y = (size - new_h) // 2
                canvas.paste(img, (x, y), img)
                img = canvas

            img.save(str(out_path))
            return str(out_path)
        except Exception as e:
            logging.warning(f"Failed to generate label preview for {image_path}: {e}")
            return None

    def _extract_nds_icon(self, rom_path: str) -> str | None:
        """Extract icon from NDS ROM and cache it as a PNG file."""
        try:
            from generator.bannergif import bannergif

            # Create cache key from ROM path
            key = rom_path.encode("utf-8")
            digest = hashlib.sha1(key).hexdigest()[:12]
            out_dir = Path(tempfile.gettempdir()) / "mgba_forwarder_previews"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"nds_icon_{digest}.png"

            if out_path.exists():
                return str(out_path)

            # Extract icon using bannergif
            icon_img = bannergif(rom_path)
            if icon_img is None or icon_img == -1:
                return None

            # Scale up from 32x32 to 48x48 for better display
            icon_img = icon_img.convert("RGBA")
            icon_img = icon_img.resize((48, 48), resample=0)  # Nearest neighbor for pixel art
            icon_img.save(str(out_path))
            return str(out_path)
        except Exception as e:
            logging.warning(f"Failed to extract NDS icon from {rom_path}: {e}")
            return None

    # =========================================================================
    # SHARED UI COMPONENT HELPERS
    # =========================================================================

    def _create_preview_section(
        self,
        item: "BatchItem",
        rom_name: str,
        icon_path: str | None,
        icon_label: str = "Icon",
    ) -> tuple["Gtk.Box", "Gtk.Picture"]:
        """
        Create a preview section with icon, label, and footer previews.

        Returns: (preview_box, footer_preview) - footer_preview for live updates
        """
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        preview_box.set_halign(Gtk.Align.CENTER)
        preview_box.set_margin_top(8)
        preview_box.set_margin_bottom(8)

        # Icon preview (48x48)
        icon_frame = Gtk.Frame()
        icon_frame.set_size_request(48, 48)
        icon_preview = Gtk.Picture()
        icon_preview.set_size_request(48, 48)
        icon_preview.set_can_shrink(True)
        icon_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._set_picture_from_file(icon_preview, icon_path, 48, 48)
        icon_frame.set_child(icon_preview)

        icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        icon_box.append(icon_frame)
        icon_lbl = Gtk.Label(label=icon_label)
        icon_lbl.add_css_class("dim-label")
        icon_lbl.add_css_class("caption")
        icon_box.append(icon_lbl)
        preview_box.append(icon_box)

        # Label preview (128x128 with fit mode)
        label_frame = Gtk.Frame()
        label_frame.set_size_request(128, 128)
        label_preview = Gtk.Picture()
        label_preview.set_size_request(128, 128)
        label_preview.set_can_shrink(True)
        label_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        if item.label_file:
            label_preview_path = self._generate_label_preview(item.label_file, item.fit_mode)
        else:
            label_preview_path = None
        self._set_picture_from_file(label_preview, label_preview_path, 128, 128)
        label_frame.set_child(label_preview)

        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label_box.append(label_frame)
        label_lbl = Gtk.Label(label="Cartridge Label")
        label_lbl.add_css_class("dim-label")
        label_lbl.add_css_class("caption")
        label_box.append(label_lbl)
        preview_box.append(label_box)

        # Footer preview (256x64)
        footer_frame = Gtk.Frame()
        footer_frame.set_size_request(256, 64)
        footer_preview = Gtk.Picture()
        footer_preview.set_size_request(256, 64)
        footer_preview.set_can_shrink(True)
        footer_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        sub = f"Released: {item.year.strip()}" if item.year and item.year.strip() else ""
        footer_path = self._write_footer_preview_png(
            self.current_template_key, self.template_dir, item.title or rom_name, sub
        )
        self._set_picture_from_file(footer_preview, footer_path, 256, 64)
        footer_frame.set_child(footer_preview)

        footer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        footer_box.append(footer_frame)
        footer_lbl = Gtk.Label(label="Footer Text")
        footer_lbl.add_css_class("dim-label")
        footer_lbl.add_css_class("caption")
        footer_box.append(footer_lbl)
        preview_box.append(footer_box)

        return preview_box, footer_preview

    def _create_label_picker_row(
        self,
        item: "BatchItem",
        batch_items: list,
        render_callback,
        summary_callback,
        scroll_key_attr: str,
        sgdb_fetch_callback=None,
    ) -> "Adw.ActionRow":
        """
        Create a label picker row with Browse, SGDB, Open, Clear buttons.

        Args:
            item: The BatchItem to edit
            batch_items: The list containing the item (for lookup by rom_path)
            render_callback: Function to re-render the batch list
            summary_callback: Function to update the summary
            scroll_key_attr: Attribute name for scroll key (e.g., '_nds_scroll_to_key')
            sgdb_fetch_callback: Optional callback for SGDB fetch (runs in thread)
        """
        label_row = Adw.ActionRow(title="Cartridge Label / Logo")
        label_pic = Gtk.Picture()
        label_pic.set_size_request(64, 64)
        label_pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        label_pic.set_can_shrink(True)
        self._set_picture_from_file(label_pic, item.label_file, 64, 64)

        if item.label_file:
            label_row.set_title("Cartridge Label / Logo [HAS IMAGE]")
            label_row.set_subtitle(item.label_file)
        else:
            label_row.set_title("Cartridge Label / Logo")
            label_row.set_subtitle("Not set (banner will use template label)")

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

        def _set_label(path: str, rom_path=item.rom_path):
            target = next((it for it in batch_items if it.rom_path == rom_path), None)
            if target:
                target.label_file = path
            setattr(self, scroll_key_attr, rom_path)
            render_callback()
            summary_callback()

        def _clear_label(_btn, rom_path=item.rom_path):
            target = next((it for it in batch_items if it.rom_path == rom_path), None)
            if target:
                target.label_file = None
            setattr(self, scroll_key_attr, rom_path)
            render_callback()
            summary_callback()

        def _open_label(_btn, it=item):
            if it.label_file:
                subprocess.Popen(["xdg-open", it.label_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        label_browse.connect(
            "clicked",
            lambda *_, fn=_set_label: pick_file_zenity_async(
                "Select Label/Logo Image",
                [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
                callback=lambda p, f=fn: f(p),
            ),
        )
        label_open.connect("clicked", _open_label)
        label_clear.connect("clicked", _clear_label)

        if sgdb_fetch_callback:
            label_fetch.connect(
                "clicked",
                lambda *_, it=item: Thread(target=sgdb_fetch_callback, args=(it,), daemon=True).start(),
            )

        label_suffix.append(label_browse)
        label_suffix.append(label_fetch)
        label_suffix.append(label_open)
        label_suffix.append(label_clear)
        label_row.add_suffix(label_suffix)
        return label_row

    def _create_fit_mode_selector(
        self,
        item: "BatchItem",
        batch_items: list,
        render_callback,
        scroll_key_attr: str,
        updating_flag_attr: str,
    ) -> "Adw.ActionRow":
        """
        Create a fit mode selector row with Fit/Fill/Stretch toggle buttons.

        Args:
            item: The BatchItem to edit
            batch_items: The list containing the item
            render_callback: Function to re-render the batch list
            scroll_key_attr: Attribute name for scroll key
            updating_flag_attr: Attribute name for the updating flag
        """
        fit_row = Adw.ActionRow(title="Label Fit Mode")
        fit_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        fit_box.set_valign(Gtk.Align.CENTER)

        def _make_fit_handler(rom_path, mode):
            def handler(btn):
                if getattr(self, updating_flag_attr, False):
                    return
                if not btn.get_active():
                    return
                target = next((it for it in batch_items if it.rom_path == rom_path), None)
                if target and target.fit_mode != mode:
                    target.fit_mode = mode
                    setattr(self, updating_flag_attr, True)
                    setattr(self, scroll_key_attr, rom_path)
                    render_callback()
                    setattr(self, updating_flag_attr, False)
            return handler

        fit_buttons = {}
        first_btn = None
        for mode, label in [("fit", "Fit"), ("fill", "Fill"), ("stretch", "Stretch")]:
            btn = Gtk.ToggleButton(label=label)
            btn.connect("toggled", _make_fit_handler(item.rom_path, mode))
            if first_btn is None:
                first_btn = btn
            else:
                btn.set_group(first_btn)
            fit_buttons[mode] = btn
            fit_box.append(btn)

        fit_buttons[item.fit_mode].set_active(True)
        fit_row.add_suffix(fit_box)
        return fit_row

    def _on_gba_filter_toggle(self, switch, param):
        self.gba_show_only_problems = bool(switch.get_active())
        self._render_gba_batch_items()

    def _config_path(self) -> Path:
        return Path.home() / ".config" / "mgba-forwarder-tools" / "config.json"

    def _load_user_config(self) -> None:
        try:
            p = self._config_path()
            if not p.exists():
                return
            cfg = json.loads(p.read_text(encoding="utf-8"))
            self.sgdb_api_key = str(cfg.get("steamgriddb_api_key") or "")
        except Exception as e:
            logging.warning(f"Failed to load config: {e}")
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
            self._render_gba_batch_items()
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
        self._render_gba_batch_items()

    # =============================================================================
    # PICTURE HELPERS
    # =============================================================================

    def _set_picture_from_file(self, picture: Gtk.Picture, path: str | None, width: int, height: int) -> None:
        """Load an image file into a Gtk.Picture widget."""
        try:
            if not path:
                picture.set_paintable(None)
                picture.queue_draw()
                return
            p = Path(path)
            if not p.exists():
                picture.set_paintable(None)
                picture.queue_draw()
                return
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(p), width, height, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            picture.set_paintable(texture)
            picture.queue_draw()
        except Exception:
            picture.set_paintable(None)
            picture.queue_draw()

    def _debounced_preview_update(self, key: str, callback, delay_ms: int = 300):
        """Debounce preview updates to avoid regenerating on every keystroke."""
        # Cancel any pending timer for this key
        if key in self._preview_debounce_timers:
            GLib.source_remove(self._preview_debounce_timers[key])

        # Schedule new update
        def do_update():
            self._preview_debounce_timers.pop(key, None)
            callback()
            return False  # Don't repeat

        self._preview_debounce_timers[key] = GLib.timeout_add(delay_ms, do_update)

    def _write_footer_preview_png(self, template_key: str, template_dir: Path, title: str, subtitle: str) -> str | None:
        """Create a footer preview PNG using a specific template; returns file path."""
        try:
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
                if img is None:
                    return self._write_footer_preview_magick(template_key, template_dir, title, subtitle)
                img.save(str(out_path))
                return str(out_path)

            if template_key == "universal_vc":
                from banner_tools.universal_vc_banner_patcher import UniversalVCBannerPatcher

                patcher = UniversalVCBannerPatcher(str(template_dir))
                img = patcher.create_footer_image(title, subtitle)
                if img is None:
                    return self._write_footer_preview_magick(template_key, template_dir, title, subtitle)
                img.save(str(out_path))
                return str(out_path)

            return self._write_footer_preview_magick(template_key, template_dir, title, subtitle)
        except Exception:
            return self._write_footer_preview_magick(template_key, template_dir, title, subtitle)

    def _simple_footer_preview(self, title: str, subtitle: str) -> str | None:
        """Fallback footer preview using Pillow only."""
        try:
            from PIL import Image, ImageDraw, ImageFont

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

    def _write_footer_preview_magick(
        self, template_key: str, template_dir: Path, title: str, subtitle: str
    ) -> str | None:
        """Render footer preview using ImageMagick (no Pillow)."""
        try:
            footer_w, footer_h = 256, 64
            nsui_dir = template_dir if template_key == "gba_vc" else template_dir.parent / "nsui_template"
            region = nsui_dir / "region_01_USA_EN.cgfx"
            if not region.exists():
                return None

            raw = self._decode_la8_to_raw(region.read_bytes(), 0x1980, footer_w, footer_h)
            out_dir = Path(tempfile.gettempdir()) / "mgba_forwarder_previews"
            out_dir.mkdir(parents=True, exist_ok=True)
            raw_path = out_dir / "footer_raw_rgba.bin"
            base_path = out_dir / "footer_base.png"
            out_path = out_dir / f"footer_magick_{hash((title, subtitle, template_key)) & 0xFFFFFFFF:08x}.png"
            raw_path.write_bytes(raw)

            subprocess.run(
                ["magick", "-size", "256x64", "-depth", "8", f"rgba:{raw_path}", str(base_path)],
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["magick", str(base_path), "-draw", self._magick_gradient_clear_draw(), str(base_path)],
                check=True,
                capture_output=True,
            )

            font_path = nsui_dir / "SCE-PS3-RD-R-LATIN.TTF"
            if not font_path.exists():
                font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

            title = (title or "").strip()
            subtitle = (subtitle or "").strip()
            lines = self._wrap_text_magick(title, str(font_path), 14, 148)
            if len(lines) >= 3:
                subtitle = ""

            annotate = ["magick", str(base_path)]
            if len(lines) == 1:
                if subtitle:
                    x = self._center_text_x_magick(lines[0], str(font_path), 14, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+16", lines[0]]
                    sx = self._center_text_x_magick(subtitle, str(font_path), 12, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "12", "-fill", "rgb(40,40,40)",
                                 "-annotate", f"+{sx}+36", subtitle]
                else:
                    x = self._center_text_x_magick(lines[0], str(font_path), 14, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+24", lines[0]]
            elif len(lines) == 2:
                if subtitle:
                    x1 = self._center_text_x_magick(lines[0], str(font_path), 14, 172)
                    x2 = self._center_text_x_magick(lines[1], str(font_path), 14, 172)
                    sx = self._center_text_x_magick(subtitle, str(font_path), 12, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest",
                                 "-annotate", f"+{x1}+10", lines[0],
                                 "-annotate", f"+{x2}+26", lines[1]]
                    annotate += ["-font", str(font_path), "-pointsize", "12", "-fill", "rgb(40,40,40)",
                                 "-annotate", f"+{sx}+44", subtitle]
                else:
                    x1 = self._center_text_x_magick(lines[0], str(font_path), 14, 172)
                    x2 = self._center_text_x_magick(lines[1], str(font_path), 14, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest",
                                 "-annotate", f"+{x1}+16", lines[0],
                                 "-annotate", f"+{x2}+34", lines[1]]
            else:
                y = 10
                for line in lines[:3]:
                    x = self._center_text_x_magick(line, str(font_path), 14, 172)
                    annotate += ["-font", str(font_path), "-pointsize", "14", "-fill", "rgb(32,32,32)",
                                 "-gravity", "northwest", "-annotate", f"+{x}+{y}", line]
                    y += 17

            annotate.append(str(out_path))
            subprocess.run(annotate, check=True, capture_output=True)
            return str(out_path)
        except Exception:
            return None

    @staticmethod
    def _decode_la8_to_raw(data: bytes, offset: int, width: int, height: int) -> bytes:
        """Decode LA8 morton-tiled texture to raw RGBA bytes (no Pillow)."""
        def morton_index(x, y):
            morton = 0
            for i in range(3):
                morton |= ((x >> i) & 1) << (2 * i)
                morton |= ((y >> i) & 1) << (2 * i + 1)
            return morton

        out = bytearray(width * height * 4)
        tiles_x, tiles_y = width // 8, height // 8
        for ty in range(tiles_y):
            for tx in range(tiles_x):
                tile_off = offset + (ty * tiles_x + tx) * 128
                for py in range(8):
                    for px in range(8):
                        idx = tile_off + morton_index(px, py) * 2
                        if idx + 1 >= len(data):
                            continue
                        a = data[idx]
                        l = data[idx + 1]
                        x = tx * 8 + px
                        y = ty * 8 + py
                        o = (y * width + x) * 4
                        out[o:o + 4] = bytes((l, l, l, a))
        return bytes(out)

    @staticmethod
    def _magick_gradient_clear_draw() -> str:
        parts = []
        for y in range(5, 59):
            progress = max(0.0, min(1.0, (y - 5) / 53.0))
            gray_val = int(255 - progress * (255 - 215))
            left_x = 95
            right_x = 250
            if y <= 6 or y >= 57:
                left_x = 100
                right_x = 245
            elif y <= 8 or y >= 55:
                left_x = 97
                right_x = 248
            parts.append(f"fill rgb({gray_val},{gray_val},{gray_val}) rectangle {left_x},{y} {right_x},{y+1}")
        return " ".join(parts)

    @staticmethod
    def _measure_text_width_magick(text: str, font_path: str, size: int) -> int:
        result = subprocess.run(
            ["magick", "-font", font_path, "-pointsize", str(size), f"label:{text}", "-format", "%w", "info:"],
            capture_output=True,
            text=True,
            check=True,
        )
        return int(result.stdout.strip() or 0)

    def _center_text_x_magick(self, text: str, font_path: str, size: int, center_x: int) -> int:
        w = self._measure_text_width_magick(text, font_path, size)
        return max(0, center_x - w // 2)

    def _wrap_text_magick(self, text: str, font_path: str, size: int, max_w: int) -> list[str]:
        # Support manual line breaks with | character
        lines: list[str] = []
        for segment in text.split("|"):
            segment = segment.strip()
            if not segment:
                continue
            words = segment.split()
            current: list[str] = []
            for word in words:
                test_line = " ".join(current + [word])
                if self._measure_text_width_magick(test_line, font_path, size) <= max_w:
                    current.append(word)
                else:
                    if current:
                        lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
        return lines

    def _build_forwarder_item(
        self,
        item: BatchItem,
        *,
        template_key: str,
        template_dir: Path,
        output_dir: Path,
        default_icon: Path | None = None,
        default_label: Path | None = None,
        progress_cb=None,
        status_cb=None,
    ) -> tuple[bool, Path | None, str | None]:
        """
        Build a single forwarder CIA for the given item.

        Returns (success, output_path, error_message).
        """
        work_dir = Path(tempfile.mkdtemp())
        log_path = Path(tempfile.gettempdir()) / "mgba_forwarder_build.log"
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
            fit_mode = item.fit_mode or "fit"
            if not label_path and default_label and default_label.exists():
                label_path = str(default_label)
            if label_path and Path(label_path).exists():
                cmd.extend(["--cartridge", label_path, "--fit-mode", fit_mode])
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0 or not banner_out.exists():
                err_out = (result.stderr or "") + (result.stdout or "")
                log_path.write_text(err_out or "Banner failed (no output)\n", encoding="utf-8")
                err = (err_out or "Banner failed").strip()[:200]
                return False, None, f"Banner failed for {footer_title}: {err} (see {log_path})"

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
                err_out = (result.stderr or "") + (result.stdout or "")
                log_path.write_text(err_out or "CIA failed (no output)\n", encoding="utf-8")
                err = (err_out or "CIA failed").strip()[-200:]
                return False, None, f"CIA failed for {footer_title}: {err} (see {log_path})"

            safe_name = self._safe_title(footer_title)
            out_path = output_dir / f"{safe_name}.cia"
            shutil.copy2(work_dir / "output.cia", out_path)

            if progress_cb:
                progress_cb(1.0)
            return True, out_path, None
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _update_gba_batch_summary(self) -> None:
        unresolved = sum(1 for it in self.gba_batch_items if it.needs_user_input)
        missing_art = sum(1 for it in self.gba_batch_items if it.needs_assets)
        count = len(self.gba_batch_items)
        if count == 0:
            self.gba_status_row.set_subtitle("No ROMs added yet")
        else:
            self.gba_status_row.set_subtitle(
                f"{count} ROM(s) | {unresolved} need title | {missing_art} missing art"
            )

        if hasattr(self, "gba_fetch_btn"):
            have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
            self.gba_fetch_btn.set_sensitive(have_key and missing_art > 0)

        # Update build button
        if hasattr(self, "gba_build_btn"):
            self.gba_build_btn.set_sensitive(count > 0)
            # Adjust button text based on ROM count
            if count == 1:
                self.gba_build_btn.set_label("Build CIA")
            else:
                self.gba_build_btn.set_label(f"Build All CIAs ({count})")

    def _update_nds_batch_row_style(self, row: "Adw.ExpanderRow", item: BatchItem, rom_name: str) -> None:
        """Update the style and subtitle of an NDS batch row when data changes."""
        title_status = "✓ Title OK" if not item.needs_user_input else "⚠️ Needs review"
        art_status = "✓ Art OK" if not item.needs_assets else "⚠️ No label"
        # Escape special characters for GTK markup
        safe_rom_name = GLib.markup_escape_text(rom_name)
        safe_title = GLib.markup_escape_text(item.title) if item.title else safe_rom_name
        row.set_title(safe_title)
        row.set_subtitle(f"{safe_rom_name} | {title_status} | {art_status}")

        row.remove_css_class("batch-needs-title")
        row.remove_css_class("batch-needs-art")
        row.remove_css_class("batch-complete")
        if item.needs_user_input:
            row.add_css_class("batch-needs-title")
        elif item.needs_assets:
            row.add_css_class("batch-needs-art")
        else:
            row.add_css_class("batch-complete")

    def _update_gba_batch_row_style(self, row: "Adw.ExpanderRow", item: BatchItem, rom_name: str) -> None:
        art_status = "OK" if not item.needs_assets else "Missing"
        title_status = "OK" if not item.needs_user_input else "Needs review"
        # Escape special characters for GTK markup
        safe_rom_name = GLib.markup_escape_text(rom_name)
        safe_title = GLib.markup_escape_text(item.title) if item.title else safe_rom_name
        safe_sd_path = GLib.markup_escape_text(item.sd_path)
        row.set_title(safe_title)
        row.set_subtitle(f"{safe_rom_name} → {safe_sd_path} | Title: {title_status} | Art: {art_status}")

        row.remove_css_class("batch-needs-title")
        row.remove_css_class("batch-needs-art")
        if item.needs_user_input:
            row.add_css_class("batch-needs-title")
        elif item.needs_assets:
            row.add_css_class("batch-needs-art")

    def _render_gba_batch_items(self):
        """Rebuild the batch item list UI."""
        # Remember which rows were expanded so fetches don't collapse them.
        prev_expanded: set[str] = set()
        scroll_to_key = getattr(self, '_batch_scroll_to_key', None)
        row = self.gba_listbox.get_first_child()
        while row is not None:
            nxt = row.get_next_sibling()
            key = getattr(row, "batch_key", None)
            try:
                if key and row.get_expanded():
                    prev_expanded.add(str(key))
            except Exception:
                pass
            self.gba_listbox.remove(row)
            row = nxt
        self._gba_expanded_keys = prev_expanded

        def sort_key(it: BatchItem):
            # Sort alphabetically by title/filename only
            return (it.title or Path(it.rom_path).name).lower()

        for item in sorted(self.gba_batch_items, key=sort_key):
            if self.gba_show_only_problems and not (item.needs_user_input or item.needs_assets):
                continue

            rom_name = Path(item.rom_path).name

            # Build status indicator
            build_status = getattr(item, 'build_status', 'pending')
            if build_status == "building":
                build_indicator = "🔨 Building..."
            elif build_status == "success":
                build_indicator = "✅ Built"
            elif build_status == "failed":
                build_indicator = "❌ Failed"
            else:
                build_indicator = ""

            # Title/art status indicators
            if item.needs_user_input:
                title_status = "⚠️ Needs review"
            else:
                title_status = "✓ Title OK"

            if not item.icon_file and not item.label_file:
                art_status = "⚠️ No art"
            elif not item.icon_file:
                art_status = "⚠️ No icon"
            elif not item.label_file:
                art_status = "⚠️ No label"
            else:
                art_status = "✓ Art OK"

            # Build subtitle with optional build status
            # Escape special characters for GTK markup (e.g., & < >)
            safe_rom_name = GLib.markup_escape_text(rom_name)
            safe_title = GLib.markup_escape_text(item.title) if item.title else safe_rom_name
            subtitle_parts = [safe_rom_name, title_status, art_status]
            if build_indicator:
                subtitle_parts.append(build_indicator)

            expander = Adw.ExpanderRow(
                title=safe_title,
                subtitle=" | ".join(subtitle_parts),
            )
            expander.batch_key = item.rom_path
            if item.rom_path in self._gba_expanded_keys:
                expander.set_expanded(True)

            # Add prefix icon based on status (prioritize build status)
            if build_status == "building":
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
                prefix_icon.add_css_class("accent")
            elif build_status == "success":
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                prefix_icon.add_css_class("success")
            elif build_status == "failed":
                prefix_icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
                prefix_icon.add_css_class("error")
            elif item.needs_user_input or item.needs_assets:
                prefix_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                prefix_icon.add_css_class("warning")
            else:
                prefix_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                prefix_icon.add_css_class("success")
            expander.add_prefix(prefix_icon)

            # Add remove button
            def _remove_rom(_btn, rom_path=item.rom_path):
                self.gba_batch_items = [it for it in self.gba_batch_items if it.rom_path != rom_path]
                self._render_gba_batch_items()
                self._update_gba_batch_summary()

            remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove_btn.add_css_class("flat")
            remove_btn.add_css_class("destructive-action")
            remove_btn.set_tooltip_text("Remove this ROM")
            remove_btn.connect("clicked", _remove_rom)
            expander.add_suffix(remove_btn)

            if item.needs_user_input:
                expander.add_css_class("batch-needs-title")
            elif item.needs_assets:
                expander.add_css_class("batch-needs-art")
            else:
                expander.add_css_class("batch-complete")

            # ROM paths (escape for markup)
            safe_rom_path = GLib.markup_escape_text(item.rom_path)
            safe_sd_path = GLib.markup_escape_text(item.sd_path)
            rom_row = Adw.ActionRow(title="ROM File", subtitle=safe_rom_path)
            sd_row = Adw.ActionRow(title="SD Path", subtitle=safe_sd_path)
            expander.add_row(rom_row)
            expander.add_row(sd_row)

            # Preview section (icon, label, footer) using shared helper
            preview_box, footer_preview = self._create_preview_section(
                item, rom_name, item.icon_file, icon_label="Icon"
            )
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
                self._update_gba_batch_row_style(ex, it, rn)
                self._update_gba_batch_summary()
                # Debounce the expensive preview update
                def update_preview():
                    sub = f"Released: {it.year.strip()}" if it.year.strip() else ""
                    footer_path = self._write_footer_preview_png(
                        self.current_template_key, self.template_dir, it.title or rn, sub
                    )
                    self._set_picture_from_file(footer_pic, footer_path, 256, 64)
                self._debounced_preview_update(f"gba_title_{it.rom_path}", update_preview)

            def _on_year_changed(entry, it=item, rn=rom_name, footer_pic=footer_preview):
                it.year = entry.get_text().strip()
                # Debounce the expensive preview update
                def update_preview():
                    sub = f"Released: {it.year.strip()}" if it.year.strip() else ""
                    footer_path = self._write_footer_preview_png(
                        self.current_template_key, self.template_dir, it.title or rn, sub
                    )
                    self._set_picture_from_file(footer_pic, footer_path, 256, 64)
                self._debounced_preview_update(f"gba_year_{it.rom_path}", update_preview)

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
            else:
                icon_row.set_subtitle("Not set (CIA will use template icon)")
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

            def _set_icon(path: str, rom_path=item.rom_path):
                target = next((it for it in self.gba_batch_items if it.rom_path == rom_path), None)
                if target:
                    target.icon_file = path
                self._gba_scroll_to_key = rom_path
                self._render_gba_batch_items()
                self._update_gba_batch_summary()

            def _clear_icon(_btn, rom_path=item.rom_path):
                target = next((it for it in self.gba_batch_items if it.rom_path == rom_path), None)
                if target:
                    target.icon_file = None
                self._gba_scroll_to_key = rom_path
                self._render_gba_batch_items()
                self._update_gba_batch_summary()

            def _open_icon(_btn, it=item):
                if it.icon_file:
                    subprocess.Popen(["xdg-open", it.icon_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            icon_browse.connect(
                "clicked",
                lambda *_, fn=_set_icon: pick_file_zenity_async(
                    "Select Icon Image",
                    [("Images", ["*.png", "*.jpg", "*.jpeg", "*.webp"])],
                    callback=lambda p, f=fn: f(p),
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

            # Label picker using shared helper
            def _gba_label_fetch(it):
                self._do_fetch_item_art_with_prompt(it, "logo")

            label_row = self._create_label_picker_row(
                item,
                self.gba_batch_items,
                self._render_gba_batch_items,
                self._update_gba_batch_summary,
                "_gba_scroll_to_key",
                sgdb_fetch_callback=_gba_label_fetch,
            )
            expander.add_row(label_row)

            # Fit mode selector using shared helper
            fit_row = self._create_fit_mode_selector(
                item,
                self.gba_batch_items,
                self._render_gba_batch_items,
                "_gba_scroll_to_key",
                "_batch_fit_updating",
            )
            expander.add_row(fit_row)

            self.gba_listbox.append(expander)

            # Track row for scroll-to
            if scroll_to_key and item.rom_path == scroll_to_key:
                # Schedule scroll after layout
                def scroll_to_row(widget=expander):
                    widget.grab_focus()
                    return False
                GLib.idle_add(scroll_to_row)

        self._update_gba_batch_summary()
        self._gba_scroll_to_key = None  # Clear after use

    # =============================================================================
    # STEAMGRIDDB ART FETCHING
    # =============================================================================

    def on_fetch_gba_batch_art(self, button):
        if not self.gba_batch_items:
            self.set_status("Add ROMs first", error=True)
            return
        if not (self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY")):
            self.set_status("Set SteamGridDB API key first", error=True)
            return
        self.gba_fetch_btn.set_sensitive(False)
        self.gba_build_btn.set_sensitive(False)
        self.gba_fetch_progress.set_fraction(0)
        try:
            self.gba_fetch_progress_row.set_visible(True)
        except Exception:
            pass
        self.gba_fetch_progress.set_visible(True)
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
        from PIL import Image

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
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"SteamGridDB fetch failed: {err}", error=True))
        finally:
            GLib.idle_add(self._render_gba_batch_items)

    def _do_fetch_nds_item_art_with_prompt(self, item: BatchItem) -> None:
        """Fetch logo/label art for NDS item (no icon, it comes from ROM)."""
        try:
            from steamgriddb import SteamGridDBClient

            if not item.title:
                return

            client = SteamGridDBClient(api_key=self.sgdb_api_key or None)
            cache_dir = self.output_path / ".sgdb_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Always prompt for game selection
            game_id = self._sgdb_select_game_id(item.title)
            if game_id is None:
                return
            item.sgdb_game_id = game_id

            # Only fetch logo/label (not icon - that comes from ROM for NDS)
            assets = self._prepare_asset_previews(client, self._sgdb_list_assets(client, game_id, "logo"))
            chosen = self._pick_asset_with_preview(assets, item.title, "Logo/Label")
            if chosen:
                item.logo_url = chosen
            elif item.logo_url is None:
                item.logo_url = client.best_logo_url(game_id)

            game_dir = cache_dir / str(game_id)
            game_dir.mkdir(parents=True, exist_ok=True)

            if item.logo_url and item.label_file is None:
                data = client.download(item.logo_url)
                logo_ext = Path(urllib.parse.urlparse(item.logo_url).path).suffix or ".png"
                logo_path = game_dir / f"logo{logo_ext}"
                logo_path.write_bytes(data)
                item.label_file = str(logo_path)

        except Exception as e:
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"SteamGridDB fetch failed: {err}", error=True))
        finally:
            GLib.idle_add(self._render_nds_batch_items)

    # =========================================================================
    # SHARED BATCH ART FETCHING
    # =========================================================================

    def _do_fetch_batch_art_for_items(
        self,
        items: list,
        *,
        fetch_icon: bool = True,
        fetch_logo: bool = True,
        progress_callback,
    ) -> None:
        """
        Shared batch art fetching logic for both GBA and NDS.

        Args:
            items: List of BatchItem to fetch art for
            fetch_icon: Whether to fetch icons (True for GBA, False for NDS)
            fetch_logo: Whether to fetch logos/labels
            progress_callback: Function(fraction, title) to call for progress updates
        """
        from difflib import SequenceMatcher
        from steamgriddb import SteamGridDBClient, SteamGridDBError

        client = SteamGridDBClient(api_key=self.sgdb_api_key or None)
        cache_dir = self.output_path / ".sgdb_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        candidates = [it for it in items if it.title and it.needs_assets]
        total = max(1, len(candidates))

        for idx, item in enumerate(candidates, start=1):
            if not item.title:
                continue

            # Auto-match game using fuzzy matching
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

            # Fetch URLs
            if fetch_icon and item.icon_url is None:
                item.icon_url = client.best_icon_url(game_id)
            if fetch_logo and item.logo_url is None:
                item.logo_url = client.best_logo_url(game_id)

            game_dir = cache_dir / str(game_id)
            game_dir.mkdir(parents=True, exist_ok=True)

            # Download files
            if fetch_icon and item.icon_url and item.icon_file is None:
                data = client.download(item.icon_url)
                icon_ext = Path(urllib.parse.urlparse(item.icon_url).path).suffix or ".png"
                icon_path = game_dir / f"icon{icon_ext}"
                icon_path.write_bytes(data)
                item.icon_file = str(icon_path)

            if fetch_logo and item.logo_url and item.label_file is None:
                data = client.download(item.logo_url)
                logo_ext = Path(urllib.parse.urlparse(item.logo_url).path).suffix or ".png"
                logo_path = game_dir / f"logo{logo_ext}"
                logo_path.write_bytes(data)
                item.label_file = str(logo_path)

            frac = idx / total
            GLib.idle_add(lambda f=frac, t=item.title: progress_callback(f, t))

    def _do_fetch_batch_art(self):
        """Fetch art for GBA ROMs (both icon and logo)."""
        try:
            self._do_fetch_batch_art_for_items(
                self.gba_batch_items,
                fetch_icon=True,
                fetch_logo=True,
                progress_callback=self._set_batch_fetch_progress,
            )
        except Exception as e:
            err_msg = str(e)
            GLib.idle_add(lambda err=err_msg: self.set_status(f"SteamGridDB fetch failed: {err}", error=True))
        finally:
            GLib.idle_add(self._finish_batch_fetch)

    def _set_batch_fetch_progress(self, fraction: float, title: str) -> bool:
        self.gba_fetch_progress.set_fraction(fraction)
        self.set_status(f"Fetching art… {int(fraction*100)}% ({title})")
        return False

    def _finish_batch_fetch(self) -> bool:
        try:
            self.gba_fetch_progress.set_visible(False)
            if hasattr(self, "gba_fetch_progress_row"):
                self.gba_fetch_progress_row.set_visible(False)
        except Exception:
            pass
        self._render_gba_batch_items()
        # Re-enable buttons
        have_key = bool(self.sgdb_api_key or os.environ.get("STEAMGRIDDB_API_KEY"))
        missing_art = sum(1 for it in self.gba_batch_items if it.needs_assets)
        self.gba_fetch_btn.set_sensitive(have_key and missing_art > 0)
        self.gba_build_btn.set_sensitive(bool(self.gba_batch_items))
        self.set_status("[GBA] Art fetch complete")
        return False

    # =============================================================================
    # STATUS & PROGRESS
    # =============================================================================

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
