"""pywebview JS API Bridge connecting HTML/JS UI with ChatService and ChatStore."""
import base64
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import threading
import time
import webbrowser

from PIL import Image

from config.chat_store import ChatStore
from core.chat import (
    ChatConversation,
    ChatSettings,
    ChatTurn,
    MAX_TURNS,
    conversation,
    server_url,
)
from core.chat_images import (
    IMAGE_ONLY_PROMPT,
    MAX_IMAGES,
    MAX_PIXELS,
    normalize_image,
)
from core.chat_search import valid_source_url
from services.chat_service import ChatService
from services.ollama_account_service import OllamaAccountService


class ApiBridge:
    def __init__(self, chat_path: Path):
        # pywebviewの再帰走査を防ぐため、内部オブジェクト・プロパティは必ずアンダースコア(_)で始める
        self._window = None
        self._chat_path = chat_path
        self._store = ChatStore(chat_path)
        self._service = ChatService()
        self._account_service = OllamaAccountService()

        self._connected_endpoint = ""
        self._models = []
        self._connecting = False
        self._current_turn = None
        self._used_search = False
        self._credit_pending = False
        self._thumbnail_cache = {}  # image_id -> base64_str
        self._lock = threading.Lock()
        self._save_timer = None
        self._closed = False

        # クレジット表示用キャッシュ
        self._credit_balance = "残量: 未取得"
        self._credit_usage = "消費量: 未取得"
        self._credit_status = ""
        self._status_text = "起動時にOllamaへ自動接続します。"
        self._save_status_text = self._store.error

        # シグナル接続
        self._service.modelsReady.connect(self._on_models_ready)
        self._service.connectionFailed.connect(self._on_connection_failed)
        self._service.connectingChanged.connect(self._on_connecting_changed)
        self._service.textReceived.connect(self._on_text_received)
        self._service.sourcesReady.connect(self._on_sources_ready)
        self._service.result.connect(self._on_result)
        self._service.status.connect(self._on_status)
        self._service.busyChanged.connect(self._on_busy_changed)

        self._account_service.updated.connect(self._on_credit_updated)
        self._account_service.failed.connect(self._on_credit_failed)
        self._account_service.busyChanged.connect(self._on_credit_busy_changed)

    def set_window(self, window):
        self._window = window

    def _notify(self, func_name: str, *args):
        if self._closed or not self._window:
            return
        args_json = ", ".join(json.dumps(a, ensure_ascii=False) for a in args)
        js = f"window.app && window.app.{func_name} && window.app.{func_name}({args_json});"
        try:
            self._window.evaluate_js(js)
        except Exception:
            logging.debug("evaluate_js failed (window may be closing)", exc_info=True)

    def _get_image_base64(self, image_id: str) -> str:
        with self._lock:
            if image_id in self._thumbnail_cache:
                return self._thumbnail_cache[image_id]
        try:
            raw = self._store.images.read(image_id)
            with Image.open(BytesIO(raw)) as img:
                img.thumbnail((200, 120), Image.Resampling.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="PNG")
                b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                with self._lock:
                    if len(self._thumbnail_cache) >= 64:
                        self._thumbnail_cache.pop(next(iter(self._thumbnail_cache)))
                    self._thumbnail_cache[image_id] = b64
                return b64
        except Exception:
            return ""

    def _schedule_save(self):
        if self._closed:
            return
        if self._save_timer and self._save_timer.is_alive():
            self._save_timer.cancel()
        self._save_timer = threading.Timer(0.5, self.flush)
        self._save_timer.daemon = True
        self._save_timer.start()

    def flush(self) -> bool:
        success = self._store.save()
        self._save_status_text = self._store.error
        if not self._closed:
            self._notify("onSaveStatus", self._save_status_text)
        return success

    # --- JS から呼び出される API ---

    def get_initial_state(self):
        """起動時の全状態をフロントエンドに返す。"""
        conversations_data = [
            {
                "id": c.id,
                "title": c.title,
                "turns_count": len(c.turns),
            }
            for c in reversed(self._store.conversations)
        ]

        turns_data = []
        for t in self._store.turns:
            images_data = []
            for img in t.images:
                images_data.append({
                    "id": img.id,
                    "name": img.name,
                    "width": img.width,
                    "height": img.height,
                    "thumbnail": self._get_image_base64(img.id),
                })
            turns_data.append({
                "user": t.user,
                "model": t.model,
                "assistant": t.assistant,
                "state": t.state,
                "error": t.error,
                "sources": t.sources,
                "images": images_data,
            })

        draft_images_data = []
        for img in self._store.active.draft_images:
            draft_images_data.append({
                "id": img.id,
                "name": img.name,
                "width": img.width,
                "height": img.height,
                "thumbnail": self._get_image_base64(img.id),
            })

        return {
            "conversations": conversations_data,
            "active_id": self._store.active_id,
            "draft": self._store.draft,
            "draft_images": draft_images_data,
            "turns": turns_data,
            "settings": {
                "endpoint": self._store.settings.endpoint,
                "model": self._store.settings.model,
                "num_ctx": self._store.settings.num_ctx,
                "web_search": self._store.settings.web_search,
            },
            "models": self._models,
            "connected_endpoint": self._connected_endpoint,
            "credit": {
                "balance": self._credit_balance,
                "usage": self._credit_usage,
                "status": self._credit_status,
                "can_refresh": time.monotonic() >= self._account_service.next_refresh,
            },
            "status": self._status_text,
            "save_status": self._save_status_text,
            "busy": self._service.is_running,
            "can_cancel": self._service.can_cancel,
            "release_failed": self._service.release_failed,
        }

    def start_auto_connect(self):
        """起動直後の Ollama 自動接続・モデル読み込み。"""
        self.refresh_models(self._store.settings.endpoint)

    def select_conversation(self, conversation_id: str):
        if self._service.is_running or self._connecting or self._store.read_failed:
            return False
        if conversation_id == self._store.active_id:
            return True
        if not self.flush():
            return False

        previous = self._store.active_id
        self._store.active_id = conversation_id
        if not self._store.save():
            self._store.active_id = previous
            self._save_status_text = self._store.error
            self._notify("onSaveStatus", self._save_status_text)
            return False

        self._save_status_text = ""
        self._notify("onConversationChanged", self.get_initial_state())
        return True

    def new_conversation(self):
        if self._service.is_running or self._connecting or self._store.read_failed:
            return False
        if not self.flush() or not (self._store.turns or self._store.draft or self._store.active.draft_images):
            return False

        previous = self._store.active_id
        entry = ChatConversation()
        self._store.conversations.append(entry)
        self._store.active_id = entry.id
        if not self._store.save():
            self._store.conversations.pop()
            self._store.active_id = previous
            self._save_status_text = self._store.error
            self._notify("onSaveStatus", self._save_status_text)
            return False

        self._notify("onConversationChanged", self.get_initial_state())
        return True

    def update_draft(self, text: str):
        self._store.draft = text
        self._schedule_save()

    def update_settings(self, endpoint: str, model: str, num_ctx: int, web_search: bool):
        self._store.settings.endpoint = endpoint
        self._store.settings.model = model
        self._store.settings.num_ctx = num_ctx
        self._store.settings.web_search = web_search
        self._schedule_save()

    def refresh_models(self, endpoint: str):
        endpoint = server_url(endpoint)
        self._store.settings.endpoint = endpoint
        self._schedule_save()
        return self._service.refresh_models(endpoint)

    def refresh_credits(self):
        return self._account_service.refresh()

    def choose_images(self):
        """pywebview のネイティブファイル選択ダイアログを開く。"""
        if not self._window:
            return
        if self._service.is_running or self._connecting:
            self._status_text = "画像の添付は応答やモデル解放が終了してから行ってください。"
            self._notify("onStatus", self._status_text)
            return

        file_types = ("画像ファイル (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)", "すべてのファイル (*.*)")
        paths = self._window.create_file_dialog(
            dialog_type=0,  # OPEN_DIALOG
            allow_multiple=True,
            file_types=file_types,
        )
        if not paths:
            return

        self._add_image_files(list(paths))

    def add_image_base64(self, name: str, base64_str: str):
        """クリップボード貼り付けやドラッグ＆ドロップで渡された画像を保存。"""
        if self._service.is_running or self._connecting:
            self._status_text = "画像の添付は応答やモデル解放が終了してから行ってください。"
            self._notify("onStatus", self._status_text)
            return

        try:
            if "," in base64_str:
                base64_str = base64_str.split(",", 1)[1]
            data = base64.b64decode(base64_str)
            self._add_image_data(data, name or "貼り付け画像")
        except Exception as exc:
            self._status_text = f"添付画像を追加できません: {exc}"
            self._notify("onStatus", self._status_text)

    def _add_image_files(self, paths):
        for path_str in paths:
            path = Path(path_str)
            current_images = list(self._store.active.draft_images)
            if len(current_images) >= MAX_IMAGES:
                self._status_text = f"添付画像は最大{MAX_IMAGES}枚までです。"
                self._notify("onStatus", self._status_text)
                return
            try:
                image_obj = self._store.images.add_file(path)
                current_images.append(image_obj)
                self._store.active.draft_images = current_images
                self.flush()
            except Exception as exc:
                self._status_text = f"画像ファイル {path.name} を読み込めません: {exc}"
                self._notify("onStatus", self._status_text)
                return

        draft_images_data = [
            {
                "id": img.id,
                "name": img.name,
                "width": img.width,
                "height": img.height,
                "thumbnail": self._get_image_base64(img.id),
            }
            for img in self._store.active.draft_images
        ]
        self._notify("onDraftImagesChanged", draft_images_data)

    def _add_image_data(self, data: bytes, name: str):
        current_images = list(self._store.active.draft_images)
        if len(current_images) >= MAX_IMAGES:
            self._status_text = f"添付画像は最大{MAX_IMAGES}枚までです。"
            self._notify("onStatus", self._status_text)
            return

        try:
            image_obj = self._store.images.add_data(data, name)
            current_images.append(image_obj)
            self._store.active.draft_images = current_images
            self.flush()

            # UIに反映
            draft_images_data = [
                {
                    "id": img.id,
                    "name": img.name,
                    "width": img.width,
                    "height": img.height,
                    "thumbnail": self._get_image_base64(img.id),
                }
                for img in current_images
            ]
            self._notify("onDraftImagesChanged", draft_images_data)
        except Exception as exc:
            self._status_text = str(exc)
            self._notify("onStatus", self._status_text)

    def remove_image(self, index: int):
        current_images = list(self._store.active.draft_images)
        if 0 <= index < len(current_images):
            current_images.pop(index)
            self._store.active.draft_images = current_images
            self.flush()
            draft_images_data = [
                {
                    "id": img.id,
                    "name": img.name,
                    "width": img.width,
                    "height": img.height,
                    "thumbnail": self._get_image_base64(img.id),
                }
                for img in current_images
            ]
            self._notify("onDraftImagesChanged", draft_images_data)

    def send_message(self, text: str):
        if self._service.is_running or self._connecting or self._store.read_failed:
            return False

        text = text.strip()
        images = list(self._store.active.draft_images)
        if not text and not images:
            return False

        try:
            settings = ChatSettings(
                server_url(self._store.settings.endpoint),
                self._store.settings.model,
                self._store.settings.num_ctx,
                web_search=self._store.settings.web_search,
            )
            if settings.web_search and images and not text:
                raise ValueError("Web検索も使う場合は、検索したい内容を質問文として入力してください。")
            if settings.endpoint != self._connected_endpoint or settings.model not in self._models:
                raise ValueError("接続確認を行い、ダウンロード済みのモデルを選択してください。")
            if len(self._store.turns) >= MAX_TURNS:
                raise ValueError("会話履歴が100件になりました。「新しい会話」で会話を始め直してください。")

            messages, omitted = conversation(self._store.turns, text, settings.num_ctx, images)
            messages = self._store.images.prepare_messages(messages)

            self._current_turn = ChatTurn(
                user=text or IMAGE_ONLY_PROMPT,
                model=settings.model,
                images=images,
            )
            self._store.turns.append(self._current_turn)
            self._store.active.draft_images = []
            self._store.draft = ""

            if not self.flush():
                self._store.turns.pop()
                self._current_turn = None
                self._store.active.draft_images = images
                self._store.draft = text
                return False

            self._used_search = settings.web_search
            self._notify("onMessageSent", self.get_initial_state())

            self._service.start(settings, messages)
            if omitted and self._service.is_active and not settings.web_search:
                self._status_text = f"応答を生成しています…（古い{omitted}件のやり取りは今回の送信対象から外しています）"
                self._notify("onStatus", self._status_text)

            return True

        except Exception as exc:
            self._status_text = str(exc)
            self._notify("onStatus", self._status_text)
            return False

    def cancel(self):
        self._service.cancel()

    def retry_release(self):
        self._service.retry_release()

    def open_external_url(self, url: str):
        if valid_source_url(url):
            webbrowser.open(url)

    # --- サービスからのシグナル受信ハンドラ ---

    def _on_models_ready(self, names):
        self._models = names
        self._connected_endpoint = server_url(self._store.settings.endpoint)
        if names:
            if not self._store.settings.model or self._store.settings.model not in names:
                self._store.settings.model = names[0]
            self._status_text = f"Ollamaに接続しました。{len(names)}個のモデルを利用できます。"
        else:
            self._status_text = "ダウンロード済みのローカルモデルがありません。PowerShellで ollama pull gemma4:e4b を実行してください。"

        self._notify("onModelsReady", {
            "models": self._models,
            "selected_model": self._store.settings.model,
            "connected_endpoint": self._connected_endpoint,
            "status": self._status_text,
        })
        # クレジット情報の自動更新
        if os.environ.get("OLLAMA_API_KEY"):
            self.refresh_credits()

    def _on_connection_failed(self, error):
        self._status_text = error
        self._notify("onConnectionFailed", error)

    def _on_connecting_changed(self, connecting):
        self._connecting = connecting
        self._notify("onConnectingChanged", connecting)

    def _on_text_received(self, text):
        if self._current_turn is not None:
            self._current_turn.assistant += text
            self._notify("onStreamText", text)
            self._schedule_save()

    def _on_sources_ready(self, sources):
        if self._current_turn is not None:
            self._current_turn.sources = sources
            self.flush()
            self._notify("onSourcesReady", sources)

    def _on_result(self, state, text, error):
        if self._current_turn is not None:
            self._current_turn.assistant = text
            self._current_turn.state = state
            self._current_turn.error = error
            self._current_turn = None
            self.flush()
            self._notify("onResult", {
                "state": state,
                "text": text,
                "error": error,
                "conversations": [
                    {"id": c.id, "title": c.title, "turns_count": len(c.turns)}
                    for c in reversed(self._store.conversations)
                ],
                "active_id": self._store.active_id,
            })
            if self._used_search:
                if time.monotonic() >= self._account_service.next_refresh:
                    self.refresh_credits()
                else:
                    self._credit_pending = True

    def _on_status(self, text):
        self._status_text = text
        self._notify("onStatus", text)

    def _on_busy_changed(self, busy):
        self._notify("onBusyChanged", {
            "busy": busy,
            "can_cancel": self._service.can_cancel,
            "release_failed": self._service.release_failed,
        })

    def _on_credit_updated(self, kind, text):
        if kind == "balance":
            self._credit_balance = text
        else:
            self._credit_usage = text
        self._credit_status = ""
        self._notify("onCreditUpdated", {
            "balance": self._credit_balance,
            "usage": self._credit_usage,
            "status": self._credit_status,
        })

    def _on_credit_failed(self, kind, error):
        self._credit_status = error
        self._notify("onCreditFailed", {
            "kind": kind,
            "error": error,
        })

    def _on_credit_busy_changed(self, busy):
        self._notify("onCreditBusyChanged", busy)

    def shutdown(self):
        """アプリ終了時: 未保存データのフラッシュとモデル解放・停止。"""
        if self._closed:
            return
        self._closed = True

        # 予約保存タイマーを停止
        if self._save_timer and self._save_timer.is_alive():
            self._save_timer.cancel()

        # ウィンドウ参照を解除して evaluate_js を完全に遮断
        self._window = None

        # サービスの停止とモデル解放
        try:
            self._service.shutdown()
        except Exception:
            logging.debug("Error shutting down chat service", exc_info=True)

        try:
            self._account_service.shutdown()
        except Exception:
            logging.debug("Error shutting down account service", exc_info=True)

        # 最後に未保存データをディスクへ安全に書き込み（UI通知なし）
        try:
            self._store.save()
        except Exception:
            logging.exception("Failed to save store on shutdown")
