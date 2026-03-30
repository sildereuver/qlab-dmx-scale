#!/usr/bin/env python3
# QLab DMX Scale — MIT License — github.com/sildereuver/qlab-dmx-scale
"""
QLab DMX Scale - server.py
Reads Art-Net from QLab via OLA (universe 0), scales per channel,
and writes the result back to OLA (universe 1) towards the Enttec interface.
Also serves the web UI at http://localhost:8765
"""

import json
import sys
import threading
import time
import socket
import http.server
import urllib.parse
import urllib.request
import concurrent.futures
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
OLA_HOST        = "127.0.0.1"
OLA_PORT        = 9090
INPUT_UNIVERSE  = 0       # QLab sends here
OUTPUT_UNIVERSE = 1       # OLA forwards this to Enttec
POLL_INTERVAL   = 0.025   # 25ms ≈ 40fps
QLAB_OSC_HOST   = "127.0.0.1"
QLAB_OSC_PORT   = 53000
WEB_PORT        = 8765
PRESETS_DIR     = Path(__file__).parent / "presets"

# ── Shared state ──────────────────────────────────────────────────────────────
state_lock = threading.Lock()
state = {
    "scales":        {},       # {"1": 0.8, ...}  channel (str) → scale factor
    "channel_names": {},       # {"1": "Front", ...}  channel (str) → label
    "project":       "",
    "venue":         "",
    "comments":      "",
    "current_file":  None,
    "qlab_connected": False,
    "fixture_ids":   {},         # {lightOutputID_str: {channels, label}} for update matching
    "notes":         {},         # {ch_str: note text} per channel notes
    "master":        1.0,        # master scale factor
    "master_mode":   "relative",  # "relative" or "absolute"
    "last_settings_file": None,  # filename of last imported .qlabsettings
    "dmx_in":        [0] * 512,
}

# ── OLA helpers ───────────────────────────────────────────────────────────────

def ola_get_dmx(universe):
    try:
        url = f"http://{OLA_HOST}:{OLA_PORT}/get_dmx?u={universe}"
        with urllib.request.urlopen(url, timeout=0.5) as r:
            data = json.loads(r.read())
            return data.get("dmx", [0] * 512)
    except Exception:
        return None


def ola_set_dmx(universe, values):
    try:
        dmx_str = ",".join(str(int(v)) for v in values)
        payload = f"u={universe}&d={dmx_str}".encode()
        req = urllib.request.Request(
            f"http://{OLA_HOST}:{OLA_PORT}/set_dmx",
            data=payload,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=0.5):
            pass
        return True
    except Exception:
        return False


def ola_get_universe_info(universe):
    try:
        url = f"http://{OLA_HOST}:{OLA_PORT}/json/universe_info?id={universe}"
        with urllib.request.urlopen(url, timeout=0.3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def ola_get_all_universes(max_id=32):
    """Scan universe IDs 0..max_id in parallel and return existing ones."""
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(ola_get_universe_info, uid): uid for uid in range(max_id)}
        for future in concurrent.futures.as_completed(futures):
            info = future.result()
            if info and "id" in info:
                results.append(info)
    return sorted(results, key=lambda u: u["id"])

# ── QLab connection check ────────────────────────────────────────────────────

