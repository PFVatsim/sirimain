"""
VOICE DECK - voice controlled launcher for Windows
Say "open <app>" or "call <person>".  Type "add" to register new apps / contacts.
"""
import array
import collections
import difflib
import glob
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser

try:
  import speech_recognition as sr
except Exception:
  sr = None
try:
  import sounddevice as sd
except Exception:
  sd = None
try:
  import pyautogui
except Exception:
  pyautogui = None

if getattr(sys, "frozen", False):
  # installed .exe: Program Files isn't writable, so keep settings in %APPDATA%
  BASE = os.path.join(
      os.environ.get("APPDATA", os.path.expanduser("~")), "VoiceDeck"
  )
  os.makedirs(BASE, exist_ok=True)
else:
  BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")

try:  # own taskbar identity so it pins/groups as "Voice Deck", not as Python
  import ctypes

  ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
      "VoiceDeck.App.1"
  )
except Exception:
  pass

# ------------------------------------------------------------------ colours
BG = "#07070f"
PANEL = "#0e0e1c"
CYAN = "#00f0ff"
PINK = "#ff2bd6"
PURPLE = "#8a5cff"
GREEN = "#39ff88"
RED = "#ff4d6d"
TEXT = "#e8e8ff"
DIM = "#6b6b8f"


# ------------------------------------------------------------------ data
def load_data():
  try:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
      d = json.load(f)
  except Exception:
    d = {}
  d.setdefault("apps", {})  # custom/added apps: name -> target
  d.setdefault("contacts", {})  # name -> {"type": "username"|"id", "value": str}
  return d


def save_data(d):
  with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2)


def norm(s):
  return re.sub(r"[^a-z0-9]", "", s.lower())


# ------------------------------------------------------------------ app catalog
def env(name):
  return os.environ.get(name, "")


def first_existing(paths):
  for p in paths:
    for hit in glob.glob(p):
      if os.path.exists(hit):
        return hit
  return None


def start_menu_shortcuts():
  """Return {display name: path to .lnk} for every Start Menu shortcut."""
  roots = [
      os.path.join(
          env("ProgramData"), r"Microsoft\Windows\Start Menu\Programs"
      ),
      os.path.join(env("APPDATA"), r"Microsoft\Windows\Start Menu\Programs"),
  ]
  out = {}
  for root in roots:
    for dirpath, _, files in os.walk(root):
      for fn in files:
        if fn.lower().endswith(".lnk"):
          name = fn[:-4]
          if any(w in name.lower() for w in ("uninstall", "readme", "help")):
            continue
          out[name] = os.path.join(dirpath, fn)
  return out


def steam_roots():
  roots = []
  for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
    if os.path.isdir(p):
      roots.append(p)
  return roots


