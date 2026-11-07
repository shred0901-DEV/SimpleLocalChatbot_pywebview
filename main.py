"""SimpleLocalChatbot エントリポイント (pywebview版)。

実行: python main.py
"""
import logging
from pathlib import Path
import sys

import webview

from core.app_diagnostics import enable_diagnostics
from ui.api_bridge import ApiBridge

APP_NAME = "SimpleLocalChatbot"
APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
CHAT_FILE_NAME = "chat.json"
HTML_PATH = (APP_ROOT / "ui" / "web" / "index.html").resolve()


def default_chat_path() -> Path:
    """アプリフォルダ内（data/chat.json）の既定のチャット保存ファイルパスを返す。"""
    return DATA_DIR / CHAT_FILE_NAME


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    chat_path = default_chat_path()
    try:
        enable_diagnostics(chat_path.parent / "logs")
    except OSError:
        logging.exception("診断ログを作成できません。コンソールへの記録を続けます。")

    bridge = ApiBridge(chat_path=chat_path)

    window = webview.create_window(
        title=APP_NAME,
        url=str(HTML_PATH),
        js_api=bridge,
        width=1280,
        height=720,
        min_size=(900, 600),
    )
    bridge.set_window(window)

    def on_closing():
        try:
            bridge.shutdown()
        except Exception:
            logging.exception("終了処理中にエラーが発生しました。")
        return True

    window.events.closing += on_closing

    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
