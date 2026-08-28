"""TradingView Desktop MCP server.

Controls the TradingView desktop app on Windows via UI automation:
window focus, native keyboard shortcuts, mouse input, and window
screenshots returned as images so the model can see and analyze charts.

Transport: stdio. Never print to stdout (it would corrupt the protocol).
"""

import ctypes
import io
import logging
import os
import re
import subprocess
import sys
import time
from typing import Annotated, Literal, Optional

import mss
import psutil
import pyautogui
import pygetwindow as gw
from PIL import Image as PILImage
from pydantic import Field
from mcp.server.fastmcp import FastMCP, Image

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tradingview_mcp")

# Make coordinates match physical pixels on high-DPI displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# Corner fail-safe aborts mid-action when a click lands near (0,0) of a
# maximized window; actions here are short, so disable it.
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.04

mcp = FastMCP("tradingview_mcp")

MAX_IMAGE_WIDTH = 1568  # keep screenshots within the vision sweet spot

DRAW_TOOLS = {
    "trendline": ("alt", "t", 2),
    "horizontal_line": ("alt", "h", 1),
    "horizontal_ray": ("alt", "j", 1),
    "vertical_line": ("alt", "v", 1),
    "cross_line": ("alt", "c", 1),
    "fib_retracement": ("alt", "f", 2),
}

LAUNCH_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\TradingView\TradingView.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\TradingView\TradingView.exe"),
    os.path.expandvars(r"%PROGRAMFILES%\TradingView\TradingView.exe"),
]


# ---------------------------------------------------------------- window utils

class TradingViewNotFound(Exception):
    pass


