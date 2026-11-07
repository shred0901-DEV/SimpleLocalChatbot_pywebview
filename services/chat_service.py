"""Nonblocking Ollama HTTP client using requests and threading.
Owns no subprocess, model files, or CUDA state.
"""
from copy import deepcopy
import json
import logging
import threading
import time

import requests

from core.chat import local_model, server_url
from core.chat_search import search_conversation
from services.signals import Signal
from services.web_search_service import WebSearchService


class ChatService:
    def __init__(self, parent=None, timeout_ms=120000, release_timeout_ms=30000, search_service=None):
        self.modelsReady = Signal(object)
        self.connectionFailed = Signal(str)
        self.connectingChanged = Signal(bool)
        self.textReceived = Signal(str)
        self.sourcesReady = Signal(object)
        self.result = Signal(str, str, str)  # state, assistant text, error
        self.status = Signal(str)
        self.busyChanged = Signal(bool)

        self.timeout_seconds = timeout_ms / 1000.0
        self.release_timeout_seconds = release_timeout_ms / 1000.0

        self._phase = 'idle'
        self._lock = threading.Lock()
        self._active_session = None
        self._release_target = None
        self._model = ''
        self._text = ''
        self._outcome = None
        self._closed = False
        self._final_status = ''
        self._search = search_service or WebSearchService(self)
        self._search.completed.connect(self._search_ready)
        self._search.failed.connect(self._search_failed)
        self._search.cancelled.connect(self._search_cancelled)
        self._work_thread = None

    @property
    def is_running(self):
        with self._lock:
            return self._phase != 'idle'

    @property
    def is_active(self):
        with self._lock:
            return self._phase not in ('idle', 'release_failed')

    @property
    def release_failed(self):
        with self._lock:
            return self._phase == 'release_failed'

    @property
    def can_cancel(self):
        with self._lock:
            return self._phase in ('search', 'show', 'chat')

    def refresh_models(self, endpoint):
        if self._closed or self.is_running:
            return False
        endpoint = server_url(endpoint)

        def worker():
            self.connectingChanged.emit(True)
            try:
                url = endpoint.rstrip('/') + '/api/tags'
                res = requests.get(url, timeout=5.0)
                if not (200 <= res.status_code < 300):
                    try:
                        err_msg = res.json().get('error', '')
                    except Exception:
                        err_msg = ''
                    raise ValueError(f'Ollama HTTP {res.status_code}: {err_msg or "リクエストに失敗しました。"}')
                if len(res.content) > 4 * 1024 * 1024:
                    raise ValueError('モデル一覧が大きすぎます。')
                raw = res.json()
                if not isinstance(raw, dict) or not isinstance(raw.get('models'), list):
                    raise ValueError('Ollamaのモデル一覧の形式が不正です。')
                names = []
                for m in raw['models']:
                    if isinstance(m, dict) and isinstance(m.get('name'), str):
                        if local_model(m['name']) and not m.get('remote_host'):
                            names.append(m['name'])
                self.modelsReady.emit(sorted(set(names)))
            except requests.Timeout:
                self.connectionFailed.emit('Ollamaの接続確認がタイムアウトしました。')
            except requests.RequestException:
                self.connectionFailed.emit('Ollamaに接続できません。起動状態と接続先を確認してください。')
            except Exception as exc:
                self.connectionFailed.emit(str(exc))
            finally:
                self.connectingChanged.emit(False)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def start(self, settings, messages):
        if self._closed or self.is_running:
            return False
        if not local_model(settings.model):
            raise ValueError('ダウンロード済みのローカルモデルを選択してください。')

        self._settings = deepcopy(settings)
        self._settings.endpoint = server_url(settings.endpoint)
        self._messages = deepcopy(messages)
        self._model = settings.model
        self._text, self._outcome = '', None

        with self._lock:
            self._phase = 'show'
        self.busyChanged.emit(True)

        self._work_thread = threading.Thread(target=self._run_flow, daemon=True)
        self._work_thread.start()
        return True

    def _run_flow(self):
        try:
            self.status.emit('モデルを確認しています…')
            endpoint = self._settings.endpoint.rstrip('/')
            url = f"{endpoint}/api/show"
            with requests.Session() as session:
                with self._lock:
                    self._active_session = session
                res = session.post(url, json={'model': self._model}, timeout=self.timeout_seconds)

            if not (200 <= res.status_code < 300):
                try:
                    err = res.json().get('error', '')
                except Exception:
                    err = ''
                raise ValueError(f'Ollama HTTP {res.status_code}: {err or "モデル情報取得に失敗しました。"}')

            data = res.json()
            if data.get('remote_host') or data.get('remote_model'):
                raise ValueError('ローカルにダウンロードしたモデルを選択してください。')
            capabilities = data.get('capabilities', [])
            if capabilities and 'completion' not in capabilities:
                raise ValueError('このモデルはチャットに対応していません。別のモデルを選択してください。')
            if any(message.get('images') for message in self._messages) and 'vision' not in capabilities:
                raise ValueError('選択したモデルの画像入力対応を確認できません。Gemma 4など画像対応モデルと最新のOllamaを使うか、新しい会話で画像を使わず送信してください。')

            self._model_info = data

            if self._settings.web_search:
                with self._lock:
                    self._phase = 'search'
                self.status.emit('Web検索中…（検索は1回、最大3件）')
                query = self._messages[-1]['content'] if self._messages else ''
                self._search.start(query)
                # Web search completes via signals (_search_ready, _search_failed, _search_cancelled)
                return
            else:
                self._start_chat()

        except requests.Timeout:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal('Ollamaの応答がタイムアウトしました。')
        except requests.RequestException as e:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal('Ollamaに接続できません。起動状態と接続先を確認してください。')
        except Exception as exc:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal(str(exc))

    def _search_ready(self, results):
        if self._closed or self._phase != 'search':
            return
        try:
            self._messages, sources = search_conversation(self._messages, results, self._settings.num_ctx)
            self.sourcesReady.emit(sources)
            self._work_thread = threading.Thread(target=self._start_chat, daemon=True)
            self._work_thread.start()
        except Exception as exc:
            self._search_failed(str(exc))

    def _search_failed(self, message):
        if not self._closed and self._phase == 'search':
            self._outcome = 'error'
            self._terminal(message)

    def _search_cancelled(self):
        if not self._closed and self._phase == 'search':
            self._outcome = 'cancelled'
            self._terminal('')

    def _start_chat(self):
        with self._lock:
            self._phase = 'chat'
        self._release_target = (self._settings.endpoint, self._model)
        self.status.emit('応答を生成しています…（初回はモデルの読み込みに時間がかかります）')

        payload = {
            'model': self._model,
            'messages': self._messages,
            'stream': True,
            'keep_alive': 0,
            'options': {
                'num_ctx': self._settings.num_ctx,
                'num_predict': 1024,
            }
        }
        thinking = self._model_info.get('thinking', {})
        if isinstance(thinking, dict) and False in thinking.get('values', []):
            payload['think'] = False
        elif 'thinking' in self._model_info.get('capabilities', []) and self._model.startswith(('qwen3:', 'qwen3.5:')) and not thinking:
            payload['think'] = False

        endpoint = self._settings.endpoint.rstrip('/')
        url = f"{endpoint}/api/chat"

        stream_error = ''
        done = False
        received_bytes = 0

        try:
            with requests.Session() as session:
                with self._lock:
                    self._active_session = session
                with session.post(url, json=payload, stream=True, timeout=self.timeout_seconds) as res:
                    if not (200 <= res.status_code < 300):
                        try:
                            err = res.json().get('error', '')
                        except Exception:
                            err = ''
                        raise ValueError(f'Ollama HTTP {res.status_code}: {err or "チャット生成に失敗しました。"}')

                    for line in res.iter_lines(decode_unicode=False):
                        if self._closed or self._outcome == 'cancelled':
                            break
                        if not line:
                            continue
                        received_bytes += len(line)
                        if received_bytes > 4 * 1024 * 1024:
                            stream_error = 'Ollamaの応答が大きすぎます。'
                            break
                        try:
                            chunk = json.loads(line.decode('utf-8'))
                            if chunk.get('error'):
                                stream_error = str(chunk['error'])[:500]
                                break
                            msg = chunk.get('message', {})
                            text = msg.get('content', '')
                            if text:
                                self._text += text
                                self.textReceived.emit(text)
                            if chunk.get('done') is True:
                                done = True
                        except Exception as e:
                            stream_error = f'Ollamaの応答を処理できません: {e}'
                            break

            if self._outcome != 'cancelled':
                if not stream_error and not done:
                    stream_error = '応答が完了する前に接続が終了しました。'
                if not stream_error and not self._text.strip():
                    stream_error = 'モデルから回答本文が返されませんでした。'
                self._outcome = 'error' if stream_error else 'complete'
            self._terminal(stream_error)

        except requests.Timeout:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal('Ollamaの応答がタイムアウトしました。')
        except requests.RequestException:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal('Ollamaとの接続が中断されました。')
        except Exception as exc:
            if self._outcome == 'cancelled':
                self._terminal('')
            else:
                self._terminal(str(exc))
        finally:
            with self._lock:
                self._active_session = None

    def _terminal(self, error):
        state = 'interrupted' if self._outcome == 'cancelled' else (self._outcome or ('error' if error else 'complete'))
        self.result.emit(state, self._text, '' if state == 'interrupted' else error)
        self._final_status = (error if state == 'error' else
                              '中止しました。' if state == 'interrupted' else '応答が完了しました。')
        if self._release_target is None:
            self._idle()
        else:
            self.retry_release()

    def retry_release(self):
        if self._release_target is None or self._closed:
            return
        threading.Thread(target=self._release_worker, daemon=True).start()

    def _release_worker(self):
        with self._lock:
            self._phase = 'unload'
        self.busyChanged.emit(True)
        self.status.emit('モデルをメモリから解放しています…')

        endpoint, model = self._release_target
        endpoint = endpoint.rstrip('/')
        url = f"{endpoint}/api/generate"
        payload = {'model': model, 'keep_alive': 0, 'stream': False}

        deadline = time.monotonic() + self.release_timeout_seconds
        try:
            res = requests.post(url, json=payload, timeout=self.release_timeout_seconds)
            if not (200 <= res.status_code < 300) or res.json().get('done') is not True:
                raise ValueError('モデル解放の完了を確認できません。')

            # Poll /api/ps to verify unload
            target = model if ':' in model else model + ':latest'
            while time.monotonic() < deadline and not self._closed:
                ps_res = requests.get(f"{endpoint}/api/ps", timeout=5.0)
                if 200 <= ps_res.status_code < 300:
                    models = ps_res.json().get('models', [])
                    loaded = any(isinstance(item, dict) and target in (item.get('name'), item.get('model'))
                                 for item in models)
                    if not loaded:
                        self._release_target = None
                        self._idle()
                        return
                time.sleep(0.3)

            if time.monotonic() >= deadline:
                raise ValueError('モデルがまだメモリに残っています。')

        except Exception as exc:
            with self._lock:
                self._phase = 'release_failed'
            self.status.emit(f'モデル解放を確認できません。「モデル解放を再試行」を押してください。{exc}')
            self.busyChanged.emit(True)

    def _idle(self):
        with self._lock:
            self._phase = 'idle'
        self.status.emit(self._final_status)
        self.busyChanged.emit(False)

    def cancel(self):
        with self._lock:
            phase = self._phase
            session = self._active_session
            self._outcome = 'cancelled'

        if phase == 'search':
            self._search.cancel()
        elif phase in ('show', 'chat') and session is not None:
            try:
                session.close()
            except Exception:
                pass

    def shutdown(self):
        self._closed = True
        self.cancel()
        self._search.shutdown()
        # アプリ終了時のモデルメモリ解放（バックグラウンドでリクエストを送信）
        if self._release_target:
            endpoint, model = self._release_target
            def quick_release():
                try:
                    requests.post(
                        f"{endpoint.rstrip('/')}/api/generate",
                        json={'model': model, 'keep_alive': 0, 'stream': False},
                        timeout=3.0,
                    )
                except Exception:
                    pass
            threading.Thread(target=quick_release, daemon=True).start()
