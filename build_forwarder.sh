#!/usr/bin/env bash
set -e

GAME_NAME="$1"
ROM_PATH="$2"

if [ -z "$GAME_NAME" ] || [ -z "$ROM_PATH" ]; then
    echo "Usage: build_forwarder.sh <game_name> <rom_path>"
    exit 1
fi

FORWARDER_DIR="/opt/forwarder"
MGBA_DIR="$FORWARDER_DIR/mgba"
CUSTOM_DATA="$MGBA_DIR/res/3ds_custom_data"
BUILD_DIR="$MGBA_DIR/build-3ds"
BANNER_TOOLS="/opt/forwarder/banner_tools"

echo "Building forwarder for: $GAME_NAME"
echo "ROM path: $ROM_PATH"

# Clean up custom data directory to avoid cached files from previous builds
rm -rf "$CUSTOM_DATA"
mkdir -p "$CUSTOM_DATA"

# Write ROM path
echo "$ROM_PATH" > "$CUSTOM_DATA/path.txt"

# Process icon - always create fresh with correct game name
ICON_IMAGE="/work/icon.png"
ICON_RESIZED="/tmp/icon_48x48.png"

if [ ! -f "$ICON_IMAGE" ]; then
    # Use default mGBA icon if no custom icon provided
    ICON_IMAGE="$MGBA_DIR/src/platform/3ds/gui-font.png"
    if [ ! -f "$ICON_IMAGE" ]; then
        # Fallback to any existing icon
        ICON_IMAGE="$BUILD_DIR/3ds/mgba.png"
    fi
    echo "No custom icon, using default: $ICON_IMAGE"
else
    echo "Using custom icon: $ICON_IMAGE"
fi

# Resize icon to 48x48 (required by bannertool)
echo "Resizing icon to 48x48..."
python3 -c "
from PIL import Image
img = Image.open('$ICON_IMAGE')
img = img.convert('RGBA')
img = img.resize((48, 48), Image.Resampling.LANCZOS)
img.save('$ICON_RESIZED')
print(f'  Resized {img.size}')
" 2>/dev/null

if [ -f "$ICON_RESIZED" ]; then
    ICON_IMAGE="$ICON_RESIZED"
    echo "  Icon resized successfully"
else
    echo "  Warning: Could not resize icon, using original"
fi

echo "Creating icon with name: $GAME_NAME"
if [ -f "$ICON_IMAGE" ]; then
    bannertool makesmdh \
        -s "$GAME_NAME" \
        -l "$GAME_NAME" \
        -p "mGBA Forwarder" \
        -i "$ICON_IMAGE" \
        -o "$CUSTOM_DATA/icon.icn" \
        --flags visible,ratingrequired,recordusage
    if [ $? -ne 0 ]; then
        echo "Warning: Failed to create custom icon, using default"
        cp "$BUILD_DIR/3ds/mgba.icn" "$CUSTOM_DATA/icon.icn"
    fi
else
    echo "Warning: No icon image found, using default"
    cp "$BUILD_DIR/3ds/mgba.icn" "$CUSTOM_DATA/icon.icn"
fi

# Process banner - check for pre-made .bnr first, then CGFX template, then PNG
if [ -f "/work/banner.bnr" ]; then
    echo "Using pre-made 3D banner from /work/banner.bnr..."
    cp "/work/banner.bnr" "$CUSTOM_DATA/banner.bnr"
    echo "3D banner installed"
elif [ -f "/work/banner.cgfx" ]; then
    echo "Creating 3D banner from CGFX model..."
    
    # Use template audio if available, otherwise create silent audio
    if [ -f "/work/banner.wav" ]; then
        AUDIO_FILE="/work/banner.wav"
    elif [ -f "/opt/forwarder/templates/gba_vc/banner.bcwav" ]; then
        # Convert bcwav to wav if possible, or use as-is
        AUDIO_FILE="/opt/forwarder/templates/gba_vc/banner.bcwav"
        # Create silent wav as fallback
        sox -n -r 22050 -c 2 -b 16 /tmp/banner_audio.wav trim 0.0 2.0 2>/dev/null || true
        if [ -f "/tmp/banner_audio.wav" ]; then
            AUDIO_FILE="/tmp/banner_audio.wav"
        fi
    else
        sox -n -r 22050 -c 2 -b 16 /tmp/banner_audio.wav trim 0.0 2.0 2>/dev/null || true
        AUDIO_FILE="/tmp/banner_audio.wav"
    fi
    
    if [ -f "$AUDIO_FILE" ]; then
        bannertool makebanner \
            -ci "/work/banner.cgfx" \
            -a "$AUDIO_FILE" \
            -o "$CUSTOM_DATA/banner.bnr" \
            || cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
    else
        echo "Warning: No audio file available, using default banner"
        cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
    fi
