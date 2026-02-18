#!/bin/bash
# Installs PipeWire Audio Router to the applications menu and autostart.
# Run once after cloning/moving the repo to its final location.

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_CONTENT="[Desktop Entry]
Type=Application
Name=PipeWire Audio Router
Comment=Route application audio for screen sharing via PipeWire
Exec=bash $INSTALL_DIR/run.sh
Icon=audio-volume-high
Terminal=false
Categories=AudioVideo;Audio;Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true"

mkdir -p "$HOME/.config/autostart" "$HOME/.local/share/applications"
echo "$DESKTOP_CONTENT" > "$HOME/.config/autostart/pw-audio-router.desktop"
echo "$DESKTOP_CONTENT" > "$HOME/.local/share/applications/pw-audio-router.desktop"

chmod +x "$INSTALL_DIR/run.sh"

echo "Installed to:"
echo "  $HOME/.config/autostart/pw-audio-router.desktop"
echo "  $HOME/.local/share/applications/pw-audio-router.desktop"
