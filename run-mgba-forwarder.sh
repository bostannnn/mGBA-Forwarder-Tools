#!/usr/bin/env bash
set -e
cd /home/bostan/mGBA-Forwarder-Tools-fixed

nix-shell -p python3 python3Packages.pygobject3 python3Packages.pycairo python3Packages.pillow python3Packages.requests gtk4 libadwaita zenity gobject-introspection patchelf glibc gcc --run '
    INTERP=$(patchelf --print-interpreter /bin/sh)
    LIBPATH=$(dirname $(gcc -print-file-name=libstdc++.so))

    patch_binary() {
        local binary="$1"
        if [ -f "$binary" ]; then
            # Check if already patched by looking at the interpreter
            current_interp=$(patchelf --print-interpreter "$binary" 2>/dev/null || echo "")
            if [ "$current_interp" != "$INTERP" ]; then
                echo "Patching $binary for NixOS..."
                patchelf --set-interpreter "$INTERP" --set-rpath "$LIBPATH" "$binary" 2>/dev/null || true
            fi
        fi
    }

    patch_binary generator/bannertool
    patch_binary generator/makerom
    python3 forwarder_gui.py
'