elif [ -f "/work/banner.png" ]; then
    echo "Creating 2D banner from /work/banner.png..."
    # Create silent audio for banner
    if command -v sox &> /dev/null; then
        sox -n -r 22050 -c 2 -b 16 /tmp/silent.wav trim 0.0 1.0 2>/dev/null || true
    fi
    
    if [ -f "/tmp/silent.wav" ]; then
        bannertool makebanner \
            -i "/work/banner.png" \
            -a "/tmp/silent.wav" \
            -o "$CUSTOM_DATA/banner.bnr" \
            || cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
    else
        # Use default audio from mGBA if available
        if [ -f "$MGBA_DIR/src/platform/3ds/bios.wav" ]; then
            bannertool makebanner \
                -i "/work/banner.png" \
                -a "$MGBA_DIR/src/platform/3ds/bios.wav" \
                -o "$CUSTOM_DATA/banner.bnr" \
                || cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
        else
            cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
        fi
    fi
else
    echo "Using default banner"
    cp "$BUILD_DIR/3ds/mgba.bnr" "$CUSTOM_DATA/banner.bnr"
fi

# Process boot splash / logo
if [ -f "/work/logo.bcma.lz" ]; then
    echo "Using custom boot splash from /work/logo.bcma.lz..."
    cp "/work/logo.bcma.lz" "$CUSTOM_DATA/logo.bcma.lz"
elif [ -f "/work/logo.darc.lz" ]; then
    echo "Using custom boot splash from /work/logo.darc.lz..."
    cp "/work/logo.darc.lz" "$CUSTOM_DATA/logo.bcma.lz"
elif [ -f "/work/splash_top.png" ]; then
    echo "Creating custom boot splash from PNG..."
    if [ -f "$BANNER_TOOLS/boot_splash.py" ]; then
        BOTTOM_ARG=""
        if [ -f "/work/splash_bottom.png" ]; then
            BOTTOM_ARG="-b /work/splash_bottom.png"
        fi
        python3 "$BANNER_TOOLS/boot_splash.py" create \
            -t "/work/splash_top.png" \
            $BOTTOM_ARG \
            -o "$CUSTOM_DATA/logo.bcma.lz" \
            || echo "Warning: Boot splash creation failed, using default"
    fi
elif [ -f "/work/gba_vc_splash" ]; then
    echo "Creating GBA VC style boot splash..."
    if [ -f "$BANNER_TOOLS/boot_splash.py" ]; then
        python3 "$BANNER_TOOLS/boot_splash.py" gba-vc \
            -n "$GAME_NAME" \
            -o "$CUSTOM_DATA/logo.bcma.lz" \
            || echo "Warning: GBA VC splash creation failed"
    fi
fi

echo "Custom data:"
ls -la "$CUSTOM_DATA"

# Copy custom files to build directory (cmake configure_file runs at config time, not build time)
# These files need to be in the build directory for makerom to use them
if [ -f "$CUSTOM_DATA/icon.icn" ]; then
    echo "Copying custom icon to build directory..."
    cp "$CUSTOM_DATA/icon.icn" "$BUILD_DIR/mgba.icn"
fi
if [ -f "$CUSTOM_DATA/banner.bnr" ]; then
    echo "Copying custom banner to build directory..."
    cp "$CUSTOM_DATA/banner.bnr" "$BUILD_DIR/mgba.bnr"
fi

# Clean and rebuild
cd "$BUILD_DIR"
rm -rf install 3ds/*.cia 3ds/*.3dsx 3ds/*.smdh

make -j$(nproc)
make install

# Copy output
if [ -f "$BUILD_DIR/3ds/mgba.cia" ]; then
    cp "$BUILD_DIR/3ds/mgba.cia" "/work/output.cia"
    echo "SUCCESS: Created /work/output.cia"
    ls -la /work/output.cia
else
    echo "ERROR: CIA not generated"
    ls -la "$BUILD_DIR/3ds/"
    exit 1
fi
