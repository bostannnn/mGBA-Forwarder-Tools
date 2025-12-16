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

# Create custom data directory
mkdir -p "$CUSTOM_DATA"

# Write ROM path
echo "$ROM_PATH" > "$CUSTOM_DATA/path.txt"

# Process icon
if [ -f "/work/icon.png" ]; then
    echo "Creating custom icon from /work/icon.png..."
    bannertool makesmdh \
        -s "$GAME_NAME" \
        -l "$GAME_NAME" \
        -p "mGBA Forwarder" \
        -i "/work/icon.png" \
        -o "$CUSTOM_DATA/icon.icn" \
        --flags visible,ratingrequired,recordusage \
        || cp "$BUILD_DIR/3ds/mgba.icn" "$CUSTOM_DATA/icon.icn"
else
    echo "Using default icon"
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