def steam_games():
  """Scan every Steam library for installed games -> {name: steam://rungameid/ID}"""
  libs = set()
  for root in steam_roots():
    libs.add(os.path.join(root, "steamapps"))
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
      with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
        for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
          libs.add(os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps"))
    except Exception:
      pass
  games = {}
  for lib in libs:
    for acf in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
      try:
        txt = open(acf, "r", encoding="utf-8", errors="ignore").read()
        appid = re.search(r'"appid"\s+"(\d+)"', txt).group(1)
        name = re.search(r'"name"\s+"([^"]+)"', txt).group(1)
        if "Steamworks" in name or "Redistributable" in name:
          continue
        games[name] = "steam://rungameid/" + appid
      except Exception:
        continue
  return games


def default_apps(shortcuts):
  """The built-in apps with their usual install locations."""

  def lnk(*keys):
    for name, path in shortcuts.items():
      if all(k in name.lower() for k in keys):
        return path
    return None

  la, ad = env("LOCALAPPDATA"), env("APPDATA")
  apps = {}

  apps["Discord"] = (
      first_existing([os.path.join(la, r"Discord\Update.exe")])
      and "discord://"
      or lnk("discord")
      or "discord://"
  )
  apps["Steam"] = first_existing([
      r"C:\Program Files (x86)\Steam\steam.exe",
      r"C:\Program Files\Steam\steam.exe",
  ]) or lnk("steam")
  apps["Roblox"] = (
      lnk("roblox", "player")
      or first_existing(
          [os.path.join(la, r"Roblox\Versions\*\RobloxPlayerBeta.exe")]
      )
      or "roblox-player:"
  )
  apps["Roblox Studio"] = lnk("roblox", "studio") or first_existing([
      os.path.join(la, r"Roblox\Versions\*\RobloxStudioBeta.exe")
  ])
  apps["Spotify"] = first_existing([
      os.path.join(ad, r"Spotify\Spotify.exe"),
      os.path.join(la, r"Microsoft\WindowsApps\Spotify.exe"),
  ]) or lnk("spotify") or "spotify:"
  apps["SteamTools"] = lnk("steamtools") or lnk("steam", "tools") or first_existing([
      r"C:\Program Files\SteamTools\*.exe",
      r"C:\Program Files (x86)\SteamTools\*.exe",
      os.path.join(la, r"SteamTools\*.exe"),
      os.path.join(ad, r"SteamTools\*.exe"),
  ])
  return {k: v for k, v in apps.items() if v}


def build_catalog(data):
  """name -> target, everything the app can open (priority: custom > defaults > games > start menu)."""
  shortcuts = start_menu_shortcuts()
  cat = {}
  cat.update(shortcuts)
  cat.update(steam_games())
  cat.update(default_apps(shortcuts))
  cat.update(data["apps"])
  return cat


def launch(target):
  if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target) and not re.match(
      r"^[a-zA-Z]:[\\/]", target
  ):
    os.startfile(target)  # protocol link (steam://, discord://, spotify: ...)
  else:
    os.startfile(target)


def discord_running():
  try:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Discord.exe"],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    return "Discord.exe" in out
  except Exception:
    return False


def find_discord_window():
  """Largest visible window whose title ends in 'Discord' (not a browser tab)."""
  try:
    import pygetwindow as gw

    best = None
    for w in gw.getAllWindows():
      t = w.title or ""
      if not t.endswith("Discord"):
        continue
      if any(b in t for b in ("Chrome", "Edge", "Firefox", "Opera", "Brave")):
        continue
      if w.width < 300 or w.height < 300:
        continue
      if best is None or w.width * w.height > best.width * best.height:
        best = w
    return best
  except Exception:
    return None


def discord_call(contact, cal=None):
  """Username: open Discord -> Ctrl+K quick switcher -> open the DM -> click the call button

  (position saved by 'calibrate'; without it, falls back to Discord's Ctrl+[ shortcut).
  User ID: opens their profile only.
  """
  kind, val = contact["type"], contact["value"]
  if kind == "id":
    os.startfile("discord://-/users/%s" % val)
    return "Opened their Discord profile - save them by Username to auto-call."
  if not pyautogui:
    raise RuntimeError("pyautogui missing - can't drive Discord")
  was_running = discord_running()
  os.startfile("discord://")  # brings Discord to the front (or launches it)
  time.sleep(2.0 if was_running else 10.0)
  win = find_discord_window()
  if win:
    try:
      if win.isMinimized:
        win.restore()
      win.activate()
    except Exception:
      pass
    time.sleep(0.5)
  pyautogui.hotkey("ctrl", "k")  # quick switcher
  time.sleep(0.7)
  pyautogui.typewrite(val, interval=0.05)
  time.sleep(1.2)  # let results load
  pyautogui.press("enter")  # opens the DM
  time.sleep(1.8)
  if cal:
    win = find_discord_window()
    if not win:
      raise RuntimeError(
          "couldn't find the Discord window to click the call button"
      )
    pyautogui.moveTo(win.right - cal["from_right"], win.top + cal["from_top"], 0.3)
    pyautogui.click()
    return "Clicked the call button for %s." % val
  pyautogui.hotkey("ctrl", "[")
  return (
      "Tried the call shortcut for %s. If nothing rang, type  calibrate  once."
      % val
  )


# ------------------------------------------------------------------ matching
def best_match(spoken, names):
  key = norm(spoken)
  if not key or not names:
    return None
  by_norm = {norm(n): n for n in names}
  if key in by_norm:
    return by_norm[key]
  starts = [k for k in by_norm if k.startswith(key) or key.startswith(k)]
  if starts:
    # prefer the closest in length
    return by_norm[min(starts, key=lambda k: abs(len(k) - len(key)))]
  close = difflib.get_close_matches(key, list(by_norm), n=1, cutoff=0.6)
  return by_norm[close[0]] if close else None


