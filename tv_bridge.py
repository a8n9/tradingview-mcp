"""Direct TradingView control bridge — full command surface, zero MCP.

  .venv\\Scripts\\python.exe tv_bridge.py <command> [args...]

status | launch | shot <out.png> [x y w h] | symbol <T> | tf <TF> | indicator <N>
click <x%> <y%> [left|right|double] | drag <x1> <y1> <x2> <y2> | type <text>
hotkey <k1+k2> | draw <tool> <x1> <y1> [x2] [y2] | undo [n] | redo [n]
nav <pan_left|pan_right|zoom_in|zoom_out|reset_view|go_to_realtime> [amount]
alert | save | axis   (axis = calibrated price<->pixel map as JSON)
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

import server  # noqa: E402


def save_image(img, path: str) -> None:
    with open(path, "wb") as f:
        f.write(img.data)


def unpack(result, shot_path: str | None = None) -> str:
    if isinstance(result, list):
        msg = next((x for x in result if isinstance(x, str)), "")
        if shot_path:
            img = next((x for x in result if not isinstance(x, str)), None)
            if img is not None:
                save_image(img, shot_path)
                msg += f" [screenshot: {shot_path}]"
        return msg
    return str(result)


def detect_gridlines(png_path: str, x0f=0.10, x1f=0.55) -> list:
    """Y pixel rows of horizontal gridlines inside the chart canvas."""
    im = PILImage.open(png_path).convert("L")
    a = np.asarray(im, dtype=np.int16)
    h, w = a.shape
    strip = a[int(h * 0.06):int(h * 0.92), int(w * x0f):int(w * x1f)]
    rowmean = strip.mean(axis=1)
    rowstd = strip.std(axis=1)
    base = np.median(rowmean)
    # gridlines: slightly brighter than background AND very uniform across x
    cand = np.where((rowmean > base + 1.2) & (rowmean < base + 14) & (rowstd < 12))[0]
    rows, last = [], -99
    for r in cand:
        if r - last > 6:
            rows.append(int(r + h * 0.06))
        last = r
    return rows


def main() -> None:
    cmd, *args = sys.argv[1:] or ["status"]

    if cmd == "status":
        print(server.tradingview_get_status())
    elif cmd == "launch":
        print(server.tradingview_launch())
    elif cmd == "shot":
        out = args[0]
        region = [float(a) for a in args[1:5]] if len(args) >= 5 else None
        save_image(server._capture(region), out)
        print(f"saved {out} | window: {server._find_window().title}")
    elif cmd == "symbol":
        print(unpack(server.tradingview_change_symbol(args[0])))
    elif cmd == "tf":
        print(unpack(server.tradingview_change_timeframe(args[0])))
    elif cmd == "indicator":
        print(unpack(server.tradingview_add_indicator(" ".join(args))))
    elif cmd == "click":
        btn = args[2] if len(args) > 2 else "left"
        print(unpack(server.tradingview_click(float(args[0]), float(args[1]), btn)))
    elif cmd == "drag":
        print(unpack(server.tradingview_drag(*[float(a) for a in args[:4]])))
    elif cmd == "type":
        print(unpack(server.tradingview_type_text(" ".join(args))))
    elif cmd == "hotkey":
        print(unpack(server.tradingview_hotkey(args[0].split("+"))))
    elif cmd == "draw":
        tool = args[0]
        nums = [float(a) for a in args[1:]]
        pts = [[nums[0], nums[1]]] if len(nums) == 2 else [[nums[0], nums[1]], [nums[2], nums[3]]]
        print(unpack(server.tradingview_draw(tool, pts)))
    elif cmd in ("undo", "redo"):
        times = int(args[0]) if args else 1
        print(unpack(server.tradingview_undo(cmd, times)))
    elif cmd == "nav":
        amount = int(args[1]) if len(args) > 1 else 5
        print(unpack(server.tradingview_navigate(args[0], amount)))
    elif cmd == "alert":
        print(unpack(server.tradingview_open_alert_dialog()))
    elif cmd == "save":
        print(unpack(server.tradingview_save_layout()))
    elif cmd == "axis":
        import tv_axis
        tmp = args[0] if args else os.path.join(tempfile.gettempdir(), "tv_axis.png")
        tv_axis.capture(tmp)
        info = tv_axis.anchors(tmp)
        info["shot"] = tmp
        print(json.dumps(info))
    else:
        print(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
