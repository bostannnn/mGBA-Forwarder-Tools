#!/usr/bin/env python3
"""
mGBA 3DS Forwarder Creator - GTK4/Adwaita Edition with Docker Support
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib, Gio, GdkPixbuf

import subprocess
import tempfile
import shutil
import os
from pathlib import Path
from threading import Thread


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


class ForwarderWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="mGBA Forwarder Creator")
        self.set_default_size(550, 800)
        
        self.script_dir = Path(__file__).parent.absolute()
        self.template_dir = self.script_dir / "templates" / "gba_vc" / "nsui_template"
        self.banner_tools_dir = self.script_dir / "banner_tools"
        
        self.docker_status = check_docker()
        
        self._setup_ui()
    
    def _setup_ui(self):
        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        # Header bar
        header = Adw.HeaderBar()
        main_box.append(header)
        
        # Scrolled content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        main_box.append(scrolled)
        
        # Content container
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
        
        # Game Name
        self.game_name_row = Adw.EntryRow(title="Game Name")
        self.game_name_row.set_text("")
        basic_group.add(self.game_name_row)
        
        # ROM Path
        self.rom_path_row = Adw.EntryRow(title="ROM Path on SD")
        self.rom_path_row.set_text("/roms/gba/")
        basic_group.add(self.rom_path_row)
        
        # Output folder
        self.output_row = Adw.ActionRow(
            title="Output Folder",
            subtitle=str(Path.home() / "3ds-forwarders")
        )
        self.output_path = Path.home() / "3ds-forwarders"
        
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
            subtitle="48×48 PNG (any size, auto-resized)"
        )
        self.icon_path = None
        
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
            title="3D GBA VC Banner",
            description="Spinning GBA cartridge like official Virtual Console"
        )
        content.append(banner_group)
        
        # Cartridge Label
        self.cartridge_row = Adw.ActionRow(
            title="Cartridge Label",
            subtitle="128×128 box art (fit mode, no cropping)"
        )
        self.cartridge_path = None
        
        cart_clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
        cart_clear_btn.add_css_class("flat")
        cart_clear_btn.connect("clicked", lambda b: self._clear_path("cartridge"))
        
        cart_btn = Gtk.Button(label="Browse", valign=Gtk.Align.CENTER)
        cart_btn.connect("clicked", self.on_browse_cartridge)
        
        self.cartridge_row.add_suffix(cart_clear_btn)
        self.cartridge_row.add_suffix(cart_btn)
        banner_group.add(self.cartridge_row)
        
        # Footer Title
        self.footer_title_row = Adw.EntryRow(title="Footer Title")
        self.footer_title_row.set_text("")
        banner_group.add(self.footer_title_row)
        
        # Footer Subtitle
        self.footer_subtitle_row = Adw.EntryRow(title="Footer Subtitle (optional)")
        self.footer_subtitle_row.set_text("")
        banner_group.add(self.footer_subtitle_row)
        
        # Sync game name to footer title
        self.game_name_row.connect("changed", self._on_game_name_changed)
        
        # Template status
        self.template_row = Adw.ActionRow(title="Template Status")
        self._check_template()
        banner_group.add(self.template_row)
        
        # === DOCKER STATUS ===
        docker_group = Adw.PreferencesGroup(
            title="Docker Build System",
            description="CIA building requires Docker"
        )
        content.append(docker_group)
        
        # Docker status row
        self.docker_row = Adw.ActionRow(title="Docker Status")
        self._update_docker_status()
        
        # Build Docker button
        self.docker_build_btn = Gtk.Button(label="Build Image", valign=Gtk.Align.CENTER)
        self.docker_build_btn.connect("clicked", self.on_build_docker)
        if self.docker_status == 'ready':
            self.docker_build_btn.set_sensitive(False)
        
        self.docker_row.add_suffix(self.docker_build_btn)
        docker_group.add(self.docker_row)
        
        # === BOTTOM SECTION ===
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom_box.set_margin_top(12)
        content.append(bottom_box)
        
        # Progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_visible(False)
        bottom_box.append(self.progress_bar)
        
        # Status label
        self.status_label = Gtk.Label(label="Ready")
        self.status_label.add_css_class("dim-label")
        bottom_box.append(self.status_label)
        
        # Create button
        self.create_btn = Gtk.Button(label="Create CIA Forwarder")
        self.create_btn.add_css_class("suggested-action")
        self.create_btn.add_css_class("pill")
        self.create_btn.connect("clicked", self.on_create)
        bottom_box.append(self.create_btn)
        
        # Banner only button
        self.banner_btn = Gtk.Button(label="Create Banner Only (.bnr)")
        self.banner_btn.add_css_class("pill")
        self.banner_btn.connect("clicked", self.on_create_banner_only)
        bottom_box.append(self.banner_btn)
    
    def _update_docker_status(self):
        if self.docker_status == 'ready':
            self.docker_row.set_subtitle("✓ Docker ready")
        elif self.docker_status == 'no_image':
            self.docker_row.set_subtitle("⚠ Docker found, image not built")
        elif self.docker_status == 'not_found':
            self.docker_row.set_subtitle("✗ Docker not installed")
        else:
            self.docker_row.set_subtitle("✗ Docker error")
    
    def _on_game_name_changed(self, entry):
        current_footer = self.footer_title_row.get_text()
        if not current_footer:
            self.footer_title_row.set_text(entry.get_text())
    
    def _clear_path(self, path_type):
        if path_type == "icon":
            self.icon_path = None
            self.icon_row.set_subtitle("48×48 PNG (any size, auto-resized)")
        elif path_type == "cartridge":
            self.cartridge_path = None
            self.cartridge_row.set_subtitle("128×128 box art (fit mode, no cropping)")
    
    def _check_template(self):
        if self.template_dir.exists():
            required = ['banner_common.cgfx', 'banner.bcwav', 'region_01_USA_EN.cgfx']
            if all((self.template_dir / f).exists() for f in required):
                self.template_row.set_subtitle("✓ NSUI template found")
                return True
        self.template_row.set_subtitle("✗ Template missing")
        return False
    
    def _create_file_filter(self, name, patterns):
        filter = Gtk.FileFilter()
        filter.set_name(name)
        for pattern in patterns:
            filter.add_pattern(pattern)
        return filter
    
    def on_browse_output(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Output Folder")
        dialog.select_folder(self, None, self._on_output_selected)
    
    def _on_output_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                self.output_path = Path(folder.get_path())
                self.output_row.set_subtitle(str(self.output_path))
        except:
            pass
    
    def on_browse_icon(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Icon Image")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(self._create_file_filter("Images", ["*.png", "*.jpg", "*.jpeg", "*.bmp"]))
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_icon_selected)
    
    def _on_icon_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self.icon_path = Path(file.get_path())
                self.icon_row.set_subtitle(self.icon_path.name)
        except:
            pass
    
    def on_browse_cartridge(self, button):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Cartridge Label Image")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(self._create_file_filter("Images", ["*.png", "*.jpg", "*.jpeg", "*.bmp"]))
        dialog.set_filters(filters)
        dialog.open(self, None, self._on_cartridge_selected)
    
    def _on_cartridge_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                self.cartridge_path = Path(file.get_path())
                self.cartridge_row.set_subtitle(self.cartridge_path.name)
        except:
            pass
    
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
        self.set_status("Building Docker image (this may take a few minutes)...")
        self.progress_bar.set_visible(True)
        self.progress_bar.pulse()
        
        Thread(target=self._do_build_docker, daemon=True).start()
    
    def _do_build_docker(self):
        try:
            # Find Dockerfile directory
            dockerfile_dir = self.script_dir
            if not (dockerfile_dir / "Dockerfile").exists():
                self.set_status("Dockerfile not found", error=True)
                GLib.idle_add(lambda: self.docker_build_btn.set_sensitive(True))
                return
            
            result = subprocess.run(
                ['docker', 'build', '--network=host', '-t', 'mgba-forwarder', '.'],
                cwd=str(dockerfile_dir),
                capture_output=True,
                text=True,
                timeout=600
            )
            
            if result.returncode == 0:
                self.docker_status = 'ready'
                self.set_status("✓ Docker image built successfully")
                GLib.idle_add(lambda: self._update_docker_status())
            else:
                self.set_status(f"Docker build failed: {result.stderr[-200:]}", error=True)
                GLib.idle_add(lambda: self.docker_build_btn.set_sensitive(True))
        except subprocess.TimeoutExpired:
            self.set_status("Docker build timed out", error=True)
            GLib.idle_add(lambda: self.docker_build_btn.set_sensitive(True))
        except Exception as e:
            self.set_status(f"Error: {str(e)}", error=True)
            GLib.idle_add(lambda: self.docker_build_btn.set_sensitive(True))
        finally:
            GLib.idle_add(lambda: self.progress_bar.set_visible(False))
    
    def on_create_banner_only(self, button):
        """Create only the banner, no CIA."""
        game_name = self.game_name_row.get_text().strip()
        if not game_name:
            self.set_status("Please enter a game name", error=True)
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
            
            self.output_path.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in game_name)
            
            self.set_status("Creating 3D GBA VC banner...")
            self.set_progress(0.3)
            
            patcher_script = self.script_dir / "banner_tools" / "gba_vc_banner_patcher.py"
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
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            self.set_progress(1.0)
            
            if result.returncode == 0 and output_banner.exists():
                self.set_status(f"✓ Created: {output_banner.name}")
            else:
                self.set_status(f"Banner failed: {result.stderr}", error=True)
                
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
            
            work_dir = Path(tempfile.mkdtemp())
            self.output_path.mkdir(parents=True, exist_ok=True)
            
            safe_name = "".join(c if c.isalnum() or c in '-_ ' else '' for c in game_name)
            
            # Step 1: Create banner
            self.set_status("Creating 3D GBA VC banner...")
            self.set_progress(0.2)
            
            patcher_script = self.script_dir / "banner_tools" / "gba_vc_banner_patcher.py"
            
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
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                self.set_status(f"Banner failed: {result.stderr}", error=True)
                return
            
            self.set_progress(0.4)
            
            # Step 2: Prepare icon if provided
            if self.icon_path and self.icon_path.exists():
                shutil.copy(self.icon_path, work_dir / "icon.png")
            
            # Step 3: Copy banner to work_dir as custom data
            data_dir = work_dir / "custom_data"
            data_dir.mkdir(exist_ok=True)
            if (work_dir / "banner.bnr").exists():
                shutil.copy(work_dir / "banner.bnr", data_dir / "banner.bnr")
            if (work_dir / "icon.png").exists():
                shutil.copy(work_dir / "icon.png", data_dir / "icon.png")
            
            # Step 4: Run Docker build
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
                self.set_status(f"✓ Created: {output_file.name}")
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
