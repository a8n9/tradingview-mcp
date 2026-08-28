"""Calibrate TradingView's price axis: map price <-> window y-percent.

Method that survives theme/layout changes: find the top-most and bottom-most
candle pixels on the canvas (saturated red/green), then read the price-axis
labels sitting at those same rows. Two exact (price, y) pairs -> linear scale.

  python tv_axis.py            -> JSON with anchor rows and axis strip bounds
"""
import json
import os
import sys
import tempfile

import numpy as np
from PIL import Image as PILImage

import server

sys.stdout.reconfigure(encoding="utf-8")


def capture(path: str) -> str:
    img = server._capture()
    with open(path, "wb") as f:
        f.write(img.data)
    return path


def anchors(png: str) -> dict:
    """Topmost / bottommost candle rows + where the price axis starts."""
    im = PILImage.open(png).convert("RGB")
    a = np.asarray(im).astype(int)
    h, w = a.shape[:2]
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = (mx - mn) > 45
    green = sat & (g > r + 25) & (g > 80)
    red = sat & (r > g + 45) & (r > 90)
    candle = green | red

    # candles live left of the price axis and below the toolbar
    canvas = candle.copy()
    canvas[: int(h * 0.07), :] = False
    canvas[int(h * 0.93):, :] = False
    canvas[:, int(w * 0.80):] = False          # exclude axis + side panel
    canvas[:, : int(w * 0.02)] = False         # exclude left toolbar
    # exclude the legend / SELL-BUY badges that sit over the top-left canvas
    canvas[: int(h * 0.13), : int(w * 0.32)] = False

    cols = np.where(canvas.sum(axis=0) > 0)[0]
    rows = np.where(canvas.sum(axis=1) > 0)[0]
    if len(rows) < 10 or len(cols) < 10:
        return {"ok": False, "reason": "no candles detected", "img_h": h, "img_w": w}

    # ignore stray 1-2 px noise: require a few candle pixels on the row
    counts = canvas.sum(axis=1)
    solid = np.where(counts >= 2)[0]
    y_top, y_bot = int(solid.min()), int(solid.max())
    x_axis = int(cols.max()) + 4

    # the current-price badge on the axis: a solid run of vivid pixels
    vivid = ((mx - mn) > 70) & (mx > 110)
    y_cur = None
    for y in range(int(h * 0.05), int(h * 0.95)):
        xs = np.where(vivid[y])[0]
        xs = xs[xs > x_axis - 2]
        if len(xs) < 12:
            continue
        start, prev, best = xs[0], xs[0], 0
        for x in list(xs[1:]) + [10 ** 9]:
            if x - prev > 2:
                best = max(best, prev - start)
                start = x
            prev = x
        if best >= 12:
            y_cur = y
            break

    return {"ok": True, "img_h": h, "img_w": w,
            "y_top": y_top, "y_top_pct": round(100 * y_top / h, 2),
            "y_bot": y_bot, "y_bot_pct": round(100 * y_bot / h, 2),
            "y_cur_pct": None if y_cur is None else round(100 * y_cur / h, 2),
            "axis_x_pct": round(100 * x_axis / w, 2),
            "candle_x_pct": [round(100 * cols.min() / w, 2), round(100 * cols.max() / w, 2)]}


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "tv_axis.png")
    capture(p)
    print(json.dumps(anchors(p)))
