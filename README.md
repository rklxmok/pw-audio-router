# PipeWire Audio Router

A lightweight Qt system tray app for routing application audio between PipeWire nodes. Useful for screen sharing audio in Element/WebRTC, feeding music into game voice chat, OBS audio routing, and more.

## Requirements

- Linux with PipeWire
- Python 3
- PyQt6
- PipeWire CLI tools (`pw-cli`, `pw-link`)

## Install

### Arch Linux / Garuda

```bash
sudo pacman -S python-pyqt6 pipewire
```

### Other distros

```bash
pip install PyQt6
```

Ensure `pw-cli` and `pw-link` are available (usually included with PipeWire).

## Usage

### Run manually

```bash
python3 pipewire-audio-router.py
```

### Install to applications menu & autostart

```bash
cp pw-audio-router.desktop ~/.local/share/applications/
cp pw-audio-router.desktop ~/.config/autostart/
```

### How it works

1. Right-click the tray icon (1/4" jack)
2. **Route Audio** - select the source app (e.g. Firefox, Spotify)
3. **Route To** - select the destination (e.g. browser WebRTC input, virtual sink)
4. Audio is routed immediately
5. Active routes are shown in the menu and can be individually removed
6. **Stop All Routes** clears everything
7. **Quit** cleans up all routes and exits

## Example use cases

- Share application audio during a screen share in Element Desktop
- Play music through a game's voice chat
- Route audio into OBS for streaming
- Any app-to-app audio routing via PipeWire
