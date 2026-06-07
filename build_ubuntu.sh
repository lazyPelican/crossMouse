#!/usr/bin/env bash
set -e

echo "=========================================="
echo "  Building Mouse Share Client (Linux)"
echo "=========================================="
echo ""

echo "[1/3] Installing Python packages ..."
pip3 install --break-system-packages pynput evdev pyinstaller

echo ""
echo "[2/3] Setting up /dev/uinput permission ..."
# One-time: allow writing to uinput without sudo
if [ ! -w /dev/uinput ]; then
    echo "  Need sudo to set uinput permissions (one-time setup)"
    sudo chmod 666 /dev/uinput
    # Make it persist across reboots
    echo 'KERNEL=="uinput", MODE="0666"' | sudo tee /etc/udev/rules.d/99-uinput.rules > /dev/null
    sudo udevadm control --reload-rules 2>/dev/null || true
    echo "  Done — /dev/uinput is now accessible"
else
    echo "  Already writable"
fi

echo ""
echo "[3/3] Building executable ..."
python3 -m PyInstaller --onefile --name "MouseShareClient" \
    --hidden-import=pynput.keyboard._xorg \
    --hidden-import=pynput.mouse._xorg \
    --hidden-import=evdev \
    client_gui.py

echo ""
echo "=========================================="
echo "  BUILD COMPLETE!"
echo "  Binary:  dist/MouseShareClient"
echo ""
echo "  Run:  ./dist/MouseShareClient"
echo "=========================================="