def check_qlab_connected():
    """Check if QLab is running by attempting a TCP connection on port 53000."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex((QLAB_OSC_HOST, QLAB_OSC_PORT))
        sock.close()
        connected = (result == 0)
        with state_lock:
            state["qlab_connected"] = connected
        return connected
    except Exception:
        with state_lock:
            state["qlab_connected"] = False
        return False


def qlab_check_loop():
    """Periodically check QLab connection status."""
    while True:
        check_qlab_connected()
        time.sleep(5)


def parse_qlabsettings(filepath):
    """Parse a QLab .qlabsettings export file.
    Returns {dmx_address_str: label} dict with per-parameter channel mapping.
    Addresses are 1-indexed (QLab stores 0-indexed internally).
    """
    import base64, plistlib
    results = {}

    def _res(uid, no):
        return no[uid.data] if hasattr(uid, "data") else uid

    def _res_str(uid, no):
        v = _res(uid, no)
        return v if isinstance(v, str) else None

    def _res_param(uid, no):
        obj = _res(uid, no)
        if not isinstance(obj, dict): return {}
        result = {}
        for k, v in obj.items():
            if k.startswith("$"): continue
            rv = _res(v, no)
            result[k] = rv
        return result

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if "light" not in entry.get("identifier", ""):
                continue
            state_b64 = entry.get("state", "")
            if not state_b64:
                continue

            raw = base64.b64decode(state_b64)
            sub = plistlib.loads(raw)
            objects = sub["$objects"]

            for obj in objects:
                if not (isinstance(obj, dict) and "NS.data" in obj):
                    continue
                try:
                    nested = plistlib.loads(obj["NS.data"])
                    no = nested["$objects"]

                    # Build definitions: defname -> {param_index: {name, twoBytes}}
                    definitions = {}
                    for nobj in no:
                        if not (isinstance(nobj, dict) and "parameters" in nobj and "name" in nobj):
                            continue
                        dname = _res_str(nobj["name"], no)
                        params_obj = _res(nobj["parameters"], no)
                        if not isinstance(params_obj, dict) or "NS.keys" not in params_obj:
                            continue
                        params = {}
                        for k_uid, v_uid in zip(params_obj["NS.keys"], params_obj["NS.objects"]):
                            idx = _res(k_uid, no)
                            if not isinstance(idx, int) or idx < 0:
                                continue
                            pd = _res_param(v_uid, no)
                            pname = pd.get("name")
                            if hasattr(pname, "data"):
                                pname = _res_str(pname, no)
                            params[idx] = {
                                "name": pname or "",
                                "twoBytes": pd.get("twoBytes", False)
                            }
                        if dname:
                            definitions[dname] = params

                    # Parse instruments
                    for nobj in no:
                        if not (isinstance(nobj, dict) and "address" in nobj and "name" in nobj):
                            continue
                        instr_name = _res_str(nobj["name"], no)
                        def_name   = _res_str(nobj["definitionName"], no)
                        start_addr = nobj["address"] + 1  # QLab stores 0-indexed

                        if not instr_name:
                            continue

                        real = {k: v for k, v in definitions.get(def_name, {}).items() if k >= 0}

                        if not real:
                            results[str(start_addr)] = instr_name
                            continue

                        current = start_addr
                        for idx, param in sorted(real.items()):
                            pname = param["name"]
                            two   = param["twoBytes"]
                            if pname and not pname.startswith("empty"):
                                label = f"{instr_name}.{pname}"
                            else:
                                label = f"{instr_name}.{idx + 1}"
                            results[str(current)] = label
                            current += 2 if two else 1

                except Exception as e:
                    print(f"[QLab] Nested parse error: {e}")
    except Exception as e:
        print(f"[QLab] Settings parse error: {e}")
    return results


def parse_qlabsettings_full(filepath):
    """Like parse_qlabsettings but also returns fixture_ids for update matching.
    Returns (channels_dict, fixture_ids_dict) where:
    - channels: {ch_str: label}
    - fixture_ids: {lightOutputID_str: {channels: [ch_str,...], label: name}}
    """
    import base64, plistlib

    def _res(uid, no):
        return no[uid.data] if hasattr(uid, "data") else uid

    def _res_str(uid, no):
        v = _res(uid, no)
        return v if isinstance(v, str) else None

    def _res_param(uid, no):
        obj = _res(uid, no)
        if not isinstance(obj, dict): return {}
        return {k: _res(v, no) for k, v in obj.items() if not k.startswith("$")}

    channels   = {}
    fixture_ids = {}

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        entries = data if isinstance(data, list) else [data]
        for entry in entries:
            if "light" not in entry.get("identifier", ""):
                continue
            state_b64 = entry.get("state", "")
            if not state_b64:
                continue

            raw = base64.b64decode(state_b64)
            sub = plistlib.loads(raw)
            objects = sub["$objects"]

            for obj in objects:
                if not (isinstance(obj, dict) and "NS.data" in obj):
                    continue
                try:
                    nested = plistlib.loads(obj["NS.data"])
                    no = nested["$objects"]

                    # Build definitions
                    definitions = {}
                    for nobj in no:
                        if not (isinstance(nobj, dict) and "parameters" in nobj and "name" in nobj):
                            continue
                        dname = _res_str(nobj["name"], no)
                        params_obj = _res(nobj["parameters"], no)
                        if not isinstance(params_obj, dict) or "NS.keys" not in params_obj:
                            continue
                        params = {}
                        for k_uid, v_uid in zip(params_obj["NS.keys"], params_obj["NS.objects"]):
                            idx = _res(k_uid, no)
                            if not isinstance(idx, int) or idx < 0:
                                continue
                            pd = _res_param(v_uid, no)
                            pname = pd.get("name")
                            if hasattr(pname, "data"):
                                pname = _res_str(pname, no)
                            params[idx] = {"name": pname or "", "twoBytes": pd.get("twoBytes", False)}
                        if dname:
                            definitions[dname] = params

                    # Parse instruments
                    for nobj in no:
                        if not (isinstance(nobj, dict) and "address" in nobj and "name" in nobj):
                            continue
                        instr_name = _res_str(nobj["name"], no)
                        def_name   = _res_str(nobj["definitionName"], no)
                        start_addr = nobj["address"] + 1
                        if not instr_name:
                            continue

                        real = {k: v for k, v in definitions.get(def_name, {}).items() if k >= 0}
                        fixture_channels = []

                        if not real:
                            channels[str(start_addr)] = instr_name
                            fixture_channels.append(str(start_addr))
                        else:
                            current = start_addr
                            for idx, param in sorted(real.items()):
                                pname = param["name"]
                                two   = param["twoBytes"]
                                label = f"{instr_name}.{pname}" if pname and not pname.startswith("empty") else f"{instr_name}.{idx+1}"
                                channels[str(current)] = label
                                fixture_channels.append(str(current))
                                current += 2 if two else 1

                        # Use instrument name as stable key for matching
                        fixture_ids[instr_name] = {
                            "channels": fixture_channels,
                            "label": instr_name
                        }

                except Exception as e:
                    print(f"[QLab] Nested parse error: {e}")
    except Exception as e:
        print(f"[QLab] Settings parse error: {e}")

    return channels, fixture_ids


# ── DMX scaler loop ───────────────────────────────────────────────────────────

def scaler_loop():
    print("[Scaler] Started")
    while True:
        try:
            dmx_in = ola_get_dmx(INPUT_UNIVERSE)
            if dmx_in is not None:
                with state_lock:
                    scales = state["scales"].copy()
                    state["dmx_in"] = list(dmx_in) + [0] * (512 - len(dmx_in))

                master      = state["master"]
                master_mode = state["master_mode"]
                dmx_out = []
                for i, val in enumerate(dmx_in[:512]):
                    ch = str(i + 1)
                    if master_mode == "absolute":
                        factor = master
                    else:
                        factor = float(scales.get(ch, 1.0)) * master
                    dmx_out.append(min(255, int(val * factor)))

                while len(dmx_out) < 512:
                    dmx_out.append(0)

                ola_set_dmx(OUTPUT_UNIVERSE, dmx_out)
        except Exception as e:
            print(f"[Scaler] Error: {e}")
        time.sleep(POLL_INTERVAL)

# ── Preset save / load ────────────────────────────────────────────────────────

def save_preset(filepath):
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    with state_lock:
        data = {
            "project":       state["project"],
            "venue":         state["venue"],
            "comments":      state["comments"],
            "scales":        state["scales"],
            "channel_names": state["channel_names"],
            "notes":         state["notes"],
            "master":        state["master"],
            "master_mode":   state["master_mode"],
            "fixture_ids":   state["fixture_ids"],
        }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with state_lock:
        state["current_file"] = str(filepath)
    return True


def load_preset(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    with state_lock:
        state["project"]       = data.get("project", "")
        state["venue"]         = data.get("venue", "")
        state["comments"]      = data.get("comments", "")
        state["scales"]        = data.get("scales", {})
        state["channel_names"] = data.get("channel_names", {})
        state["notes"]         = data.get("notes", {})
        state["master"]        = float(data.get("master", 1.0))
        state["master_mode"]   = data.get("master_mode", "relative")
        state["fixture_ids"]   = data.get("fixture_ids", {})
        state["current_file"]  = str(filepath)
    return True


def list_presets(max_recent=None):
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    files = [(p.name, p.stat().st_mtime) for p in PRESETS_DIR.glob("*.json")]
    files.sort(key=lambda x: x[1], reverse=True)  # nieuwste eerst
    if max_recent:
        files = files[:max_recent]
    return [{"name": f[0], "mtime": int(f[1])} for f in files]


def list_all_presets():
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    files = [(p.name, p.stat().st_mtime) for p in PRESETS_DIR.glob("*.json")]
    files.sort(key=lambda x: x[1], reverse=True)
    return [{"name": f[0], "mtime": int(f[1])} for f in files]

# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.send_html(get_html())

        elif path == "/api/state":
            with state_lock:
                s = {
                    "project":        state["project"],
                    "venue":          state["venue"],
                    "comments":       state["comments"],
                    "scales":         state["scales"],
                    "channel_names":  state["channel_names"],
                    "current_file":   state["current_file"],
                    "qlab_connected": state["qlab_connected"],
                    "has_fixture_ids": bool(state["fixture_ids"]),
                    "fixture_ids": state["fixture_ids"],
                    "notes": state["notes"],
                    "master": state["master"],
                    "master_mode": state["master_mode"],
                    "last_settings_file": state["last_settings_file"],
                    "presets":        list_presets(max_recent=5),
                    "dmx_in":         state["dmx_in"],
                }
            self.send_json(200, s)


        elif path == "/api/all_presets":
            self.send_json(200, {"presets": list_all_presets()})

        elif path == "/api/load":
            fname = params.get("file", [None])[0]
            if fname:
                fp = PRESETS_DIR / fname
                if fp.exists():
                    load_preset(fp)
                    self.send_json(200, {"ok": True})
                else:
                    self.send_json(404, {"ok": False, "error": "File not found"})
            else:
                self.send_json(400, {"ok": False, "error": "No filename specified"})

        elif path == "/api/download_preset":
            fname = params.get("file", [None])[0]
            if fname:
                fp = PRESETS_DIR / fname
                if fp.exists():
                    body = fp.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Content-Length", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_json(404, {"error": "Not found"})
            else:
                self.send_json(400, {"error": "No filename"})

        elif path == "/api/ola/universes":
            universes = ola_get_all_universes()
            result = [{
                "id":           u["id"],
                "name":         u.get("name", f"Universe {u['id']}"),
                "input_ports":  u.get("input_ports",  []),
                "output_ports": u.get("output_ports", []),
            } for u in universes]
            self.send_json(200, {"ok": True, "universes": result})



    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self.send_json(400, {"error": "Invalid JSON"})
            return

        path = self.path

        if path == "/api/update":
            with state_lock:
                for key in ("project", "venue", "comments"):
                    if key in data:
                        state[key] = data[key]
                if "scales" in data:
                    state["scales"] = {str(k): float(v) for k, v in data["scales"].items()}
                if "channel_names" in data:
                    state["channel_names"] = {str(k): str(v) for k, v in data["channel_names"].items()}
                if "notes" in data:
                    state["notes"] = {str(k): str(v) for k, v in data["notes"].items()}
                if "master" in data:
                    state["master"] = float(data["master"])
                if "master_mode" in data:
                    state["master_mode"] = str(data["master_mode"])
            self.send_json(200, {"ok": True})

        elif path == "/api/import_json":
            # Direct laden van een JSON preset object
            with state_lock:
                state["project"]       = data.get("project", "")
                state["venue"]         = data.get("venue", "")
                state["comments"]      = data.get("comments", "")
                state["scales"]        = {str(k): float(v) for k, v in data.get("scales", {}).items()}
                state["channel_names"] = {str(k): str(v) for k, v in data.get("channel_names", {}).items()}
                state["notes"]         = {str(k): str(v) for k, v in data.get("notes", {}).items()}
                state["fixture_ids"]   = data.get("fixture_ids", {})
                state["master"]        = float(data.get("master", 1.0))
                state["master_mode"]   = data.get("master_mode", "relative")
            self.send_json(200, {"ok": True})

        elif path == "/api/save":
            fname = data.get("filename")
            if not fname:
                self.send_json(400, {"error": "No filename"})
                return
            if not fname.endswith(".json"):
                fname += ".json"
            save_preset(PRESETS_DIR / fname)
            self.send_json(200, {"ok": True, "file": fname})

        elif path == "/api/import_settings":
            import tempfile, base64 as b64, os
            try:
                payload = json.loads(body)
                mode = payload.get("mode", "import")  # "import" or "update"
                raw = b64.b64decode(payload.get("content", ""))
                with tempfile.NamedTemporaryFile(suffix=".qlabsettings", delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name

                channels, fixture_ids = parse_qlabsettings_full(tmp_path)
                os.unlink(tmp_path)

                if not channels:
                    self.send_json(200, {"ok": False, "error": "No fixtures found in file"})
                    return

                # Store filename for "update" shortcut
                filename = payload.get("filename", "")

                with state_lock:
                    old_fixture_ids = state.get("fixture_ids", {})

                    # For both import and update:
                    # - If lightOutputID is known: move scale by position, remove old channels, update name+address
                    # - If lightOutputID is new: add with scale 1.0
                    # - Channels without a known lightOutputID (import only): add if not existing

                    for fid, new_info in fixture_ids.items():
                        old_info = old_fixture_ids.get(fid)
                        new_channels = new_info["channels"]

                        if old_info:
                            old_channels = old_info["channels"]
                            # Rescue scale values by position before removing
                            old_scales = [state["scales"].pop(ch, 1.0) for ch in old_channels]
                            for ch in old_channels:
                                state["channel_names"].pop(ch, None)
                            # Apply to new channels
                            for i, ch in enumerate(new_channels):
                                state["scales"][ch] = old_scales[i] if i < len(old_scales) else 1.0
                        else:
                            for ch in new_channels:
                                if ch not in state["scales"]:
                                    state["scales"][ch] = 1.0

                        # Always update names
                        for ch in new_channels:
                            if ch in channels:
                                state["channel_names"][ch] = channels[ch]

                    # On import: also add channels that have no fixture_id (edge case)
                    if mode == "import":
                        known_channels = {ch for info in fixture_ids.values() for ch in info["channels"]}
                        for ch, name in channels.items():
                            if ch not in known_channels:
                                if ch not in state["scales"]:
                                    state["scales"][ch] = 1.0
                                state["channel_names"][ch] = name

                    # Store fixture IDs and file content for future updates
                    state["fixture_ids"] = fixture_ids
                    state["last_settings_file"] = filename

                self.send_json(200, {"ok": True, "imported": len(channels), "mode": mode})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})





        elif path == "/api/shutdown":
            self.send_json(200, {"ok": True})
            def _shutdown():
                time.sleep(0.3)
                # Stop OLA via its quit endpoint, fallback to pkill
                try:
                    urllib.request.urlopen(
                        f"http://{OLA_HOST}:{OLA_PORT}/quit", timeout=2.0
                    )
                except Exception:
                    import subprocess
                    subprocess.run(["pkill", "olad"], capture_output=True)
                time.sleep(0.5)
                Handler._server.shutdown()
            threading.Thread(target=_shutdown, daemon=True).start()


        else:
            self.send_json(404, {"error": "Not found"})


def get_html():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>index.html not found</h1>"


def run_server():
    server = http.server.HTTPServer(("", WEB_PORT), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"[Web] http://localhost:{WEB_PORT}")
    Handler._server = server
    server.serve_forever()

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("── QLab DMX Scale ──────────────────────────────")
    print(f"  OLA:     http://{OLA_HOST}:{OLA_PORT}")
    print(f"  Web UI:  http://localhost:{WEB_PORT}")
    print(f"  In:      universe {INPUT_UNIVERSE}  →  Out: universe {OUTPUT_UNIVERSE}")
    print(f"  Presets: {PRESETS_DIR}")
    print("────────────────────────────────────────────────")

    threading.Thread(target=qlab_check_loop, daemon=True).start()
    threading.Thread(target=scaler_loop, daemon=True).start()
    try:
        run_server()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping OLA...")
        try:
            urllib.request.urlopen(
                f"http://{OLA_HOST}:{OLA_PORT}/quit", timeout=2.0
            )
        except Exception:
            import subprocess
            subprocess.run(["pkill", "olad"], capture_output=True)
        print("Done.")
        # Signal the start.command script that we are done
        sys.exit(0)