def suggest(query, names, limit=8):
  q = norm(query)
  names = sorted(names, key=str.lower)
  if not q:
    return names[:limit]
  starts = [n for n in names if norm(n).startswith(q)]
  contains = [n for n in names if q in norm(n) and n not in starts]
  fuzzy = [
      n
      for n in difflib.get_close_matches(query, names, n=limit, cutoff=0.5)
      if n not in starts and n not in contains
  ]
  return (starts + contains + fuzzy)[:limit]


# ------------------------------------------------------------------ UI
class App:

  def __init__(self, root):
    self.root = root
    self.data = load_data()
    self.catalog = build_catalog(self.data)
    self.q = queue.Queue()
    self.state = None  # add-flow state
    self.tmp = {}
    self.mode = "idle"  # idle / listening / heard / error
    self.listening = True
    self.t0 = time.time()
    self.heard_until = 0
    self.suggestions = []

    root.title("VOICE DECK")
    root.configure(bg=BG)
    root.geometry("760x820")
    root.minsize(640, 700)

    tk.Label(
        root,
        text="V O I C E    D E C K",
        font=("Consolas", 22, "bold"),
        fg=CYAN,
        bg=BG,
    ).pack(pady=(18, 0))
    tk.Label(
        root,
        text='say  "open Spotify"    •    "call Logan"    •    type  add',
        font=("Consolas", 10),
        fg=DIM,
        bg=BG,
    ).pack(pady=(0, 4))

    self.canvas = tk.Canvas(
        root, width=700, height=300, bg=BG, highlightthickness=0
    )
    self.canvas.pack()
    self.canvas.bind("<Button-1>", lambda e: self.toggle_listen())

    self.status = tk.Label(
        root,
        text="starting...",
        font=("Consolas", 13, "bold"),
        fg=CYAN,
        bg=BG,
    )
    self.status.pack(pady=(0, 2))
    self.heard = tk.Label(root, text="", font=("Consolas", 11), fg=DIM, bg=BG)
    self.heard.pack()

    self.prompt = tk.Label(
        root, text="type a command", font=("Consolas", 10), fg=PURPLE, bg=BG
    )
    self.prompt.pack(pady=(14, 2))

    box = tk.Frame(root, bg=CYAN, padx=2, pady=2)
    box.pack(padx=40, fill="x")
    self.entry = tk.Entry(
        box,
        font=("Consolas", 15),
        bg=PANEL,
        fg=TEXT,
        insertbackground=CYAN,
        relief="flat",
        bd=8,
    )
    self.entry.pack(fill="x")
    # Updated to allow input/text up to 30,000 characters
    self.entry.config(validate="key")
    self.entry.bind("<KeyRelease>", self.on_key)
    self.entry.bind("<Return>", self.on_enter)
    self.entry.bind("<Down>", lambda e: self.move_sel(1))
    self.entry.bind("<Up>", lambda e: self.move_sel(-1))
    self.entry.bind("<Escape>", lambda e: self.reset_flow())
    self.entry.focus_set()

    self.lb = tk.Listbox(
        root,
        font=("Consolas", 12),
        bg=PANEL,
        fg=TEXT,
        selectbackground=PINK,
        selectforeground="white",
        activestyle="none",
        relief="flat",
        bd=0,
        height=8,
        highlightthickness=1,
        highlightbackground=PURPLE,
    )
    self.lb.pack(padx=40, pady=(6, 0), fill="x")
    self.lb.bind("<Double-Button-1>", lambda e: self.on_enter())

    self.log_lbl = tk.Label(
        root,
        text="",
        font=("Consolas", 10),
        fg=GREEN,
        bg=BG,
        wraplength=680,
        justify="center",
    )
    self.log_lbl.pack(pady=10)

    self.refresh_suggestions()
    self.animate()
    self.poll()
    self.start_voice()

  # ---------------- animation with non-overlapping circles & speech reaction
  def animate(self):
    c = self.canvas
    c.delete("all")
    w, h = 700, 300
    cx, cy = w / 2, h / 2
    t = time.time() - self.t0
    now = time.time()

    # Determine speech reaction amplitude from current microphone level
    lvl = getattr(self, "level", 0.0)
    current_reaction = min(max(lvl / 2000.0, 0.0), 2.0)

    if now < self.heard_until:
      col, amp = GREEN, 1.0
    elif self.mode == "listening" and self.listening:
      col, amp = CYAN, 0.55 + current_reaction
    elif self.mode == "error" or not self.listening:
      col, amp = RED if self.mode == "error" else DIM, 0.15
    else:
      col, amp = PURPLE, 0.3

    # --- DRAW NON-OVERLAPPING CONCENTRIC RINGS ---
    base_radii = [55, 95, 135, 175, 215]
    for i, base_r in enumerate(base_radii):
      r = base_r + int(current_reaction * 12 * (i + 1) * 0.3)
      ring_color = col if i % 2 == 0 else PINK
      c.create_oval(
          cx - r, cy - r, cx + r, cy + r, outline=ring_color, width=2
      )

    # --- CENTER CORE PULSAR ---
    pulse = 28 + int(current_reaction * 15)
    for k, cc in enumerate((PURPLE, PINK, col)):
      rr = max(5, pulse - k * 6)
      c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill=cc, outline="")

    # --- SPECTRUM BARS ---
    num_bars = 48
    outer_radius = 240 + int(current_reaction * 15)
    for i in range(num_bars):
      ang = i / num_bars * math.tau
      bar_len = 8 + (math.sin(t * 6 + i * 0.7) + 1) * 12 * amp
      x1 = cx + math.cos(ang) * outer_radius
      y1 = cy + math.sin(ang) * outer_radius
      x2 = cx + math.cos(ang) * (outer_radius + bar_len)
      y2 = cy + math.sin(ang) * (outer_radius + bar_len)
      c.create_line(x1, y1, x2, y2, fill=col if i % 3 else PINK, width=2)

    # --- LIVE MIC LEVEL BAR ---
    thr = getattr(self, "thr", 0.0)
    if getattr(self, "stream", None) is not None:
      bw = 360
      x0 = cx - bw / 2
      full = 6000.0
      c.create_rectangle(x0, 288, x0 + bw, 296, outline=DIM)
      c.create_rectangle(
          x0,
          288,
          x0 + bw * min(1.0, lvl / full),
          296,
          fill=GREEN if lvl > thr else CYAN,
          outline="",
      )
      tx = x0 + bw * min(1.0, thr / full)
      c.create_line(tx, 284, tx, 300, fill=PINK, width=2)

    self.root.after(33, self.animate)

  # ---------------- voice
  def start_voice(self):
    if sr is None or sd is None:
      self.set_status("speech/mic library missing - typing only", RED)
      self.mode = "error"
      self.log(
          "Run:  py -3 -m pip install SpeechRecognition sounddevice", RED
      )
      return
    self.rec = sr.Recognizer()
    self.audio_q = queue.Queue()
    self.stream = None
    self.level = 0.0
    self.thr = 0.0
    self.devices = []
    try:
      for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
          self.devices.append((i, d["name"]))
    except Exception as e:
      self.mode = "error"
      self.set_status("mic problem - typing only", RED)
      self.log("Couldn't list microphones: %s" % e, RED)
      return
    if not self.devices:
      self.mode = "error"
      self.set_status("no microphone found - typing only", RED)
      self.log(
          "Windows reports no input device. Plug one in / enable it in Sound"
          " settings.",
          RED,
      )
      return
    try:
      default_in = sd.default.device[0]
    except Exception:
      default_in = None
    self.dev_idx = 0
    for n, (i, _) in enumerate(self.devices):
      if i == default_in:
        self.dev_idx = n
    self.open_stream()
    threading.Thread(target=self.vad_loop, daemon=True).start()
    self.root.bind("<F2>", lambda e: self.next_device())

  def open_stream(self):
    """Open the current device; falls back to its native sample rate if 16 kHz is refused."""
    if self.stream is not None:
      try:
        self.stream.stop()
        self.stream.close()
      except Exception:
        pass
      self.stream = None
    dev, name = self.devices[self.dev_idx]
    last = None
    rates = [16000]
    try:
      rates.append(int(sd.query_devices(dev)["default_samplerate"]))
    except Exception:
      pass
    for rate in rates:
      try:
        self.rate = rate
        self.stream = sd.RawInputStream(
            samplerate=rate,
            device=dev,
            channels=1,
            dtype="int16",
            blocksize=rate // 10,
            callback=self.audio_cb,
        )
        self.stream.start()
        self.mode = "listening"
        self.set_status("listening  -  mic: %s" % name[:40], CYAN)
        self.log(
            "F2 = switch microphone.  Watch the level bar under the orb: it"
            " should jump when you talk.",
            DIM,
        )
        return
      except Exception as e:
        last = e
    self.mode = "error"
    self.set_status("mic failed: %s" % name[:40], RED)
    self.log("Couldn't open that mic (%s). Press F2 to try another." % last, RED)

  def next_device(self):
    if not self.devices:
      return
    self.dev_idx = (self.dev_idx + 1) % len(self.devices)
    self.open_stream()

  def audio_cb(self, indata, frames, time_info, status):
    self.audio_q.put(bytes(indata))

  @staticmethod
  def rms(chunk):
    a = array.array("h", chunk)
    if not a:
      return 0.0
    return math.sqrt(sum(x * x for x in a) / len(a))

  def vad_loop(self):
    """Tiny voice-activity detector: record while you talk, send each phrase to recognition."""
    noise = None
    preroll = collections.deque(maxlen=3)  # keep 0.3s before speech starts
    frames, silent, talking = [], 0, False
    while True:
      chunk = self.audio_q.get()
      if not self.listening:
        frames, silent, talking = [], 0, False
        preroll.clear()
        continue
      level = self.rms(chunk)
      if noise is None:
        noise = level
      thr = max(150.0, noise * 2.5)
      self.level, self.thr = level, thr
      if level > thr:
        if not talking:
          talking = True
          frames = list(preroll)
        frames.append(chunk)
        silent = 0
      elif talking:
        frames.append(chunk)
        silent += 1
        if silent >= 7:  # 0.7s of quiet = phrase finished
          self.finish_phrase(frames)
          frames, silent, talking = [], 0, False
      else:
        noise = noise * 0.95 + level * 0.05
        preroll.append(chunk)
      if talking and len(frames) > 80:  # 8s cap
        self.finish_phrase(frames)
        frames, silent, talking = [], 0, False

  def finish_phrase(self, frames):
    if len(frames) < 4:  # too short, probably a click
      return
    data = b"".join(frames)
    threading.Thread(
        target=self.recognize, args=(data,), daemon=True
    ).start()

  def recognize(self, data):
    self.q.put(("status", "heard something - recognizing..."))
    try:
      audio = sr.AudioData(data, self.rate, 2)
      text = self.rec.recognize_google(audio)
      self.q.put(("voice", text))
    except sr.UnknownValueError:
      self.q.put(
          (
              "err",
              (
                  "Heard sound but couldn't make out words - speak a bit"
                  " closer/clearer."
              ),
          )
      )
    except Exception as e:
      self.q.put(
          (
              "err",
              (
                  "Speech recognition failed: %s: %s  (needs internet)"
                  % (type(e).__name__, e)
              ),
          )
      )

  def toggle_listen(self):
    if self.mode == "error":
      return
    self.listening = not self.listening
    self.set_status(
        "listening...  (click the orb to mute)"
        if self.listening
        else "muted  (click the orb to listen)",
        CYAN if self.listening else DIM,
    )

  def poll(self):
    try:
      while True:
        kind, text = self.q.get_nowait()
        if kind == "voice":
          self.heard.config(text='heard: "%s"' % text)
          self.heard_until = time.time() + 1.2
          self.handle_voice(text)
        elif kind == "status":
          self.heard.config(text=text)
        elif kind == "err":
          self.heard.config(text="")
          self.log(text, RED)
    except queue.Empty:
      pass
    self.root.after(100, self.poll)

  # ---------------- helpers
  def set_status(self, text, colour=CYAN):
    self.status.config(text=text, fg=colour)

  def log(self, text, colour=GREEN):
    self.log_lbl.config(text=text, fg=colour)

  def names_for(self, kind):
    if kind == "call":
      return list(self.data["contacts"].keys())
    return list(self.catalog.keys())

  # ---------------- commands
  def handle_voice(self, text):
    if self.state:  # don't fire commands while in the add flow
      return
    self.run_command(text)

  def run_command(self, text):
    t = text.strip()
    low = t.lower()
    m = re.match(r"^(open|launch|start|call)\s+(.+)$", low)
    if low == "add":
      self.begin_add()
      return
    if low == "calibrate":
      self.begin_calibrate()
      return
    if not m:
      self.log('Say or type "open <app>" or "call <name>".', RED)
      return
    verb, name = m.group(1), m.group(2).strip()
    if verb == "call":
      hit = best_match(name, self.names_for("call"))
      if not hit:
        self.log(
            'No contact "%s". Type add -> Call to create one.' % name, RED
        )
        return
      self.set_status("calling %s..." % hit, PINK)
      self.log(
          (
              "Driving Discord - don't touch the keyboard for a few"
              " seconds..."
          ),
          PINK,
      )
      self.root.update()
      try:
        self.log(
            discord_call(
                self.data["contacts"][hit], self.data.get("call_btn")
            )
        )
      except Exception as e:
        self.log("Couldn't call %s: %s" % (hit, e), RED)
    else:
      hit = best_match(name, self.names_for("open"))
      if not hit:
        self.log('No app "%s". Type add -> Open to add it.' % name, RED)
        return
      try:
        launch(self.catalog[hit])
        self.log("Opening %s" % hit)
        self.set_status("opening %s" % hit, GREEN)
      except Exception as e:
        self.log("Couldn't open %s: %s" % (hit, e), RED)

  # ---------------- calibrate the Discord call button
  def begin_calibrate(self):
    self.log(
        (
            "Switch to Discord, open a chat with someone, and HOLD your mouse"
            " over the CALL (phone) button until the countdown ends."
        ),
        PINK,
    )
    self.cal_count(10)

  def cal_count(self, n):
    if n > 0:
      self.set_status("calibrating - hover the call button: %d" % n, PINK)
      self.root.after(1000, lambda: self.cal_count(n - 1))
      return
    win = find_discord_window()
    if not win or not pyautogui:
      self.set_status("calibration failed", RED)
      self.log(
          (
              "Couldn't find the Discord window. Keep Discord open and"
              " visible, then type calibrate again."
          ),
          RED,
      )
      return
    x, y = pyautogui.position()
    if not (win.left <= x <= win.right and win.top <= y <= win.bottom):
      self.set_status("calibration failed", RED)
      self.log("Your mouse wasn't over the Discord window. Try again.", RED)
      return
    self.data["call_btn"] = {
        "from_right": win.right - x,
        "from_top": y - win.top,
    }
    save_data(self.data)
    self.set_status("calibrated!", GREEN)
    self.log(
        (
            "Saved the call button position. Now try:  call <name>  (keep"
            " Discord's window size the same)."
        )
    )

  # ---------------- add flow
  def begin_add(self):
    self.state = "type"
    self.tmp = {}
    self.entry.delete(0, "end")
    self.prompt.config(text="ADD  -  Call or Open?", fg=PINK)
    self.refresh_suggestions()

  def reset_flow(self):
    self.state = None
    self.tmp = {}
    self.entry.delete(0, "end")
    self.prompt.config(text="type a command", fg=PURPLE)
    self.refresh_suggestions()

  def on_key(self, e=None):
    if e is not None and e.keysym in (
        "Up",
        "Down",
        "Return",
        "Escape",
    ):
      return
    # Ensure text entry stays well within the expanded character handling limits (up to 30,000 characters)
    if len(self.entry.get()) > 30000:
      self.entry.delete(30000, "end")
    self.refresh_suggestions()

  def refresh_suggestions(self):
    text = self.entry.get()
    s = []
    if self.state == "type":
      s = [
          x for x in ("Call", "Open") if x.lower().startswith(text.lower())
      ]
    elif self.state == "open_pick":
      s = suggest(text, list(self.catalog.keys()), 8)
    elif self.state == "call_kind":
      s = [
          x
          for x in ("Username", "User ID")
          if x.lower().startswith(text.lower())
      ]
    elif self.state is None:
      m = re.match(r"^(open|call)\s+(.*)$", text, re.I)
      if m:
        pool = self.names_for(m.group(1).lower())
        s = [
            "%s %s" % (m.group(1).lower(), n)
            for n in suggest(m.group(2), pool, 8)
        ]
      elif text:
        s = [
            x
            for x in ("open ", "call ", "add", "calibrate")
            if x.startswith(text.lower())
        ]
    self.suggestions = s
    self.lb.delete(0, "end")
    for x in s:
      self.lb.insert("end", "  " + x)
    if s:
      self.lb.selection_set(0)

  def move_sel(self, d):
    if not self.suggestions:
      return "break"
    cur = self.lb.curselection()
    i = (cur[0] if cur else -1) + d
    i = max(0, min(len(self.suggestions) - 1, i))
    self.lb.selection_clear(0, "end")
    self.lb.selection_set(i)
    self.lb.see(i)
    return "break"

  def chosen(self):
    """Selected suggestion if any, else raw text."""
    raw = self.entry.get().strip()
    cur = self.lb.curselection()
    if cur and self.suggestions:
      return self.suggestions[cur[0]], raw
    return raw, raw

  def on_enter(self, e=None):
    pick, raw = self.chosen()
    st = self.state

    if st is None:
      # in normal mode a highlighted "open xyz" suggestion wins only if the user typed a partial name
      cmd = (
          pick
          if pick.lower().startswith(("open ", "call "))
          and raw.lower() != pick.lower()
          and len(raw.split()) > 1
          else raw
      )
      self.entry.delete(0, "end")
      if cmd:
        self.run_command(cmd)
      self.refresh_suggestions()
      return

    if st == "type":
      choice = pick.lower()
      if choice not in ("call", "open"):
        self.log('Type "Call" or "Open".', RED)
        return
      self.entry.delete(0, "end")
      if choice == "open":
        self.state = "open_pick"
        self.prompt.config(
            text="ADD OPEN  -  search for the app / Steam game", fg=PINK
        )
      else:
        self.state = "call_name"
        self.prompt.config(
            text="ADD CALL  -  what name do you say?  (e.g. Logan)", fg=PINK
        )

    elif st == "open_pick":
      if pick not in self.catalog:
        self.log("Pick an app from the list.", RED)
        return
      self.tmp["app"] = pick
      self.entry.delete(0, "end")
      self.entry.insert(0, pick)
      self.entry.select_range(0, "end")
      self.state = "open_alias"
      self.prompt.config(
          text='What do you want to say after "open"?  (Enter keeps "%s")'
          % pick,
          fg=PINK,
      )

    elif st == "open_alias":
      alias = raw or self.tmp["app"]
      self.data["apps"][alias] = self.catalog[self.tmp["app"]]
      save_data(self.data)
      self.catalog = build_catalog(self.data)
      self.log('Saved! say "open %s"' % alias)
      self.reset_flow()
      return

    elif st == "call_name":
      if not raw:
        return
      self.tmp["name"] = raw
      self.entry.delete(0, "end")
      self.state = "call_kind"
      self.prompt.config(
          text='"call %s" -> use Discord Username or User ID?' % raw, fg=PINK
      )

    elif st == "call_kind":
      c = pick.lower().replace(" ", "")
      if c not in ("username", "userid"):
        self.log('Type "Username" or "User ID".', RED)
        return
      self.tmp["kind"] = "username" if c == "username" else "id"
      self.entry.delete(0, "end")
      self.state = "call_value"
      self.prompt.config(
          text="Enter their Discord %s"
          % (
              "username"
              if self.tmp["kind"] == "username"
              else "user ID (numbers)"
          ),
          fg=PINK,
      )

    elif st == "call_value":
      if not raw:
        return
      if self.tmp["kind"] == "id" and not raw.isdigit():
        self.log(
            (
                "A user ID is only numbers (Discord > Settings > Advanced >"
                " Developer Mode, then right click user > Copy User ID)."
            ),
            RED,
        )
        return
      self.data["contacts"][self.tmp["name"]] = {
          "type": self.tmp["kind"],
          "value": raw,
      }
      save_data(self.data)
      self.log('Saved! say "call %s"' % self.tmp["name"])
      self.reset_flow()
      return

    self.refresh_suggestions()


def main():
  root = tk.Tk()
  App(root)
  root.mainloop()


if __name__ == "__main__":
  main()