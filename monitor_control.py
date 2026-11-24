#!/usr/bin/env python3
"""
Monitor Control for Ubuntu

A small GTK4 / libadwaita front end for ddcutil (DDC/CI), in the spirit of the
Windows and macOS apps monitor makers ship (LG OnScreen Control, Dell Display
Manager and the like): brightness, contrast, sharpness, color temperature,
RGB gain, input source, volume, and saved profiles. Works with any brand.
The refresh rate is set through GNOME's display configuration service
(Mutter), since DDC/CI can't change it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk
except (ImportError, ValueError):
    sys.exit(
        "GTK 4 / libadwaita bindings are missing.\n"
        "Install them with: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
    )

APP_ID = "io.github.monitorcontrol"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "monitor-control")
PROFILES_FILE = os.path.join(CONFIG_DIR, "profiles.json")

# EDID manufacturer IDs (PNP IDs) of common monitor brands
MANUFACTURERS = {
    "ACI": "ASUS", "AUS": "ASUS", "ACR": "Acer", "AOC": "AOC", "BNQ": "BenQ",
    "DEL": "Dell", "ENC": "EIZO", "GBT": "Gigabyte", "GSM": "LG", "HPN": "HP",
    "HWP": "HP", "IVM": "iiyama", "LEN": "Lenovo", "MSI": "MSI", "NEC": "NEC",
    "PHL": "Philips", "SAM": "Samsung", "SHP": "Sharp", "SNY": "Sony",
    "VSC": "ViewSonic", "XMI": "Xiaomi",
}

# MCCS VCP feature codes
BRIGHTNESS, CONTRAST, SHARPNESS = 0x10, 0x12, 0x87
COLOR_PRESET, RED_GAIN, GREEN_GAIN, BLUE_GAIN = 0x14, 0x16, 0x18, 0x1A
INPUT_SOURCE, VOLUME = 0x60, 0x62

SECTIONS = [
    ("Picture", [
        (BRIGHTNESS, "Brightness", "slider"),
        (CONTRAST, "Contrast", "slider"),
        (SHARPNESS, "Sharpness", "slider"),
    ]),
    ("Color", [
        (COLOR_PRESET, "Color temperature", "choice"),
        (RED_GAIN, "Red", "slider"),
        (GREEN_GAIN, "Green", "slider"),
        (BLUE_GAIN, "Blue", "slider"),
    ]),
    ("Input and audio", [
        (INPUT_SOURCE, "Input", "choice"),
        (VOLUME, "Volume", "slider"),
    ]),
]
ALL_CODES = [code for _, items in SECTIONS for code, _, _ in items]
ALWAYS_TRY = (BRIGHTNESS, CONTRAST)
PROFILE_CODES = [BRIGHTNESS, CONTRAST, SHARPNESS, RED_GAIN, GREEN_GAIN, BLUE_GAIN]
SHORT_NAMES = {BRIGHTNESS: "Brightness", CONTRAST: "Contrast", SHARPNESS: "Sharpness",
               RED_GAIN: "R", GREEN_GAIN: "G", BLUE_GAIN: "B"}

# Used when the monitor's capability string doesn't name the options
FALLBACK_LABELS = {
    COLOR_PRESET: {0x01: "sRGB", 0x04: "5000 K", 0x05: "6500 K", 0x06: "7500 K",
                   0x08: "9300 K", 0x0B: "User 1", 0x0C: "User 2"},
    INPUT_SOURCE: {0x01: "VGA", 0x03: "DVI", 0x0F: "DisplayPort 1", 0x10: "DisplayPort 2",
                   0x11: "HDMI 1", 0x12: "HDMI 2", 0x1B: "USB-C"},
}


# --------------------------------------------------------------------------
# ddcutil wrapper
# --------------------------------------------------------------------------
_ddc_lock = threading.Lock()  # the I2C bus doesn't like parallel requests


def ddcutil(*args, timeout=20):
    """Run ddcutil and return (returncode, stdout, stderr)."""
    with _ddc_lock:
        try:
            r = subprocess.run(["ddcutil", *args], capture_output=True,
                               text=True, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "ddcutil timed out"
        except FileNotFoundError:
            return 127, "", "ddcutil not found"


def monitor_name(mfg_id, model):
    """Build a readable name like "Dell U2720Q" from an EDID maker ID and model."""
    brand = MANUFACTURERS.get(mfg_id.upper(), mfg_id)
    if not model:
        return brand
    if model.upper().startswith(brand.upper()):  # "DELL U2720Q", "LG FULL HD"
        return model
    first, _, rest = model.partition(" ")
    if first.upper() == mfg_id.upper() and rest:  # "LEN T27h-20" -> "Lenovo T27h-20"
        return f"{brand} {rest}"
    return f"{brand} {model}"


def parse_detect(out):
    displays, cur = [], None
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():
            m = re.match(r"Display\s+(\d+)", line)
            cur = {"num": m.group(1), "bus": None, "name": f"Display {m.group(1)}",
                   "edid": None, "connector": None} if m else None
            if cur:
                displays.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("I2C bus:"):
            m = re.search(r"i2c-(\d+)", s)
            if m:
                cur["bus"] = m.group(1)
        elif s.startswith(("DRM connector:", "DRM_connector:")):
            # "card1-HDMI-A-1" -> "HDMI-1", the name GNOME uses
            conn = re.sub(r"^card\d+-", "", s.split(":", 1)[1].strip())
            cur["connector"] = re.sub(r"^HDMI-A-", "HDMI-", conn)
        elif s.startswith("Monitor:"):
            parts = [p.strip() for p in s.split(":", 1)[1].split(":")]
            cur["edid"] = tuple((parts + ["", "", ""])[:3])
            model = parts[1] if len(parts) > 1 else ""
            cur["name"] = monitor_name(parts[0], model)
    displays = [d for d in displays if d["bus"]]
    for d in displays:
        d["label"] = f"{d['name']} (display {d['num']})" if len(displays) > 1 else d["name"]
    return displays


def detect_displays():
    _, out, _ = ddcutil("detect", "--brief")
    return parse_detect(out)


def parse_capabilities(out):
    """Return {code: {"name": str, "values": {int: str}}}."""
    feats, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^\s*Feature:\s*([0-9A-Fa-f]{2})\s*\((.*)\)", line)
        if m:
            cur = int(m.group(1), 16)
            feats[cur] = {"name": m.group(2), "values": {}}
            continue
        if cur is not None:
            m = re.match(r"^\s+([0-9A-Fa-f]{2}):\s*(.+)$", line)
            if m:
                feats[cur]["values"][int(m.group(1), 16)] = m.group(2).strip()
    return feats


def read_capabilities(bus):
    _, out, _ = ddcutil("--bus", bus, "capabilities", timeout=30)
    return parse_capabilities(out)


def parse_getvcp(out):
    """Return (current, max) for continuous features, (value, None) for others."""
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 3 and p[0] == "VCP":
            if p[2] == "C" and len(p) >= 5:
                return int(p[3]), int(p[4])
            if p[2] in ("SNC", "CNC") and len(p) >= 4:
                return int(p[3].lstrip("x"), 16), None
            return None
    return None


def getvcp(bus, code, attempts=3):
    # DDC reads fail now and then (especially right after a capabilities read), so retry
    for i in range(attempts):
        _, out, _ = ddcutil("--bus", bus, "getvcp", f"{code:02x}", "--brief")
        result = parse_getvcp(out)
        if result is not None:
            return result
        time.sleep(0.2 * (i + 1))
    return None


def setvcp(bus, code, value):
    rc, _, err = ddcutil("--bus", bus, "--noverify", "setvcp", f"{code:02x}", str(int(value)))
    return rc == 0, err.strip()


# --------------------------------------------------------------------------
# Refresh rate, through GNOME's display configuration service (Mutter)
# --------------------------------------------------------------------------
MUTTER = ("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
          "org.gnome.Mutter.DisplayConfig")
APPLY_TEMPORARY, APPLY_PERSISTENT = 1, 2
CONFIRM_SECONDS = 15


def mutter_call(method, params=None):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    return bus.call_sync(*MUTTER, method, params, None, Gio.DBusCallFlags.NONE, 10000, None)


def mutter_state():
    """Return (serial, monitors, logical_monitors, properties), or None outside GNOME."""
    try:
        return mutter_call("GetCurrentState").unpack()
    except GLib.Error:
        return None


def find_mutter_monitor(monitors, display):
    norm = lambda t: tuple((x or "").strip().lower() for x in t)  # noqa: E731
    by_conn = [m for m in monitors if display.get("connector") and m[0][0] == display["connector"]]
    by_edid = [m for m in monitors if display.get("edid") and norm(m[0][1:]) == norm(display["edid"])]
    # Prefer a monitor that matches on both, in case two identical models are connected
    for m in by_conn:
        if m in by_edid:
            return m
    return (by_edid or by_conn or [None])[0]


def format_hz(rate):
    return f"{rate:.2f}".rstrip("0").rstrip(".") + " Hz"


def read_refresh_rates(display):
    """Return {"connector", "options": [(mode_id, label)], "current": mode_id}, or None.

    Only modes at the current resolution are offered, so changing the refresh
    rate never changes the resolution or the scale.
    """
    state = mutter_state()
    if not state:
        return None
    _, monitors, logical, _ = state
    mon = find_mutter_monitor(monitors, display)
    if not mon:
        return None
    conn, modes = mon[0][0], mon[1]
    cur = next((m for m in modes if m[6].get("is-current")), None)
    if not cur:
        return None  # the monitor is connected but turned off
    scale = next((lm[2] for lm in logical if any(c[0] == conn for c in lm[5])), None)
    vrr = cur[6].get("refresh-rate-mode", "fixed")
    rates = []
    for mode_id, w, h, rate, _pref, scales, props in modes:
        if (w, h) != (cur[1], cur[2]) or props.get("is-interlaced"):
            continue
        if props.get("refresh-rate-mode", "fixed") != vrr:
            continue
        if scale is not None and not any(abs(s - scale) < 0.01 for s in scales):
            continue
        rates.append((rate, mode_id))
    options, seen = [], set()
    for rate, mode_id in sorted(rates, reverse=True):
        label = format_hz(rate)
        if label not in seen or mode_id == cur[0]:
            seen.add(label)
            options.append((mode_id, label))
    return {"connector": conn, "options": options, "current": cur[0]}


def apply_display_mode(connector, mode_id, method):
    """Switch one monitor to mode_id, keeping everything else as it is."""
    state = mutter_state()
    if not state:
        raise RuntimeError("GNOME's display settings service isn't available")
    serial, monitors, logical, props = state
    current = {m[0][0]: next((md[0] for md in m[1] if md[6].get("is-current")), None)
               for m in monitors}
    mon_props = {m[0][0]: m[2] for m in monitors}
    config = []
    for x, y, scale, transform, primary, mons, _ in logical:
        out = []
        for conn, *_ in mons:
            p, mp = {}, mon_props.get(conn, {})
            if "is-underscanning" in mp:
                p["underscanning"] = GLib.Variant("b", mp["is-underscanning"])
            if "color-mode" in mp:
                p["color-mode"] = GLib.Variant("u", mp["color-mode"])
            out.append((conn, mode_id if conn == connector else current[conn], p))
        config.append((x, y, scale, transform, primary, out))
    global_props = {}
    if props.get("supports-changing-layout-mode") and "layout-mode" in props:
        global_props["layout-mode"] = GLib.Variant("u", props["layout-mode"])
    mutter_call("ApplyMonitorsConfig", GLib.Variant(
        "(uua(iiduba(ssa{sv}))a{sv})", (serial, method, config, global_props)))


def read_monitor_state(display):
    bus = display["bus"]
    caps = read_capabilities(bus)
    state = {}
    for code in ALL_CODES:
        # Some monitors (several LG models among them) leave brightness/contrast out
        # of their capability list even though they support them, so always try those two.
        if caps and code not in caps and code not in ALWAYS_TRY:
            continue
        v = getvcp(bus, code)
        if v is not None:
            state[code] = v
        elif code in ALWAYS_TRY:
            state[code] = (None, 100)  # can't read it, but writing usually still works
    try:
        rates = read_refresh_rates(display)
    except Exception:  # noqa: BLE001
        rates = None
    return caps, state, rates


class Writer:
    """Background writer that collapses rapid slider moves into one write."""

    def __init__(self, on_error):
        self.pending = {}
        self.cv = threading.Condition()
        self.on_error = on_error
        threading.Thread(target=self._loop, daemon=True).start()

    def set(self, bus, code, value):
        with self.cv:
            self.pending[(bus, code)] = value
            self.cv.notify()

    def _loop(self):
        while True:
            with self.cv:
                while not self.pending:
                    self.cv.wait()
                (bus, code), value = self.pending.popitem()
            ok, err = setvcp(bus, code, value)
            if not ok:
                GLib.idle_add(self.on_error, err or "The monitor didn't accept the change.")


def run_bg(fn, callback, *args):
    def work():
        try:
            result = fn(*args)
        except Exception as e:  # noqa: BLE001
            result = e
        GLib.idle_add(callback, result)
    threading.Thread(target=work, daemon=True).start()


def load_profiles():
    try:
        with open(PROFILES_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_profiles(profiles):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Monitor Control")
        self.set_default_size(560, 760)
        self.displays, self.current = [], None
        self.sliders, self.loading, self.sync_all = {}, False, False
        self.profiles = load_profiles()
        self.writer = Writer(self.on_write_error)
        self.rate_row, self.rate_info, self.rate_pending = None, None, False
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).signal_subscribe(
                MUTTER[0], MUTTER[2], "MonitorsChanged", MUTTER[1], None,
                Gio.DBusSignalFlags.NONE, lambda *_: self.reload_refresh_rates())
        except GLib.Error:
            pass

        self.toasts = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Detect monitors again")
        refresh.connect("clicked", lambda *_: self.detect())
        header.pack_start(refresh)
        root.append(header)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)

        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER)
        self.loading_page = Adw.StatusPage(child=spinner)
        self.stack.add_named(self.loading_page, "loading")

        self.error_page = Adw.StatusPage(icon_name="video-display-symbolic")
        retry = Gtk.Button(label="Try again", halign=Gtk.Align.CENTER)
        retry.add_css_class("pill")
        retry.connect("clicked", lambda *_: self.detect())
        self.error_page.set_child(retry)
        self.stack.add_named(self.error_page, "error")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.clamp = Adw.Clamp(maximum_size=620, margin_top=12, margin_bottom=24,
                               margin_start=12, margin_end=12)
        scroller.set_child(self.clamp)
        self.stack.add_named(scroller, "main")

        root.append(self.stack)
        self.toasts.set_child(root)
        self.set_content(self.toasts)
        self.detect()

    # ---- state pages -----------------------------------------------------
    def show_loading(self, title):
        self.loading_page.set_title(title)
        self.stack.set_visible_child_name("loading")

    def show_error(self, title, description):
        self.error_page.set_title(title)
        self.error_page.set_description(description)
        self.stack.set_visible_child_name("error")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def on_write_error(self, msg):
        self.toast(msg.splitlines()[0] if msg else "Couldn't change that setting")

    # ---- detection ---------------------------------------------------------
    def detect(self):
        if not shutil.which("ddcutil"):
            self.show_error("ddcutil isn't installed",
                            "Run install.sh, or install it with: sudo apt install ddcutil")
            return
        self.show_loading("Looking for monitors…")
        run_bg(detect_displays, self.on_detected)

    def on_detected(self, displays):
        if isinstance(displays, Exception) or not displays:
            self.show_error(
                "No controllable monitor found",
                "Turn on DDC/CI in the monitor's on-screen menu (usually under General), "
                "then make sure you logged out and back in after running install.sh.")
            return
        self.displays = displays
        self.load_display(0)

    def load_display(self, idx):
        self.current = self.displays[idx]
        self.show_loading(f"Reading settings from {self.current['name']}…")
        run_bg(read_monitor_state, self.on_state, self.current)

    def on_state(self, result):
        if isinstance(result, Exception):
            self.show_error("Couldn't read the monitor", str(result))
            return
        caps, state, rates = result
        self.build_content(caps, state, rates)
        self.stack.set_visible_child_name("main")

    # ---- main page ---------------------------------------------------------
    def build_content(self, caps, state, rates=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.sliders = {}
        self.loading = True

        top = Adw.PreferencesGroup()
        combo = Adw.ComboRow(title="Monitor",
                             model=Gtk.StringList.new([d["label"] for d in self.displays]))
        combo.set_selected(self.displays.index(self.current))
        combo.connect("notify::selected", self.on_monitor_changed)
        top.add(combo)
        if len(self.displays) > 1:
            row = Adw.ActionRow(title="Adjust all monitors together",
                                subtitle="Sliders change every connected monitor")
            sw = Gtk.Switch(active=self.sync_all, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s, _: setattr(self, "sync_all", s.get_active()))
            row.add_suffix(sw)
            row.set_activatable_widget(sw)
            top.add(row)
        box.append(top)

        shown = 0
        self.rate_row = None
        if rates and rates["options"]:
            group = Adw.PreferencesGroup(title="Display")
            self.rate_row = Adw.ComboRow(title="Refresh rate")
            self.rate_row.connect("notify::selected", self.on_refresh_rate)
            self.set_refresh_rates(rates)
            group.add(self.rate_row)
            box.append(group)
            shown += 1

        for title, items in SECTIONS:
            group, count = Adw.PreferencesGroup(title=title), 0
            for code, label, kind in items:
                if code not in state:
                    continue
                cur, maxv = state[code]
                if kind == "slider" and maxv:
                    group.add(self.make_slider(code, label, cur, maxv))
                elif kind == "choice":
                    values = caps.get(code, {}).get("values") or FALLBACK_LABELS.get(code, {})
                    group.add(self.make_choice(code, label, cur, values))
                else:
                    continue
                count += 1
            if count:
                box.append(group)
                shown += count

        if not shown:
            note = Adw.StatusPage(icon_name="dialog-warning-symbolic",
                                  title="No adjustable settings",
                                  description="The monitor answered, but didn't report any "
                                              "settings that can be changed over DDC/CI.")
            box.append(note)

        self.profiles_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.profiles_holder)
        self.refresh_profiles()

        self.clamp.set_child(box)
        self.loading = False

    def make_slider(self, code, label, cur, maxv):
        row = Adw.ActionRow(title=label)
        if cur is None:
            row.set_subtitle("Couldn't read the current value")
            cur = maxv // 2
        adj = Gtk.Adjustment(value=cur, lower=0, upper=maxv, step_increment=1, page_increment=10)
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj, hexpand=True,
                          draw_value=True, digits=0, value_pos=Gtk.PositionType.RIGHT,
                          valign=Gtk.Align.CENTER)
        scale.set_size_request(260, -1)
        scale.connect("value-changed", self.on_slider, code)
        row.add_suffix(scale)
        self.sliders[code] = scale
        return row

    def make_choice(self, code, label, cur, values):
        options = dict(sorted(values.items()))
        if cur not in options:
            options[cur] = f"Current (0x{cur:02X})"
        keys = list(options)
        row = Adw.ComboRow(title=label, model=Gtk.StringList.new([options[k] for k in keys]))
        row.set_selected(keys.index(cur))
        row.connect("notify::selected", self.on_choice, code, keys)
        return row

    # ---- refresh rate -------------------------------------------------------
    def set_refresh_rates(self, info):
        self.rate_info = info
        was_loading, self.loading = self.loading, True
        self.rate_row.set_model(Gtk.StringList.new([label for _, label in info["options"]]))
        keys = [mode_id for mode_id, _ in info["options"]]
        if info["current"] in keys:
            self.rate_row.set_selected(keys.index(info["current"]))
        self.rate_row.set_sensitive(len(keys) > 1)
        self.rate_row.set_subtitle("" if len(keys) > 1 else "Only one rate is available "
                                   "at the current resolution")
        self.loading = was_loading

    def reload_refresh_rates(self):
        """Pick up changes made elsewhere, like in GNOME Settings."""
        if self.rate_row is None or self.rate_pending or self.current is None:
            return
        display = self.current
        run_bg(read_refresh_rates,
               lambda info: self.rate_row is not None and display is self.current
               and isinstance(info, dict) and self.set_refresh_rates(info), display)

    def on_refresh_rate(self, row, _pspec):
        if self.loading or self.rate_pending or not self.rate_info:
            return
        info = self.rate_info
        new_id, label = info["options"][row.get_selected()]
        old_id = info["current"]
        if new_id == old_id:
            return
        self.rate_pending = True
        run_bg(lambda: apply_display_mode(info["connector"], new_id, APPLY_TEMPORARY),
               lambda res: self.confirm_refresh_rate(res, info, new_id, old_id, label))

    def confirm_refresh_rate(self, result, info, new_id, old_id, label):
        if isinstance(result, Exception):
            self.rate_pending = False
            msg = result.message if isinstance(result, GLib.Error) else str(result)
            self.toast(f"Couldn't change the refresh rate: {msg}")
            self.set_refresh_rates(info)
            return

        done = {"value": False}
        heading = f"Keep {label}?"
        body = "The refresh rate will go back to the previous setting in {} seconds."
        remaining = {"n": CONFIRM_SECONDS}

        def finish(keep):
            if done["value"]:
                return
            done["value"] = True
            if timer["id"]:
                GLib.source_remove(timer["id"])
                timer["id"] = None

            def after(res):
                self.rate_pending = False
                if isinstance(res, Exception):
                    self.toast("Couldn't save the refresh rate")
                elif keep:
                    self.toast(f"Refresh rate set to {label}")
                self.reload_refresh_rates()

            mode_id = new_id if keep else old_id
            run_bg(apply_display_mode, after, info["connector"], mode_id, APPLY_PERSISTENT)

        if hasattr(Adw, "AlertDialog"):
            dlg = Adw.AlertDialog(heading=heading, body=body.format(CONFIRM_SECONDS))
        else:
            dlg = Adw.MessageDialog(transient_for=self, modal=True, heading=heading,
                                    body=body.format(CONFIRM_SECONDS))
        dlg.add_response("revert", "Revert")
        dlg.add_response("keep", "Keep changes")
        dlg.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("keep")
        dlg.set_close_response("revert")
        dlg.connect("response", lambda _d, r: finish(r == "keep"))

        def tick():
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                timer["id"] = None  # returning SOURCE_REMOVE ends it
                finish(False)
                dlg.close()
                return GLib.SOURCE_REMOVE
            dlg.set_body(body.format(remaining["n"]))
            return GLib.SOURCE_CONTINUE

        timer = {"id": GLib.timeout_add_seconds(1, tick)}
        if hasattr(Adw, "AlertDialog"):
            dlg.present(self)
        else:
            dlg.present()

    # ---- events ------------------------------------------------------------
    def on_monitor_changed(self, row, _pspec):
        idx = row.get_selected()
        if 0 <= idx < len(self.displays) and self.displays[idx] is not self.current:
            self.load_display(idx)

    def write_slider(self, code, value):
        buses = [d["bus"] for d in self.displays] if self.sync_all else [self.current["bus"]]
        for bus in buses:
            self.writer.set(bus, code, value)

    def on_slider(self, scale, code):
        if not self.loading:
            self.write_slider(code, int(round(scale.get_value())))

    def on_choice(self, row, _pspec, code, keys):
        if self.loading:
            return
        if code == INPUT_SOURCE:
            self.toast("Switching input. This screen may go blank.")
        self.writer.set(self.current["bus"], code, keys[row.get_selected()])

    # ---- profiles ----------------------------------------------------------
    def refresh_profiles(self):
        child = self.profiles_holder.get_first_child()
        if child:
            self.profiles_holder.remove(child)

        group = Adw.PreferencesGroup(
            title="Profiles",
            description="Save the current picture settings and bring them back in one click.")

        entry_row = Gtk.ListBoxRow(activatable=False, selectable=False)
        hb = Gtk.Box(spacing=8, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        entry = Gtk.Entry(placeholder_text="Profile name, like Night", hexpand=True)
        save = Gtk.Button(label="Save profile")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self.save_profile(entry))
        entry.connect("activate", lambda *_: self.save_profile(entry))
        hb.append(entry)
        hb.append(save)
        entry_row.set_child(hb)
        group.add(entry_row)

        for name, values in self.profiles.items():
            summary = ", ".join(f"{SHORT_NAMES.get(int(k, 16), k)} {v}" for k, v in values.items())
            row = Adw.ActionRow(title=GLib.markup_escape_text(name), subtitle=summary)
            apply_btn = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
            apply_btn.connect("clicked", lambda _b, n=name: self.apply_profile(n))
            delete_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                    tooltip_text="Delete profile")
            delete_btn.add_css_class("flat")
            delete_btn.connect("clicked", lambda _b, n=name: self.delete_profile(n))
            row.add_suffix(apply_btn)
            row.add_suffix(delete_btn)
            group.add(row)

        self.profiles_holder.append(group)

    def save_profile(self, entry):
        values = {f"{c:02x}": int(round(self.sliders[c].get_value()))
                  for c in PROFILE_CODES if c in self.sliders}
        if not values:
            self.toast("Nothing to save for this monitor")
            return
        name = entry.get_text().strip() or f"Profile {len(self.profiles) + 1}"
        self.profiles[name] = values
        save_profiles(self.profiles)
        self.refresh_profiles()
        self.toast(f"Saved “{name}”")

    def apply_profile(self, name):
        values = self.profiles.get(name, {})
        self.loading = True
        for k, v in values.items():
            code = int(k, 16)
            if code in self.sliders:
                self.sliders[code].set_value(v)
        self.loading = False
        for k, v in values.items():
            code = int(k, 16)
            if code in self.sliders:
                self.write_slider(code, v)
        self.toast(f"Applied “{name}”")

    def delete_profile(self, name):
        self.profiles.pop(name, None)
        save_profiles(self.profiles)
        self.refresh_profiles()
        self.toast(f"Deleted “{name}”")


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        (self.props.active_window or MainWindow(self)).present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
