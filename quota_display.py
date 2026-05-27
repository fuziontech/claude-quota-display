#!/usr/bin/env python3
"""Fullscreen Claude quota display for a 640x480 screen (Raspberry Pi kiosk).

Big horizontal bars + clock. Data is fetched in a background thread so the
clock stays smooth and a network hiccup never freezes the screen.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time

import pygame

import quota_api

# ---- layout / theme -------------------------------------------------------
WIDTH, HEIGHT = 640, 480
PAD = 28
POLL_INTERVAL = 60          # seconds between usage fetches
STALE_AFTER = 5 * 60        # seconds before cached data is flagged stale

BG = (16, 18, 24)
FG = (235, 237, 243)
MUTED = (120, 126, 140)
TRACK = (38, 42, 52)
ACCENT = (210, 120, 70)     # Claude-ish terracotta for the title

# utilization thresholds -> bar colour
GREEN = (60, 190, 120)
YELLOW = (220, 200, 70)
ORANGE = (230, 150, 60)
RED = (225, 80, 80)


def bar_color(pct: float) -> tuple[int, int, int]:
    if pct >= 95:
        return RED
    if pct >= 80:
        return ORANGE
    if pct >= 50:
        return YELLOW
    return GREEN


# ---- shared state between fetch thread and render loop --------------------
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.usage: dict | None = None
        self.last_success: float = 0.0       # wall clock, for display
        self.last_success_mono: float = 0.0  # monotonic, for age/staleness
        self.error: str | None = None

    def update_ok(self, usage: dict) -> None:
        with self.lock:
            self.usage = usage
            self.last_success = time.time()
            self.last_success_mono = time.monotonic()
            self.error = None

    def update_err(self, msg: str) -> None:
        with self.lock:
            self.error = msg

    def snapshot(self):
        with self.lock:
            return self.usage, self.last_success, self.last_success_mono, self.error


def fetch_loop(state: State, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            state.update_ok(quota_api.fetch_usage())
        except Exception as exc:  # noqa: BLE001 - keep the display alive
            state.update_err(str(exc))
        stop.wait(POLL_INTERVAL)


# ---- formatting helpers ---------------------------------------------------
def parse_reset(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return None


def fmt_reset_short(when: dt.datetime | None) -> str:
    if when is None:
        return ""
    now = dt.datetime.now().astimezone()
    if when.date() == now.date():
        return f"resets {when:%H:%M}"
    if (when.date() - now.date()).days == 1:
        return f"resets tomorrow {when:%H:%M}"
    # Build the day without %-d, which is glibc-only.
    return f"resets {when:%a %b} {when.day}"


# ---- drawing --------------------------------------------------------------
class Display:
    def __init__(self, screen: pygame.Surface) -> None:
        self.screen = screen
        self.f_title = self._font(40, bold=True)
        self.f_clock = self._font(40, bold=True)
        self.f_label = self._font(26, bold=True)
        self.f_pct = self._font(30, bold=True)
        self.f_small = self._font(20)
        self.f_tiny = self._font(16)

    @staticmethod
    def _font(size: int, bold: bool = False) -> pygame.font.Font:
        return pygame.font.SysFont("dejavusans", size, bold=bold)

    def _text(self, surf_font, text, color, x, y, right=False, center=False):
        img = surf_font.render(text, True, color)
        rect = img.get_rect()
        if right:
            rect.topright = (x, y)
        elif center:
            rect.midtop = (x, y)
        else:
            rect.topleft = (x, y)
        self.screen.blit(img, rect)
        return rect

    def _bar(self, x, y, w, h, pct):
        pct = max(0.0, min(100.0, pct))
        radius = h // 2
        pygame.draw.rect(self.screen, TRACK, (x, y, w, h), border_radius=radius)
        fill_w = int(w * pct / 100)
        if fill_w >= h:
            pygame.draw.rect(self.screen, bar_color(pct), (x, y, fill_w, h),
                             border_radius=radius)
        elif fill_w > 0:
            pygame.draw.rect(self.screen, bar_color(pct), (x, y, fill_w, h),
                             border_radius=min(radius, fill_w // 2 or 1))

    def quota_block(self, y, label, pct, reset_text):
        self._text(self.f_label, label, FG, PAD, y)
        self._text(self.f_pct, f"{pct:.0f}%", bar_color(pct), WIDTH - PAD, y - 2,
                   right=True)
        self._bar(PAD, y + 36, WIDTH - 2 * PAD, 26, pct)
        if reset_text:
            self._text(self.f_small, reset_text, MUTED, PAD, y + 70)

    @staticmethod
    def signature(state: State) -> tuple:
        """A cheap fingerprint of everything on screen, so the main loop can
        skip the redraw+flip when nothing has changed (keeps CPU near idle)."""
        usage, last_success, last_success_mono, error = state.snapshot()
        now = dt.datetime.now().astimezone()
        stale = bool(last_success_mono) and (
            time.monotonic() - last_success_mono > STALE_AFTER
        )
        return (now.strftime("%H:%M:%S"), last_success, error, usage is None, stale)

    def render(self, state: State) -> None:
        usage, last_success, last_success_mono, error = state.snapshot()
        self.screen.fill(BG)

        # header
        now = dt.datetime.now().astimezone()
        self._text(self.f_title, "CLAUDE", ACCENT, PAD, PAD - 6)
        self._text(self.f_clock, now.strftime("%H:%M:%S"), FG, WIDTH - PAD, PAD - 6,
                   right=True)
        pygame.draw.line(self.screen, TRACK, (PAD, PAD + 48),
                         (WIDTH - PAD, PAD + 48), 2)

        if usage is None:
            msg = (error or "connecting...")[:42]
            self._text(self.f_label, msg, MUTED, WIDTH // 2, HEIGHT // 2 - 20,
                       center=True)
            return

        five = usage.get("five_hour") or {}
        seven = usage.get("seven_day") or {}
        extra = usage.get("extra_usage") or {}

        self.quota_block(
            108, "5-HOUR",
            float(five.get("utilization") or 0),
            fmt_reset_short(parse_reset(five.get("resets_at"))),
        )
        self.quota_block(
            228, "7-DAY",
            float(seven.get("utilization") or 0),
            fmt_reset_short(parse_reset(seven.get("resets_at"))),
        )

        # credits
        if extra.get("is_enabled"):
            used = float(extra.get("used_credits") or 0)
            limit = float(extra.get("monthly_limit") or 0)
            cur = extra.get("currency", "USD")
            sym = "$" if cur == "USD" else f"{cur} "
            self._text(self.f_label, "CREDITS", FG, PAD, 348)
            self._text(self.f_pct, f"{sym}{used:.0f} / {sym}{limit:.0f}", FG,
                       WIDTH - PAD, 346, right=True)
            pct = (used / limit * 100) if limit else 0
            self._bar(PAD, 384, WIDTH - 2 * PAD, 18, pct)

        # status footer
        if error and last_success:
            footer = f"stale - {error[:46]}"
            color = ORANGE
        elif last_success:
            age = time.monotonic() - last_success_mono
            stale = age > STALE_AFTER
            footer = f"updated {dt.datetime.fromtimestamp(last_success):%H:%M:%S}"
            if stale:
                footer += "  (stale)"
            color = ORANGE if stale else MUTED
        else:
            footer = ""
            color = MUTED
        if footer:
            self._text(self.f_tiny, footer, color, WIDTH // 2, HEIGHT - 28,
                       center=True)


def main() -> None:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    pygame.init()
    pygame.mouse.set_visible(False)
    # No SCALED: the window already matches the native 640x480, so scaling
    # would just burn CPU on a per-frame blit.
    screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.FULLSCREEN)
    pygame.display.set_caption("Claude Quota")

    state = State()
    stop = threading.Event()
    thread = threading.Thread(target=fetch_loop, args=(state, stop), daemon=True)
    thread.start()

    display = Display(screen)
    clock = pygame.time.Clock()
    last_sig = None
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (
                pygame.K_ESCAPE, pygame.K_q,
            ):
                running = False
        # Only repaint when something visible actually changed (the clock ticks
        # once a second), so the loop is effectively idle the rest of the time.
        sig = display.signature(state)
        if sig != last_sig:
            display.render(state)
            pygame.display.flip()
            last_sig = sig
        clock.tick(10)  # poll for input/clock changes 10x/s; redraw is gated

    stop.set()
    pygame.quit()


if __name__ == "__main__":
    main()
