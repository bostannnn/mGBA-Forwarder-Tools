# mGBA 3DS Forwarder Creator

A GTK4/libadwaita GUI application for creating GBA Virtual Console style forwarders for Nintendo 3DS.

![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Features

- **3D GBA VC Banner** - Spinning cartridge animation like official Virtual Console titles
- **Custom Cartridge Label** - Your box art on the 3D cartridge (fit mode, no cropping)
- **Smart Text Wrapping** - Long titles auto-wrap, subtitles auto-drop when needed
- **Custom Home Menu Icon** - Any image, auto-resized to 48×48
- **NSUI Template** - Authentic "Virtual Console" branding
- **Native GTK4 UI** - Modern GNOME-style interface

## Screenshots

The app creates forwarders that look like official Nintendo Virtual Console releases, complete with a spinning 3D GBA cartridge banner when you hover over the icon on your 3DS home menu.

## Requirements

- **Linux** (tested on NixOS, Ubuntu, Fedora)
- **Docker** (for CIA building)
- **Python 3.10+**
- **GTK4 & libadwaita**

## Installation

### NixOS

```bash
# One-liner to run (no installation needed)
nix-shell -p python3 python3Packages.pygobject3 python3Packages.pillow gtk4 libadwaita gobject-introspection --run "python3 forwarder_gui.py"
```

### Ubuntu/Debian

```bash
# Install dependencies
sudo apt install python3 python3-gi python3-pil gir1.2-gtk-4.0 gir1.2-adw-1 docker.io

# Add yourself to docker group (log out and back in after)
sudo usermod -aG docker $USER
```

### Fedora

```bash
# Install dependencies
sudo dnf install python3 python3-gobject python3-pillow gtk4 libadwaita docker

# Start and enable Docker
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

### Arch Linux

```bash
# Install dependencies
sudo pacman -S python python-gobject python-pillow gtk4 libadwaita docker

# Start and enable Docker
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

## Building the Docker Image

The Docker image contains all the 3DS homebrew tools needed to build CIA files. You only need to build it once.

### Option 1: Build from GUI

1. Run the app
2. Click the **"Build Image"** button in the Docker section
3. Wait 5-10 minutes for the build to complete

### Option 2: Build from command line

```bash
cd mgba-forwarder-gtk
docker build --network=host -t mgba-forwarder .
```

### Verify the build

```bash
docker images | grep mgba-forwarder
```

You should see:
```
mgba-forwarder   latest   abc123def456   1 minute ago   1.2GB
```

## Usage

### Running the App

```bash
cd mgba-forwarder-gtk
python3 forwarder_gui.py
```

Or on NixOS:
```bash
nix-shell -p python3 python3Packages.pygobject3 python3Packages.pillow gtk4 libadwaita gobject-introspection --run "python3 forwarder_gui.py"
```

### Creating a Forwarder

1. **Game Name** - The title shown on your 3DS home screen
2. **ROM Path on SD** - Where the ROM will be on your 3DS SD card
   - Example: `/roms/gba/Pokemon Emerald.gba`
   - This is the path on the 3DS, not your computer
3. **Icon Image** (optional) - Custom home menu icon (any size, auto-resized to 48×48)
4. **Cartridge Label** (optional) - Box art for the spinning 3D cartridge (any size, auto-resized to 128×128)
5. **Footer Title** - Text shown in the banner footer (auto-filled from game name)
6. **Footer Subtitle** (optional) - Secondary text (auto-hidden if title is too long)

### Output Options

| Button | Output | Requires Docker |
|--------|--------|-----------------|
| **Create CIA Forwarder** | `.cia` file ready to install | Yes |
| **Create Banner Only** | `.bnr` banner file | No |

### Installing on 3DS

1. Copy the generated `.cia` file to your 3DS SD card
2. Copy your ROM file to the path you specified (e.g., `/roms/gba/Pokemon Emerald.gba`)
3. Install the CIA using FBI or another CIA installer
4. Launch from home menu!

## File Structure

```
mgba-forwarder-gtk/
├── forwarder_gui.py      # Main GTK4 application
├── Dockerfile            # Docker build configuration
├── banner_tools/         # Banner generation library
│   └── gba_vc_banner_patcher.py
├── templates/
│   └── gba_vc/
│       └── nsui_template/   # NSUI-extracted banner template
│           ├── banner_common.cgfx
│           ├── banner.bcwav
│           ├── region_*.cgfx
│           └── SCE-PS3-RD-R-LATIN.TTF
└── README.md
```

## Technical Details

### Banner Format
- CWAV audio offset at 0x84 in CBMD header
- 4-byte alignment for all region offsets
- RGB565 Morton-tiled COMMON1 texture (cartridge label, 128×128)
- LA8 Morton-tiled COMMON2 texture (footer, 256×64)
- 13 region CGFX support (USA, EUR, JPN, etc.)
- PS3 font for authentic Nintendo styling

### Docker Image Contents
The Docker image builds and includes:
- **mGBA** - GBA emulator (compiled for 3DS)
- **bannertool** - Creates SMDH icons
- **makerom** - Builds CIA files
- **3dstool** - Manipulates 3DS file formats
- **devkitARM** - 3DS toolchain

## Troubleshooting

### "Docker not installed"
Install Docker for your distribution (see Installation section above).

### "Docker image not built"
Click the "Build Image" button or run `docker build -t mgba-forwarder .`

### Build fails with network error
Make sure you have internet access and try:
```bash
docker build --network=host -t mgba-forwarder .
```

### CIA installs but crashes on 3DS
- Make sure the ROM path matches exactly where you placed the ROM on your SD card
- Paths are case-sensitive on 3DS

### Banner doesn't show custom image
- Make sure your image file exists and is a valid PNG/JPG
- Try a smaller image (under 1MB)

## Credits

- [mGBA](https://mgba.io/) - GBA emulator by endrift
- [NSUI](https://github.com/lordfriky/NSUI) - New Super Ultimate Injector (banner template source)
- [devkitPro](https://devkitpro.org/) - 3DS toolchain
- [Project_CTR](https://github.com/3DSGuy/Project_CTR) - makerom tool

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions welcome! Please open an issue or pull request.
