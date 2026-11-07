"""Read-only Ollama account balance/usage, independent of local model inference."""
import json
import logging
import math
import os
import threading
import time

import requests

from services.signals import Signal


def money(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('金額の形式が不正です。')
    return f'${value:,.3f}'


def balance_text(data):
    if not isinstance(data, dict):
        raise ValueError('残高の形式が不正です。')
    purchased, included = data.get('purchased'), data.get('included')
    lines = []
    if isinstance(purchased, dict) and 'balance_usd' in purchased:
        lines.append('購入分の残量: ' + money(purchased['balance_usd']))
    if isinstance(included, dict):
        if 'balance_usd' in included:
            lines.append('プラン付属分の残量: ' + money(included['balance_usd']))
            if 'allowance_usd' in included:
                allowance, remaining = included['allowance_usd'], included['balance_usd']
                money(allowance)
                lines.append('付属枠の当期消費量: ' + money(max(0, allowance - remaining)))
        else:
            for key, title in (('session', 'セッション'), ('weekly', '週間')):
                limit = included.get(key)
                if isinstance(limit, dict) and 'remaining_percent' in limit:
                    percent = limit['remaining_percent']
                    if type(percent) not in (int, float) or not 0 <= percent <= 100:
                        raise ValueError('利用枠の形式が不正です。')
                    lines.append(f'{title}枠の残量: {percent:g}%')
    if not lines:
        raise ValueError('残高情報が含まれていません。')
    return '\n'.join(lines)


def usage_text(data):
    if not isinstance(data, dict) or not isinstance(data.get('totals'), dict):
        raise ValueError('利用量の形式が不正です。')
    total = data['totals']
    if 'usage_usd' not in total:
        return '消費量（過去30日・本人）: 金額はAPI未提供'
    return '消費量（過去30日・本人）: ' + money(total['usage_usd'])


class OllamaAccountService:
    def __init__(self, parent=None, *, endpoint='https://ollama.com', timeout_ms=15000,
                 key_provider=None):
        self.updated = Signal(str, str)
        self.failed = Signal(str, str)
        self.busyChanged = Signal(bool)

        self._endpoint = endpoint
        self._timeout_seconds = timeout_ms / 1000.0
        self._key_provider = key_provider or (lambda: os.environ.get('OLLAMA_API_KEY', ''))
        self._thread = None
        self._lock = threading.Lock()
        self.next_refresh = 0.0
        self._closed = False
        self._running = False

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def refresh(self):
        if self._closed or self.is_running or time.monotonic() < self.next_refresh:
            return False
        key = self._key_provider().strip()
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            for kind in ('balance', 'usage'):
                self.failed.emit(kind, 'OLLAMA_API_KEY を設定してアプリを再起動してください。')
            return False

        with self._lock:
            self._running = True
        self.next_refresh = time.monotonic() + 60
        self.busyChanged.emit(True)

        thread = threading.Thread(target=self._worker, args=(key,), daemon=True)
        self._thread = thread
        thread.start()
        return True

    def _worker(self, key):
        headers = {
            'Authorization': f'Bearer {key}',
            'User-Agent': 'SimpleLocalChatbot/1.0',
        }
        endpoints = (
            ('balance', '/api/balance'),
            ('usage', '/api/usage?range=30d'),
        )
        try:
            for kind, path in endpoints:
                if self._closed:
                    break
                url = self._endpoint.rstrip('/') + path
                try:
                    res = requests.get(url, headers=headers, timeout=self._timeout_seconds)
                    if res.status_code == 429:
                        try:
                            retry_after = int(res.headers.get('Retry-After', '60'))
                            self.next_refresh = max(self.next_refresh, time.monotonic() + max(60, retry_after))
                        except (ValueError, TypeError):
                            pass
                    if not (200 <= res.status_code < 300):
                        raise ValueError(f'HTTP {res.status_code}: Ollamaの認証・利用条件を確認してください。')
                    if len(res.content) > 1024 * 1024:
                        raise ValueError('応答が大きすぎます。')
                    data = res.json()
                    text = balance_text(data) if kind == 'balance' else usage_text(data)
                    self.updated.emit(kind, text)
                except requests.Timeout:
                    self.failed.emit(kind, '取得がタイムアウトしました。')
                except requests.RequestException as e:
                    self.failed.emit(kind, 'OllamaのアカウントAPIに接続できません。')
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self.failed.emit(kind, str(exc))
        finally:
            with self._lock:
                self._running = False
            self.busyChanged.emit(False)

    def shutdown(self):
        self._closed = True
