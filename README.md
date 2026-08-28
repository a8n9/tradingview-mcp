# TradingView Desktop MCP Server

MCP server that gives Claude full control of the **TradingView desktop app on Windows** through UI automation — no TradingView API needed. Claude can see the chart (screenshots are returned as images it analyzes visually), switch symbols and timeframes, add indicators, draw trendlines/fibs/levels, pan and zoom, open the alert dialog, and fall back to raw click/drag/type/hotkey control for everything else.

## How it works

The TradingView desktop app has no public automation API, so this server drives the real app:

- Finds and focuses the TradingView window (`pygetwindow`)
- Sends TradingView's native keyboard shortcuts and mouse input (`pyautogui`)
- Captures the window with `mss` and returns PNG images over MCP, so the model can *see* the chart and verify every action

All coordinates are **percentages of the TradingView window** (0,0 = top-left, 100,100 = bottom-right), so screenshots and clicks share the same coordinate space regardless of window size.

## Tools (15)

| Tool | What it does |
|---|---|
| `tradingview_get_status` | Is the app running? Window title/size/focus |
| `tradingview_launch` | Start the app if not running |
| `tradingview_screenshot` | Capture window (or zoomed sub-region) for analysis |
| `tradingview_change_symbol` | Load a ticker (`BINANCE:BTCUSDT`, `AAPL`, `NQ1!`…) |
| `tradingview_change_timeframe` | `1`, `15`, `1H`, `4H`, `1D`, `1W`… |
| `tradingview_add_indicator` | Add RSI, MACD, Bollinger Bands, etc. via indicator search |
| `tradingview_draw` | trendline, horizontal line/ray, vertical line, cross line, fib retracement |
| `tradingview_undo` | Undo/redo drawings and actions |
| `tradingview_navigate` | Pan, zoom, reset view, jump to real-time bar |
| `tradingview_click` | Click anything (toolbars, legends, dialogs, context menus) |
| `tradingview_drag` | Pan chart, move drawings, rescale axes |
| `tradingview_type_text` | Type into dialogs / text annotations |
| `tradingview_hotkey` | Any keyboard shortcut (Alt+A alert, Alt+S snapshot, Shift+F fullscreen…) |
| `tradingview_open_alert_dialog` | Open the pre-filled Create Alert dialog |
| `tradingview_save_layout` | Ctrl+S save layout |

Every action tool returns a fresh screenshot so Claude verifies the result and self-corrects.

## Setup

```powershell
git clone https://github.com/a8n9/tradingview-mcp
cd tradingview-mcp
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "C:\\Users\\YOU\\tradingview-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\YOU\\tradingview-mcp\\server.py"]
    }
  }
}
```

### Claude Code

```powershell
claude mcp add --scope user tradingview -- C:\Users\YOU\tradingview-mcp\.venv\Scripts\python.exe C:\Users\YOU\tradingview-mcp\server.py
```

## Notes & limitations

- **Windows only**, and the TradingView desktop app must be installed (https://www.tradingview.com/desktop/).
- The server takes over your mouse/keyboard briefly during actions and brings TradingView to the foreground — don't type while it's mid-action.
- Actions rely on TradingView's default keyboard shortcuts; if you've remapped them, the corresponding tools will misbehave.
- Symbol/indicator selection takes the **top search result** — Claude verifies via the returned screenshot, but exchange-prefixed symbols (`BINANCE:BTCUSDT`) are more reliable.
- This automates the *chart UI* only. It never touches broker/trading panels — don't ask it to place orders.
