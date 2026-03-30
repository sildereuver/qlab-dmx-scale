# QLab DMX Scale

Per-channel DMX scale tool between QLab and your Enttec USB DMX interface, via OLA.

QLab sends a value of 100% to channel 1 → you set scale 0.8 → Enttec receives 80%.  
Channels not in the list pass through unchanged (scale factor 1.0).

## Signal flow

```
QLab → Art-Net (broadcast) → OLA (universe 0)
                                    ↓
                            server.py scales per channel
                                    ↓
                            OLA (universe 1) → Enttec → fixtures
```

## Requirements

- macOS 11 Big Sur or higher (Intel or Apple Silicon)
- Python 3.8 or higher (included with macOS via Command Line Tools)
- OLA installed via Homebrew: `brew install ola`  
  If Homebrew is not installed yet, install it first:
  ```
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- No additional Python packages required

## First-time setup

After downloading, make the startup script executable. Open Terminal and follow these steps:

1. Type: `chmod +x ` (with a space at the end, do not press Enter yet)
2. Drag **QLab DMX Scale.command** from Finder into the Terminal window
3. Press Enter

If the command succeeded, Terminal shows a new empty line — no confirmation message is displayed. This only needs to be done once.

## Starting the app

Double-click **QLab DMX Scale.command** in Finder.  
A Terminal window opens, OLA starts automatically, and the browser opens at `http://localhost:8765`.

To stop: click **Quit** in the browser, or press Ctrl+C in the Terminal window. OLA is stopped automatically.

## OLA setup

The startup script automatically configures OLA to use loopback (`ip = 127.0.0.1`, `use_loopback = true`). This means QLab and OLA can communicate on the same machine without a network connection.

Open `http://localhost:9090` and add two universes:

**Universe 0 — input from QLab**
1. Click **Add Universe**
2. Set Universe ID to `0`, Name to `QLab`
3. Under Input Ports, check **ArtNet [127.0.0.1]**
4. Click **Add Universe**

**Universe 1 — output to Enttec**
1. Click **Add Universe**
2. Set Universe ID to `1`, Name to `Enttec`
3. Under Output Ports, check **Enttec USB Pro** (appears when the dongle is connected)
4. Click **Add Universe**

**QLab patch settings**
In QLab, each fixture must be patched to Art-Net with:
- Universe: `0`
- Sub-Net: `0`
- Net: `0`

This is set per fixture in **Workspace Settings → Lighting → Patch → Output**.

**QLab preferences**
Enable **Use broadcast mode for Art-Net** in QLab Preferences.

## Using the app

**Channels**
- Add a channel by entering a DMX channel number and clicking **+ Add channel**
- Set a label and scale factor (0.000 – 2.000) via the slider or by typing
  - 1.0 = no change
  - 0.8 = output is 80% of input
  - 1.25 = output is 125% of input (capped at 255 DMX)
  - 0.0 = channel always off
- The two small numbers below the slider show live **in** (from QLab) and **out** (to Enttec) values

**Importing fixtures from QLab**
1. In QLab: open the **Light Patch Editor** (Window → Light Patch), then choose **Light Patch → Export** from the menu. Make sure **Light Patch** is selected before exporting. Save as `.qlabsettings`
2. In DMX Scale: click **Import QLab patch…** → select the file
3. Fixtures are imported with their DMX channel numbers and labels
4. If you re-import after changing the patch in QLab, existing scale values are preserved (matched by fixture name)

Note: the QLab connection indicator in the toolbar shows whether QLab is running — it checks every 5 seconds via a connection on port 53000.

**Presets**
- **Save / Save as** — saves current settings as JSON in the `presets/` folder next to the app
- **⌘S** — save shortcut
- Click a preset in the sidebar to load it

**OLA panel**
- Shows active universes with input/output ports
- **Open OLA** button opens the OLA web interface at `http://localhost:9090`

## Preset files

Presets are stored in the `presets/` folder inside the QLab DMX Scale folder:

```json
{
  "project": "My Show",
  "venue": "Theatre Name",
  "comments": "Fresnels slightly dimmed, spots unchanged",
  "scales": {
    "1": 0.8,
    "5": 1.0
  },
  "channel_names": {
    "1": "frontlight.intensity",
    "5": "spot.intensity"
  }
}
```

## Tips

- Always launch via **QLab DMX Scale.command** — this ensures OLA is correctly configured
- The scaler runs at ~40fps (every 25ms), sufficient for smooth fades
- Scale factors above 1.0 are useful when QLab outputs at less than full and you want to compensate
- When quitting, OLA is stopped automatically so QLab can take over the Enttec directly

## License

MIT — see [LICENSE](LICENSE)

© Studio Tussenruimte - Sil de Reuver