def _tradingview_pids() -> set[int]:
    pids = set()
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == "tradingview.exe":
                pids.add(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _window_pid(hwnd) -> int:
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _find_window():
    """Return the best TradingView window.

    The desktop app titles its windows after the active chart tab (e.g.
    'BTCUSD 64,608 -1.12% / Unnamed' or 'New tab'), not 'TradingView', so
    windows are matched by owning process. When several exist, prefer the
    focused one, then one whose title looks like a chart, then the largest.
    """
    pids = _tradingview_pids()
    candidates = []
    for w in gw.getAllWindows():
        if w.width < 200 or w.height < 200:
            continue
        if not ctypes.windll.user32.IsWindowVisible(w._hWnd):
            continue
        title = w.title or ""
        if _window_pid(w._hWnd) in pids or "tradingview" in title.lower():
            candidates.append(w)
    if not candidates:
        raise TradingViewNotFound(
            "No TradingView window found. Is the TradingView desktop app running? "
            "Use tradingview_launch to start it, or open it manually and retry."
        )

    def score(w):
        title = w.title or ""
        looks_like_chart = 1 if re.search(r"[%▲▼]|\d", title) and "new tab" not in title.lower() else 0
        return (1 if w.isActive else 0, looks_like_chart, w.width * w.height)

    return max(candidates, key=score)


def _focus():
    """Bring TradingView to the foreground and return the window."""
    w = _find_window()
    try:
        if w.isMinimized:
            w.restore()
            time.sleep(0.4)
        if not w.isActive:
            try:
                w.activate()
            except Exception:
                # Windows blocks SetForegroundWindow without recent input;
                # a synthetic Alt press lifts that restriction.
                pyautogui.press("alt")
                w.activate()
            time.sleep(0.35)
    except Exception as exc:
        raise TradingViewNotFound(f"Could not focus the TradingView window: {exc}") from exc
    return w


def _rect(w):
    return w.left, w.top, w.width, w.height


def _abs_point(w, x_pct: float, y_pct: float):
    left, top, width, height = _rect(w)
    return int(left + width * x_pct / 100.0), int(top + height * y_pct / 100.0)


def _capture(region_pct=None) -> Image:
    """Capture the TradingView window (or a sub-region given in percent)."""
    w = _find_window()
    left, top, width, height = _rect(w)
    if region_pct:
        rx, ry, rw, rh = region_pct
        left = int(left + width * rx / 100.0)
        top = int(top + height * ry / 100.0)
        width = max(1, int(width * rw / 100.0))
        height = max(1, int(height * rh / 100.0))
    with mss.mss() as sct:
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
        img = PILImage.frombytes("RGB", raw.size, raw.rgb)
    if img.width > MAX_IMAGE_WIDTH:
        ratio = MAX_IMAGE_WIDTH / img.width
        img = img.resize((MAX_IMAGE_WIDTH, int(img.height * ratio)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


def _hotkey(*keys: str):
    """Press a key combo with explicit key-down/up timing.

    The TradingView desktop app (Electron) drops modifier combos sent at
    pyautogui.hotkey speed — Alt+T/Alt+H etc. silently do nothing — so hold
    each modifier briefly before and after the final keypress.
    """
    if len(keys) == 1:
        pyautogui.press(keys[0])
        return
    *mods, key = keys
    for m in mods:
        pyautogui.keyDown(m)
    time.sleep(0.15)
    pyautogui.press(key)
    time.sleep(0.1)
    for m in reversed(mods):
        pyautogui.keyUp(m)


def _dismiss_dialogs():
    pyautogui.press("esc")
    time.sleep(0.15)
    pyautogui.press("esc")
    time.sleep(0.15)


def _result(message: str, screenshot: bool = True):
    """Standard action result: status text plus a fresh screenshot."""
    if not screenshot:
        return message
    time.sleep(0.25)
    try:
        return [message, _capture()]
    except Exception as exc:
        return f"{message} (screenshot failed: {exc})"


# --------------------------------------------------------------------- status


@mcp.tool(
    name="tradingview_get_status",
    annotations={
        "title": "Get TradingView Window Status",
        "readOnlyHint": True,
        "openWorldHint": False,
    },
)
def tradingview_get_status() -> str:
    """Check whether the TradingView desktop app is running and report its
    window title, position, size, and focus state.

    Call this first in a session to confirm the app is available before
    using other tools.

    Returns:
        str: Human-readable status summary, or guidance if the app is not running.
    """
    try:
        w = _find_window()
    except TradingViewNotFound as exc:
        return str(exc)
    left, top, width, height = _rect(w)
    return (
        f"TradingView window found.\n"
        f"Title: {w.title}\n"
        f"Position: ({left}, {top})  Size: {width}x{height}\n"
        f"Active (focused): {w.isActive}  Minimized: {w.isMinimized}\n"
        f"Note: click/draw coordinates are percentages of this window "
        f"(0,0 = top-left, 100,100 = bottom-right)."
    )


@mcp.tool(
    name="tradingview_launch",
    annotations={
        "title": "Launch TradingView",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def tradingview_launch(
    wait_seconds: Annotated[int, Field(description="Seconds to wait for the app window to appear after launching (5-60).", ge=5, le=60)] = 15,
) -> str:
    """Launch the TradingView desktop app if it is not already running,
    then wait for its window to appear.

    Returns:
        str: Status message with the window title once available.
    """
    try:
        w = _focus()
        return f"TradingView is already running and focused: {w.title}"
    except TradingViewNotFound:
        pass

    # The app is typically a Microsoft Store package, so launch via its
    # registered deep-link protocol; fall back to known exe locations.
    launched = False
    try:
        os.startfile("tradingview://")
        launched = True
    except OSError:
        for exe in LAUNCH_CANDIDATES:
            if os.path.isfile(exe):
                subprocess.Popen([exe], close_fds=True)
                launched = True
                break
    if not launched:
        return (
            "The tradingview:// protocol is not registered and TradingView.exe was not "
            "found in the usual install locations. Install the TradingView desktop app "
            "from https://www.tradingview.com/desktop/ or start it manually."
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            w = _find_window()
            time.sleep(1.0)
            _focus()
            return f"TradingView launched: {w.title}"
        except TradingViewNotFound:
            time.sleep(1.0)
    return f"Launch command sent, but no TradingView window appeared within {wait_seconds}s. It may still be loading; check tradingview_get_status shortly."


# ----------------------------------------------------------------- screenshots


@mcp.tool(
    name="tradingview_screenshot",
    annotations={
        "title": "Screenshot TradingView Chart",
        "readOnlyHint": True,
        "openWorldHint": False,
    },
)
def tradingview_screenshot(
    region: Annotated[
        Optional[list[float]],
        Field(
            description="Optional sub-region to capture, as [x_pct, y_pct, width_pct, height_pct] "
            "percentages of the window (e.g. [50, 0, 50, 100] = right half). "
            "Omit for the full window. Use a region to zoom into price labels or small details.",
            min_length=4,
            max_length=4,
        ),
    ] = None,
) -> list:
    """Capture the current TradingView window as an image for visual analysis
    (price action, candlestick patterns, indicators, support/resistance, drawings).

    The app is focused first so the capture is not obscured. Use the optional
    region to magnify part of the chart (e.g. the price axis or a candle cluster).

    Returns:
        Image of the chart plus a caption describing what was captured.
    """
    w = _focus()
    time.sleep(0.2)
    if region:
        rx, ry, rw, rh = region
        if not (0 <= rx <= 100 and 0 <= ry <= 100 and 0 < rw <= 100 and 0 < rh <= 100):
            raise ValueError("region values must be percentages within 0-100 (width/height > 0)")
        cap = _capture(region)
        caption = f"Region [{rx},{ry},{rw},{rh}]% of window '{w.title}'"
    else:
        cap = _capture()
        caption = f"Full window '{w.title}' ({w.width}x{w.height})"
    return [caption, cap]


# -------------------------------------------------------------- chart control


@mcp.tool(
    name="tradingview_change_symbol",
    annotations={
        "title": "Change Chart Symbol",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def tradingview_change_symbol(
    symbol: Annotated[str, Field(description="Ticker to load, e.g. 'BTCUSD', 'AAPL', 'BINANCE:BTCUSDT', 'NQ1!', 'EURUSD'. An exchange prefix with ':' is supported.", min_length=1, max_length=40, pattern=r"^[A-Za-z0-9:!._\-/]+$")],
) -> list:
    """Switch the active chart to a different symbol by typing it into
    TradingView's symbol search and pressing Enter.

    The top search match is loaded, so prefer an exchange-qualified symbol
    (e.g. 'BINANCE:BTCUSDT') when ambiguity matters. Returns a screenshot of
    the newly loaded chart — verify the loaded symbol in the image.

    Args:
        symbol: Ticker text to type.

    Returns:
        Confirmation text and a screenshot of the resulting chart.
    """
    sym = symbol.upper()
    base = sym.split(":")[-1]

    # The symbol box sits at a different window-percentage depending on the
    # window's aspect ratio, and Electron eats keystrokes typed before the
    # search dialog mounts — so try several anchors and verify the title.
    CANDIDATES = [(3.2, 4.0), (6.8, 4.2), (4.8, 3.2), (2.4, 5.2)]

    def attempt(px: float, py: float) -> None:
        w = _find_window()
        _focus()
        for _ in range(3):            # clear broker promo / news pane modals
            pyautogui.press("esc")
            time.sleep(0.2)
        pyautogui.click(*_abs_point(w, px, py))
        time.sleep(0.9)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")
        time.sleep(0.5)
        pyautogui.typewrite(sym, interval=0.09)
        time.sleep(1.6)
        pyautogui.press("enter")
        time.sleep(2.2)

    def current() -> str:
        try:
            return _find_window().title.split()[0].upper()
        except Exception:
            return ""

    def matches(t: str) -> bool:
        return bool(t) and (t == base or t.startswith(base[:4]) or base.startswith(t[:4]))

    loaded = current()
    if not matches(loaded):
        for px, py in CANDIDATES:
            attempt(px, py)
            loaded = current()
            if matches(loaded):
                break
    ok = matches(loaded)
    return _result(f"{'Loaded' if ok else 'FAILED to load'} '{sym}' — chart header shows '{loaded}'.")


@mcp.tool(
    name="tradingview_change_timeframe",
    annotations={
        "title": "Change Chart Timeframe",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def tradingview_change_timeframe(
    timeframe: Annotated[str, Field(description="Interval, e.g. '1', '5', '15', '30' (minutes), '1H', '4H' (hours), '1D', '1W', '1M' (day/week/month), '30S' (seconds).", min_length=1, max_length=5)],
) -> list:
    """Change the active chart's timeframe/interval using TradingView's
    keyboard interval entry (type the interval, press Enter).

    Args:
        timeframe: Interval string. Plain numbers are minutes; suffix
            S/H/D/W/M selects seconds/hours/days/weeks/months.

    Returns:
        Confirmation text and a screenshot of the chart on the new interval.
    """
    tf = timeframe.strip().upper()
    if not re.fullmatch(r"\d{1,4}[SHDWM]?|[HDWM]", tf):
        raise ValueError("Invalid timeframe. Examples: '1', '15', '1H', '4H', '1D', '1W', '1M', '30S'.")
    _focus()
    _dismiss_dialogs()
    pyautogui.typewrite(tf, interval=0.05)
    time.sleep(0.4)
    pyautogui.press("enter")
    time.sleep(1.2)
    return _result(f"Set timeframe to {tf}. Verify the interval shown in the chart toolbar.")


@mcp.tool(
    name="tradingview_add_indicator",
    annotations={
        "title": "Add Indicator to Chart",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_add_indicator(
    name: Annotated[str, Field(description="Indicator to search for, e.g. 'RSI', 'MACD', 'Bollinger Bands', 'Volume Profile', 'EMA'. The top search result is added.", min_length=1, max_length=60)],
) -> list:
    """Add an indicator to the active chart via TradingView's indicator
    search ('/' hotkey): types the name, adds the top match, closes the dialog.

    Repeated calls add multiple instances. To configure or remove an
    indicator afterwards, use screenshots plus tradingview_click on its
    legend controls, or tradingview_undo right after adding.

    Args:
        name: Indicator search text.

    Returns:
        Confirmation text and a screenshot — verify the indicator appears in
        the chart legend, since the top search match is selected blindly.
    """
    _focus()
    _dismiss_dialogs()
    pyautogui.press("/")
    time.sleep(1.2)
    pyautogui.typewrite(name, interval=0.03)
    time.sleep(1.2)
    # Nothing is pre-highlighted in the results list; Down selects the top
    # match, then Enter adds it.
    pyautogui.press("down")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(1.0)
    pyautogui.press("esc")
    time.sleep(0.5)
    return _result(f"Searched indicators for '{name}' and added the top match. Check the legend in the screenshot to confirm it is the intended indicator.")


# ------------------------------------------------------------------- drawing


@mcp.tool(
    name="tradingview_draw",
    annotations={
        "title": "Draw on Chart",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_draw(
    tool: Annotated[Literal["trendline", "horizontal_line", "horizontal_ray", "vertical_line", "cross_line", "fib_retracement"], Field(description="Drawing tool to use. trendline and fib_retracement need 2 points; the others need 1.")],
    points: Annotated[
        list[list[float]],
        Field(
            description="Anchor points as [[x_pct, y_pct], ...] — percentages of the WINDOW (0,0 top-left; 100,100 bottom-right). "
            "Take a screenshot first and estimate positions from it; the chart canvas is roughly x 5-92, y 8-92 "
            "(left toolbar, top toolbar, price axis and bottom axis take the edges). "
            "trendline/fib_retracement: [[x1,y1],[x2,y2]]; single-point tools: [[x,y]].",
            min_length=1,
            max_length=2,
        ),
    ],
) -> list:
    """Draw an object on the chart: select the tool via its TradingView
    hotkey, then click the anchor point(s).

    Workflow: 1) tradingview_screenshot to see the chart, 2) estimate anchor
    percentages from the image, 3) draw, 4) inspect the returned screenshot
    and use tradingview_undo if placement is off.

    For fib_retracement, click the swing start first, then the swing end
    (order sets the 0 -> 1 direction).

    Args:
        tool: Which drawing tool.
        points: Window-percentage anchor coordinates.

    Returns:
        Confirmation text and a screenshot showing the drawing.
    """
    mod, key, needed = DRAW_TOOLS[tool]
    if len(points) != needed:
        raise ValueError(f"{tool} needs exactly {needed} point(s), got {len(points)}.")
    for p in points:
        if len(p) != 2 or not (0 <= p[0] <= 100 and 0 <= p[1] <= 100):
            raise ValueError(f"Each point must be [x_pct, y_pct] within 0-100, got {p}.")
    w = _focus()
    _dismiss_dialogs()
    _hotkey(mod, key)
    time.sleep(0.35)
    for p in points:
        x, y = _abs_point(w, p[0], p[1])
        pyautogui.moveTo(x, y, duration=0.15)
        pyautogui.click()
        time.sleep(0.45)
    time.sleep(0.4)
    return _result(f"Drew {tool} at {points} (window %). If misplaced, call tradingview_undo and redraw with adjusted coordinates.")


@mcp.tool(
    name="tradingview_undo",
    annotations={
        "title": "Undo / Redo",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_undo(
    action: Annotated[Literal["undo", "redo"], Field(description="'undo' (Ctrl+Z) or 'redo' (Ctrl+Y).")] = "undo",
    times: Annotated[int, Field(description="How many times to repeat (1-20).", ge=1, le=20)] = 1,
) -> list:
    """Undo or redo the last chart action(s) — drawings, indicator adds, etc.

    Returns:
        Confirmation text and a screenshot of the chart afterwards.
    """
    _focus()
    combo = ("ctrl", "z") if action == "undo" else ("ctrl", "y")
    for _ in range(times):
        _hotkey(*combo)
        time.sleep(0.2)
    return _result(f"Performed {action} x{times}.")


# ----------------------------------------------------------- navigation/view


@mcp.tool(
    name="tradingview_navigate",
    annotations={
        "title": "Pan / Zoom / Reset Chart View",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_navigate(
    action: Annotated[Literal["pan_left", "pan_right", "zoom_in", "zoom_out", "reset_view", "go_to_realtime"], Field(description="pan_left/pan_right scroll the chart horizontally; zoom_in/zoom_out change bar spacing; reset_view = Alt+R; go_to_realtime jumps to the latest bar.")],
    amount: Annotated[int, Field(description="Repeat count for pan/zoom steps (1-30). Ignored for reset_view/go_to_realtime.", ge=1, le=30)] = 5,
) -> list:
    """Navigate the chart view: pan through history, zoom, reset the view,
    or jump back to the current (real-time) bar.

    Returns:
        Confirmation text and a screenshot of the new view.
    """
    w = _focus()
    _dismiss_dialogs()
    cx, cy = _abs_point(w, 50, 45)
    pyautogui.moveTo(cx, cy, duration=0.1)
    if action == "reset_view":
        _hotkey("alt", "r")
        time.sleep(0.5)
    elif action == "go_to_realtime":
        _hotkey("alt", "right")  # jump to present
        time.sleep(0.5)
    elif action in ("pan_left", "pan_right"):
        key = "left" if action == "pan_left" else "right"
        for _ in range(amount):
            pyautogui.press(key)
            time.sleep(0.05)
    else:
        clicks = amount if action == "zoom_in" else -amount
        with pyautogui.hold("ctrl"):
            pyautogui.scroll(clicks * 120, x=cx, y=cy)
        time.sleep(0.4)
    return _result(f"Navigation '{action}' (amount={amount}) done.")


# --------------------------------------------------------- low-level control


@mcp.tool(
    name="tradingview_click",
    annotations={
        "title": "Click Inside TradingView",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_click(
    x_pct: Annotated[float, Field(description="X position as percent of window width (0=left, 100=right).", ge=0, le=100)],
    y_pct: Annotated[float, Field(description="Y position as percent of window height (0=top, 100=bottom).", ge=0, le=100)],
    button: Annotated[Literal["left", "right", "double"], Field(description="left click, right click (context menu), or double click.")] = "left",
) -> list:
    """Click anywhere inside the TradingView window using window-percentage
    coordinates. Escape hatch for anything without a dedicated tool: toolbar
    buttons, legend controls, dialog buttons, context menus, selecting a
    drawing (then tradingview_hotkey 'delete' removes it).

    Take a screenshot first to locate the target; screenshots and clicks use
    the same percentage space.

    Returns:
        Confirmation text and a post-click screenshot.
    """
    w = _focus()
    x, y = _abs_point(w, x_pct, y_pct)
    pyautogui.moveTo(x, y, duration=0.15)
    if button == "double":
        pyautogui.doubleClick()
    elif button == "right":
        pyautogui.rightClick()
    else:
        pyautogui.click()
    time.sleep(0.4)
    return _result(f"{button} click at ({x_pct}%, {y_pct}%) = screen ({x}, {y}).")


@mcp.tool(
    name="tradingview_drag",
    annotations={
        "title": "Drag Inside TradingView",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_drag(
    from_x_pct: Annotated[float, Field(ge=0, le=100, description="Drag start X, percent of window width.")],
    from_y_pct: Annotated[float, Field(ge=0, le=100, description="Drag start Y, percent of window height.")],
    to_x_pct: Annotated[float, Field(ge=0, le=100, description="Drag end X, percent of window width.")],
    to_y_pct: Annotated[float, Field(ge=0, le=100, description="Drag end Y, percent of window height.")],
) -> list:
    """Press-and-drag inside the window: pan the chart by dragging the
    canvas, move a drawing or its anchor, rescale by dragging the price axis,
    or resize panes by dragging their divider.

    Returns:
        Confirmation text and a post-drag screenshot.
    """
    w = _focus()
    x1, y1 = _abs_point(w, from_x_pct, from_y_pct)
    x2, y2 = _abs_point(w, to_x_pct, to_y_pct)
    pyautogui.moveTo(x1, y1, duration=0.15)
    pyautogui.mouseDown()
    time.sleep(0.15)
    pyautogui.moveTo(x2, y2, duration=0.5)
    time.sleep(0.15)
    pyautogui.mouseUp()
    time.sleep(0.4)
    return _result(f"Dragged from ({from_x_pct}%, {from_y_pct}%) to ({to_x_pct}%, {to_y_pct}%).")


@mcp.tool(
    name="tradingview_type_text",
    annotations={
        "title": "Type Text Into TradingView",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_type_text(
    text: Annotated[str, Field(description="Text to type at the current focus (dialog field, text drawing, search box).", min_length=1, max_length=500)],
    press_enter: Annotated[bool, Field(description="Press Enter after typing.")] = False,
) -> list:
    """Type text into whatever currently has keyboard focus in TradingView —
    a dialog input, alert message field, or text annotation. Click the target
    field first with tradingview_click if needed.

    Returns:
        Confirmation text and a screenshot.
    """
    _focus()
    pyautogui.typewrite(text, interval=0.02)
    if press_enter:
        time.sleep(0.2)
        pyautogui.press("enter")
    time.sleep(0.3)
    return _result(f"Typed {len(text)} characters{' and pressed Enter' if press_enter else ''}.")


@mcp.tool(
    name="tradingview_hotkey",
    annotations={
        "title": "Send Keyboard Shortcut",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def tradingview_hotkey(
    keys: Annotated[
        list[str],
        Field(
            description="Keys pressed together as a combo, e.g. ['alt','a'] (create alert), ['alt','s'] (chart snapshot), "
            "['shift','f'] (fullscreen chart), ['delete'] (delete selected drawing), ['esc'], ['ctrl','z']. "
            "pyautogui key names: ctrl, alt, shift, enter, esc, delete, tab, up, down, left, right, f1-f12, a-z, 0-9.",
            min_length=1,
            max_length=4,
        ),
    ],
    times: Annotated[int, Field(description="Repeat count (1-20).", ge=1, le=20)] = 1,
) -> list:
    """Send an arbitrary keyboard shortcut to TradingView. Escape hatch for
    any hotkey without a dedicated tool.

    Useful TradingView shortcuts: Alt+A create alert on visible chart,
    Alt+S chart snapshot dialog, Alt+G go to date, Shift+F fullscreen chart,
    Alt+W add symbol to watchlist, Delete removes the selected drawing,
    Tab/arrow keys navigate dialogs.

    Returns:
        Confirmation text and a screenshot.
    """
    _focus()
    for _ in range(times):
        _hotkey(*keys)
        time.sleep(0.25)
    return _result(f"Sent {'+'.join(keys)} x{times}.")


# ------------------------------------------------------------- alerts/layout


@mcp.tool(
    name="tradingview_open_alert_dialog",
    annotations={
        "title": "Open Create-Alert Dialog",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def tradingview_open_alert_dialog() -> list:
    """Open TradingView's Create Alert dialog (Alt+A) for the current symbol.

    The dialog opens pre-filled; inspect the returned screenshot, then use
    tradingview_click / tradingview_type_text to adjust condition, price, and
    message, and click the dialog's Create button to save (or Esc to cancel).

    Returns:
        Confirmation text and a screenshot of the open dialog.
    """
    _focus()
    _dismiss_dialogs()
    _hotkey("alt", "a")
    time.sleep(1.0)
    return _result("Opened the Create Alert dialog (Alt+A). Configure fields with click/type tools, then click Create — or send Esc to cancel.")


@mcp.tool(
    name="tradingview_save_layout",
    annotations={
        "title": "Save Chart Layout",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def tradingview_save_layout() -> list:
    """Save the current chart layout (Ctrl+S), preserving symbols, intervals,
    indicators, and drawings.

    Returns:
        Confirmation text and a screenshot.
    """
    _focus()
    _hotkey("ctrl", "s")
    time.sleep(0.8)
    return _result("Sent Ctrl+S to save the current layout. If a naming dialog appeared (first save), fill it via type/click tools.")


if __name__ == "__main__":
    log.info("Starting tradingview_mcp (stdio)")
    mcp.run()
