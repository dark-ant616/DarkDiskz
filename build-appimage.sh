#!/bin/bash
set -e

echo "Building DarkDiskz AppImage using linuxdeploy..."

# 1. Check for linuxdeploy
if ! command -v linuxdeploy &> /dev/null; then
    echo "Installing linuxdeploy..."
    wget -c "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
    chmod +x linuxdeploy-x86_64.AppImage
    sudo mv linuxdeploy-x86_64.AppImage /usr/local/bin/linuxdeploy
fi

# 2. Create AppDir structure
APPDIR="DarkDiskz.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# 3. Copy files
cp main.py "$APPDIR/usr/bin/"
cp -r icons "$APPDIR/usr/bin/" 2>/dev/null || true
cp hamster.png "$APPDIR/usr/bin/" 2>/dev/null || true
cp hamster.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/hamster.png" 2>/dev/null || true

# 4. Create simple launcher script
cat > "$APPDIR/usr/bin/darkdiskz" <<'EOF'
#!/bin/bash
cd "$(dirname "$0")"
python3 main.py
EOF
chmod +x "$APPDIR/usr/bin/darkdiskz"

# 5. Create .desktop file
cat > "$APPDIR/usr/share/applications/DarkDiskz.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DarkDiskz
Exec=darkdiskz
Icon=hamster
Categories=Utility;
EOF

# 6. Create AppRun script
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}"/usr/bin/:"${PATH}"
export LD_LIBRARY_PATH="${HERE}"/usr/lib/:"${LD_LIBRARY_PATH}"
exec "${HERE}"/usr/bin/darkdiskz "$@"
EOF
chmod +x "$APPDIR/AppRun"

# 7. Build the AppImage using linuxdeploy
echo "Building AppImage..."
ARCH=x86_64 linuxdeploy --appdir "$APPDIR" --output appimage

echo "Done! Look for DarkDiskz-x86_64.AppImage"
