#!/usr/bin/env python3
"""Menu bar Claude quota display for macOS.

A thin NSStatusItem front-end over the existing `quota_api` data layer: same
token handling, same usage endpoint, same thresholds as quota_display.py, but
living in the menu bar instead of a pygame window.

    pip install rumps          # or:  uv add rumps
    python quota_menubar.py    # must sit next to quota_api.py

Only requirement is being logged into Claude Code — the token comes from the
login Keychain via quota_api, exactly as the pygame app does. No window, no
Dock icon.

NOTE on structure: this file deliberately re-implements the small fetch loop
and reset-time helpers so it stays standalone (importing quota_display would
drag in pygame). If you'd rather not duplicate the backoff/stale logic, the
clean move is to lift `State`, `fetch_loop`, `parse_reset`, `fmt_reset_short`
and `bar_color` out of quota_display.py into a pygame-free `quota_core.py`
and import them from both front-ends. Left as a follow-up to keep this diff
self-contained.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import threading
import time
import urllib.error

import rumps
from AppKit import (
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSImage,
)

import quota_api

# ---- config (mirrors quota_display.py) ------------------------------------

POLL_INTERVAL = 5 * 60      # seconds between fetches (quotas move slowly)
MAX_BACKOFF = 30 * 60       # cap for exponential backoff after errors
STALE_AFTER = 20 * 60       # seconds before cached data is flagged stale
UI_TICK = 5                 # seconds between cheap main-thread UI refreshes


# Small drawn status dot. NSStatusItem.title can't size an emoji down, so
# instead of a 🟢 glyph we render a tiny filled circle as the status item's
# image and keep the title as plain text beside it. Colours mirror bar_color()
# in quota_display.py; "idle" is the muted grey shown before the first fetch.
DOT_RGB = {
    "green":  (60, 190, 120),
    "yellow": (220, 200, 70),
    "orange": (230, 150, 60),
    "red":    (225, 80, 80),
    "idle":   (120, 126, 140),
}

ICON_PT = 16          # status-item image canvas, in points (~menu bar height)
DOT_DIAMETER = 8      # the visible dot — small enough to sit in line with text
FILLED = True         # set False for a hollow ring instead of a solid dot
RING_WIDTH = 1.6


def dot_level(pct: float) -> str:
    """Threshold bucket for the dot colour — mirrors bar_color()."""
    if pct >= 95:
        return "red"
    if pct >= 80:
        return "orange"
    if pct >= 50:
        return "yellow"
    return "green"


def _render_dot_png(rgb: tuple[int, int, int], path: str) -> None:
    """Draw one centred circle of `rgb` on a transparent ICON_PT canvas and
    write it to `path` as PNG. Marked non-template so macOS keeps our colour
    rather than tinting it to the menu bar. (Render at 2x if you want it razor
    sharp on Retina; an 8pt solid dot is fine as-is.)"""
    img = NSImage.alloc().initWithSize_((ICON_PT, ICON_PT))
    img.lockFocus()
    r, g, b = rgb
    NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, 1.0
    ).set()
    inset = (ICON_PT - DOT_DIAMETER) / 2.0
    # PyObjC bridges an NSRect to ((origin_x, origin_y), (width, height)).
    oval = NSBezierPath.bezierPathWithOvalInRect_(
        ((inset, inset), (DOT_DIAMETER, DOT_DIAMETER))
    )
    if FILLED:
        oval.fill()
    else:
        oval.setLineWidth_(RING_WIDTH)
        oval.stroke()
    img.unlockFocus()
    img.setTemplate_(False)

    rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    png.writeToFile_atomically_(path, True)


def build_dot_icons() -> dict[str, str]:
    """Pre-render one PNG per colour into a temp dir; return {level: path}.
    Done once at startup so tick() just swaps the icon path by level."""
    directory = tempfile.mkdtemp(prefix="claude-quota-dots-")
    paths = {}
    for level, rgb in DOT_RGB.items():
        png_path = os.path.join(directory, f"{level}.png")
        _render_dot_png(rgb, png_path)
        paths[level] = png_path
    return paths


# Credits come back in MINOR units (pence/cents), so 52 means £0.52, not £52.
CURRENCY_SYMBOLS = {"USD": "$", "GBP": "\u00A3", "EUR": "\u20AC"}


def fmt_money(minor_units: float, currency: str) -> str:
    sym = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    return f"{sym}{minor_units / 100:.2f}"


# ---- reset-time formatting (lifted from quota_display, pygame-free) --------

def parse_reset(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return None


def fmt_countdown(when: dt.datetime | None) -> str:
    """Terse time until reset for the menu bar, e.g. '2h14m', '14m', '<1m'."""
    if when is None:
        return ""
    secs = (when - dt.datetime.now().astimezone()).total_seconds()
    if secs <= 0:
        return "now"
    hours, rem = divmod(int(secs), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def fmt_reset_short(when: dt.datetime | None) -> str:
    if when is None:
        return ""
    now = dt.datetime.now().astimezone()
    if when.date() == now.date():
        return f"resets {when:%H:%M}"
    if (when.date() - now.date()).days == 1:
        return f"resets tomorrow {when:%H:%M}"
    return f"resets {when:%a %b} {when.day}"


# ---- background fetch thread (mirrors quota_display.fetch_loop) ------------

class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.usage: dict | None = None
        self.last_success_mono: float = 0.0
        self.last_success_wall: float = 0.0
        self.error: str | None = None

    def update_ok(self, usage: dict) -> None:
        with self.lock:
            self.usage = usage
            self.last_success_mono = time.monotonic()
            self.last_success_wall = time.time()
            self.error = None

    def update_err(self, msg: str) -> None:
        with self.lock:
            self.error = msg

    def snapshot(self):
        with self.lock:
            return (self.usage, self.last_success_mono,
                    self.last_success_wall, self.error)


def fetch_loop(state: State, stop: threading.Event, wake: threading.Event) -> None:
    """Poll the usage endpoint with exponential backoff, honouring Retry-After
    on 429. `wake` lets the 'Refresh now' menu item interrupt the sleep."""
    backoff = POLL_INTERVAL
    while not stop.is_set():
        try:
            state.update_ok(quota_api.fetch_usage())
            wait = POLL_INTERVAL
            backoff = POLL_INTERVAL
        except urllib.error.HTTPError as exc:
            state.update_err(str(exc))
            backoff = min(backoff * 2, MAX_BACKOFF)
            wait = backoff
            if exc.code == 429:
                ra = exc.headers.get("Retry-After") if exc.headers else None
                if ra and ra.strip().isdigit():
                    wait = max(float(ra.strip()), backoff)
        except Exception as exc:  # noqa: BLE001 - keep the app alive on any error
            state.update_err(str(exc))
            backoff = min(backoff * 2, MAX_BACKOFF)
            wait = backoff
        wake.wait(wait)
        wake.clear()


# ---- the menu bar app -----------------------------------------------------

class QuotaBarApp(rumps.App):
    def __init__(self) -> None:
        # Pre-render the coloured dots, then start with the muted "idle" dot and
        # a plain "Claude" title until the first fetch lands. template=False so
        # macOS shows our colours instead of tinting the image to the menu bar.
        self._dot_icons = build_dot_icons()
        self._dot_level: str | None = None
        super().__init__("Claude", title="Claude",
                         icon=self._dot_icons["idle"], template=False)
        self.state = State()
        self.stop = threading.Event()
        self.wake = threading.Event()

        # Menu rows we update in place each tick (keeps references so the
        # dropdown doesn't flicker or lose state on refresh).
        self.row_5h = rumps.MenuItem("5-hour: \u2014")
        self.row_7d = rumps.MenuItem("7-day: \u2014")
        self.row_credits = rumps.MenuItem("Credits: \u2014")
        self.row_status = rumps.MenuItem("Connecting\u2026")
        self.menu = [
            self.row_5h,
            self.row_7d,
            self.row_credits,
            None,  # separator
            self.row_status,
            rumps.MenuItem("Refresh now", callback=self.refresh_now),
        ]
        # (rumps appends a Quit item automatically.)

        threading.Thread(
            target=fetch_loop,
            args=(self.state, self.stop, self.wake),
            daemon=True,
        ).start()

    def refresh_now(self, _sender) -> None:
        self.wake.set()

    def _set_dot(self, level: str) -> None:
        """Swap the menu bar dot only when its colour bucket changes."""
        if level != self._dot_level:
            self.icon = self._dot_icons[level]
            self._dot_level = level

    @rumps.timer(UI_TICK)
    def tick(self, _timer) -> None:
        """Cheap main-thread refresh of the title + menu from shared state.
        Network never happens here — only the background thread fetches."""
        usage, last_mono, last_wall, error = self.state.snapshot()

        if usage is None:
            self._set_dot("idle")
            self.title = "Claude"
            self.row_status.title = (error or "Connecting\u2026")[:60]
            return

        five = usage.get("five_hour") or {}
        seven = usage.get("seven_day") or {}
        extra = usage.get("extra_usage") or {}

        p5 = float(five.get("utilization") or 0)
        p7 = float(seven.get("utilization") or 0)

        r5 = fmt_reset_short(parse_reset(five.get("resets_at")))
        r7 = fmt_reset_short(parse_reset(seven.get("resets_at")))

        # Menu bar: coloured dot (the status-item image) + plain title of the
        # current (5-hour) window — % and a terse countdown, e.g. "42% · 2h14m".
        # The dropdown keeps the absolute reset time/day. To track the most-
        # constrained window instead, base the dot/title on max(p5, p7).
        cd5 = fmt_countdown(parse_reset(five.get("resets_at")))
        self._set_dot(dot_level(p5))
        self.title = f"{p5:.0f}%" + (f" \u00B7 {cd5}" if cd5 else "")
        self.row_5h.title = f"5-hour: {p5:.0f}%   {r5}".rstrip()
        self.row_7d.title = f"7-day: {p7:.0f}%   {r7}".rstrip()

        if extra.get("is_enabled"):
            used = float(extra.get("used_credits") or 0)
            limit = float(extra.get("monthly_limit") or 0)
            cur = extra.get("currency", "USD")
            self.row_credits.title = (
                f"Credits: {fmt_money(used, cur)} / {fmt_money(limit, cur)}"
            )
        else:
            self.row_credits.title = "Credits: not enabled"

        if error and last_wall:
            self.row_status.title = f"Stale \u2014 {error[:48]}"
        elif last_wall:
            stale = (time.monotonic() - last_mono) > STALE_AFTER
            stamp = dt.datetime.fromtimestamp(last_wall).strftime("%H:%M:%S")
            self.row_status.title = f"Updated {stamp}" + (" (stale)" if stale else "")


if __name__ == "__main__":
    app = QuotaBarApp()
    try:
        app.run()
    finally:
        app.stop.set()
        app.wake.set()
