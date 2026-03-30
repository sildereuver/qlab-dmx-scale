#!/bin/bash
# QLab DMX Scale - startup script

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OLA_CONF_DIR="$HOME/.ola"
OLA_ARTNET_CONF="$OLA_CONF_DIR/ola-artnet.conf"

# Stop any running instance first
if pgrep -f "server.py" > /dev/null; then
    echo "Stopping existing server..."
    pkill -f "server.py"
    sleep 1
fi

# Configure OLA Art-Net for loopback if not already set
mkdir -p "$OLA_CONF_DIR"
if [ ! -f "$OLA_ARTNET_CONF" ] || ! grep -q "use_loopback = true" "$OLA_ARTNET_CONF"; then
    echo "Configuring OLA Art-Net for loopback..."
    cat > "$OLA_ARTNET_CONF" << CONF
always_broadcast = false
enabled = true
ip = 127.0.0.1
long_name = OLA - ArtNet node
net = 0
output_ports = 4
short_name = OLA - ArtNet node
subnet = 0
use_limited_broadcast = false
use_loopback = true
CONF
    echo "OLA Art-Net config updated."
fi

# Start OLA if not running
if ! pgrep -x "olad" > /dev/null; then
    echo "Starting OLA..."
    olad -l 1 &
    sleep 2
else
    echo "OLA already running."
fi

# Start the Python server
echo "Starting QLab DMX Scale server..."
python3 "$SCRIPT_DIR/server.py" &
SERVER_PID=$!
sleep 1

# Check if server started successfully
if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "Server failed to start. Check if port 8765 is in use."
    exit 1
fi

# Open browser
open http://localhost:8765

echo ""
echo "QLab DMX Scale is running at http://localhost:8765"
echo "Press Ctrl+C to stop, or use the Quit button in the browser."

# Poll until server stops, then close terminal
while kill -0 $SERVER_PID 2>/dev/null; do
    sleep 1
done

echo "Server stopped."
# Close terminal window
osascript -e 'tell application "Terminal" to close front window' &
exit 0
