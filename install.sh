#!/usr/bin/env bash
# Installs Monitor Control for the current user on Ubuntu.
set -euo pipefail
cd "$(dirname "$0")"

echo "Installing dependencies…"
sudo apt-get update
sudo apt-get install -y ddcutil python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

echo "Enabling I2C access to monitors…"
sudo modprobe i2c-dev
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf >/dev/null
getent group i2c >/dev/null || sudo groupadd i2c
sudo usermod -aG i2c "$USER"

echo "Installing the app…"
install -Dm755 monitor_control.py "$HOME/.local/bin/monitor-control"
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/monitor-control.desktop" <<EOF
[Desktop Entry]
Name=Monitor Control
Comment=Adjust brightness, contrast, color and input on your monitor
Exec=$HOME/.local/bin/monitor-control
Icon=video-display
Terminal=false
Type=Application
Categories=Settings;HardwareSettings;
Keywords=brightness;contrast;monitor;display;ddc;
EOF

echo
echo "Done. Log out and back in once so the i2c group takes effect,"
echo "then open \"Monitor Control\" from your app grid."
