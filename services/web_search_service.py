"""One explicit, cancellable Ollama search request. No automatic retries or web fetch."""
import json
import logging
import os
import threading

import requests

from services.signals import Signal


class WebSearchService:
    def __init__(self, parent=None, *, endpoint='https://ollama.com', timeout_ms=30000, key_provider=None):
        self.completed = Signal(object)
        self.failed = Signal(str)
        self.cancelled = Signal()

        self._endpoint = endpoint
        self._timeout_seconds = timeout_ms / 1000.0
        self._key_provider = key_provider or (lambda: os.environ.get('OLLAMA_API_KEY', ''))
        self._thread = None
        self._session = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._running = False
        self._closed = False

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def start(self, query):
        if self.is_running:
            return False
        key = self._key_provider().strip()
        if not key:
            raise ValueError('ユーザー環境変数 OLLAMA_API_KEY を設定し、アプリを起動し直してください。')
        if any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError('OLLAMA_API_KEY の形式を確認してください。空白・改行を含めないでください。')
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError('Web検索の質問は1〜4000文字で入力してください。')

        with self._lock:
            self._running = True
            self._cancelled = False

        thread = threading.Thread(target=self._worker, args=(query, key), daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _worker(self, query, key):
        url = self._endpoint.rstrip('/') + '/api/web_search'
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {key}',
            'User-Agent': 'SimpleLocalChatbot/1.0',
        }
        payload = {'query': query, 'max_results': 3}
        try:
            with requests.Session() as session:
                with self._lock:
                    self._session = session
                res = session.post(url, headers=headers, json=payload, timeout=self._timeout_seconds)

            if self._cancelled:
                self.cancelled.emit()
                return

            if len(res.content) > 1024 * 1024:
                raise ValueError('Web検索の応答が大きすぎます。')

            try:
                data = res.json()
            except (ValueError, UnicodeError):
                data = None

            detail = str(data.get('error', ''))[:600] if isinstance(data, dict) else ''

            if not (200 <= res.status_code < 300):
                if res.status_code == 401:
                    message = 'APIキーの認証に失敗しました。'
                elif res.status_code == 429:
                    message = '利用上限に達しました。時間をおいて再送信してください。'
                else:
                    message = 'Ollamaの利用条件・アカウント設定を確認してください。'
                raise ValueError(f'Web検索 HTTP {res.status_code}: {message} {detail}')

            if detail:
                raise ValueError('Web検索に失敗しました: ' + detail)

            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise ValueError('Web検索の応答形式が不正、または空です。')

            self.completed.emit(data['results'])

        except requests.Timeout:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit('Web検索がタイムアウトしました。自動再試行は行いません。')
        except requests.RequestException:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit('Web検索サービスに接続できません。インターネット接続を確認してください。')
        except (ValueError, TypeError) as exc:
            msg = str(exc).replace(key, '[API key]') if key else str(exc)
            self.failed.emit(msg)
        finally:
            with self._lock:
                self._session = None
                self._running = False

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._session:
                try:
                    self._session.close()
                except Exception:
                    pass

    def shutdown(self):
        self._closed = True
        self.cancel()
