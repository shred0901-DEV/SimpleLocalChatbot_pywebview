/* ============================================================
   SimpleLocalChatbot Web UI Logic (Modern Cyber Dark Theme)
   ============================================================ */

(function () {
  "use strict";

  // DOM要素キャッシュ
  const dom = {
    headerConnectionText: document.getElementById("header-connection-text"),
    historyList: document.getElementById("history-list"),
    historyCount: document.getElementById("history-count"),
    btnNewChat: document.getElementById("btn-new-chat"),
    creditBalance: document.getElementById("credit-balance"),
    creditUsage: document.getElementById("credit-usage"),
    creditStatus: document.getElementById("credit-status"),
    btnRefreshCredits: document.getElementById("btn-refresh-credits"),
    transcript: document.getElementById("transcript"),
    attachmentBar: document.getElementById("attachment-bar"),
    chatInput: document.getElementById("chat-input"),
    endpointInput: document.getElementById("endpoint-input"),
    modelSelect: document.getElementById("model-select"),
    btnRefreshModels: document.getElementById("btn-refresh-models"),
    webSearchCheck: document.getElementById("web-search-check"),
    contextInput: document.getElementById("context-input"),
    btnAttach: document.getElementById("btn-attach"),
    btnRetryRelease: document.getElementById("btn-retry-release"),
    btnSend: document.getElementById("btn-send"),
    btnCancel: document.getElementById("btn-cancel"),
    statusLabel: document.getElementById("status-label"),
    saveStatusLabel: document.getElementById("save-status-label"),
  };

  let state = {
    conversations: [],
    active_id: "",
    draft: "",
    draft_images: [],
    turns: [],
    settings: {
      endpoint: "http://127.0.0.1:11434",
      model: "",
      num_ctx: 8192,
      web_search: false,
    },
    models: [],
    credit: {
      balance: "未取得",
      usage: "未取得",
      status: "",
    },
    status: "",
    save_status: "",
    busy: false,
    can_cancel: false,
    release_failed: false,
  };

  let isComposing = false; // 日本語IME変換フラグ
  let draftSaveTimer = null;
  let currentAssistantTextElem = null;

  // 外部リンクのハンドリング
  document.addEventListener("click", function (e) {
    const link = e.target.closest("a");
    if (link && link.href) {
      e.preventDefault();
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.open_external_url(link.href);
      }
    }
  });

  // --- 初期化 ---
  function init() {
    setupInputHandlers();
    setupButtonHandlers();

    if (window.pywebview && window.pywebview.api) {
      loadInitialState();
    } else {
      window.addEventListener("pywebviewready", loadInitialState);
    }
  }

  function loadInitialState() {
    window.pywebview.api.get_initial_state().then(function (res) {
      applyState(res);
      window.pywebview.api.start_auto_connect();
    }).catch(function (err) {
      console.error("Failed to get initial state:", err);
    });
  }

  function applyState(newState) {
    state = Object.assign({}, state, newState);
    renderHistory();
    renderTranscript();
    renderAttachments();
    renderSettings();
    renderCredits();
    renderStatus();
    updateControlsState();

    if (state.draft !== undefined && dom.chatInput.value !== state.draft) {
      dom.chatInput.value = state.draft;
    }
  }

  // --- 描画関数群 ---

  function renderHistory() {
    dom.historyList.innerHTML = "";
    const list = state.conversations || [];
    dom.historyCount.textContent = list.length;

    list.forEach(function (conv) {
      const li = document.createElement("li");
      li.className = "history-item" + (conv.id === state.active_id ? " active" : "");
      li.textContent = conv.title || "（新しい会話）";
      li.title = (conv.title || "") + "\n" + (conv.turns_count || 0) + "件のやり取り";
      li.addEventListener("click", function () {
        if (!state.busy) {
          window.pywebview.api.select_conversation(conv.id);
        }
      });
      dom.historyList.appendChild(li);
    });
  }

  function renderTranscript() {
    if (!state.turns || state.turns.length === 0) {
      dom.transcript.innerHTML = 
        '<div class="transcript-welcome">' +
          '<div class="welcome-icon-box">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">' +
              '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>' +
            '</svg>' +
          '</div>' +
          '<h2>SimpleLocalChatbot へようこそ</h2>' +
          '<p>メッセージを入力して会話を開始してください。<br>画像のドラッグ＆ドロップやクリップボード貼り付け（Ctrl+V）にも対応しています。</p>' +
        '</div>';
      currentAssistantTextElem = null;
      return;
    }

    const wasAtBottom = isScrolledToBottom();
    dom.transcript.innerHTML = "";

    state.turns.forEach(function (turn, idx) {
      const isLast = idx === state.turns.length - 1;
      const turnDiv = document.createElement("div");
      turnDiv.className = "turn-block";

      // ユーザー発言
      const userDiv = document.createElement("div");
      userDiv.className = "turn-user";
      userDiv.innerHTML = 
        '<div class="turn-user-header">' +
          '<svg class="mini-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
            '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>' +
            '<circle cx="12" cy="7" r="4"/>' +
          '</svg>' +
          'あなた' +
        '</div>' +
        '<div class="turn-user-body">' + escapeHtml(turn.user) + '</div>';
      turnDiv.appendChild(userDiv);

      // 添付画像プレビュー
      if (turn.images && turn.images.length > 0) {
        const imgList = document.createElement("div");
        imgList.className = "turn-images-list";
        turn.images.forEach(function (img) {
          const card = document.createElement("div");
          card.className = "turn-image-card";
          if (img.thumbnail) {
            card.innerHTML = '<img src="' + img.thumbnail + '" alt="' + escapeHtml(img.name) + '">' +
                             '<div class="turn-image-name">' + escapeHtml(img.name) + '</div>';
          } else {
            card.innerHTML = '<div class="turn-image-name">添付画像: ' + escapeHtml(img.name) + '</div>';
          }
          imgList.appendChild(card);
        });
        turnDiv.appendChild(imgList);
      }

      // アシスタント発言
      const asstDiv = document.createElement("div");
      asstDiv.className = "turn-assistant";
      let suffix = "";
      if (isLast && state.busy) {
        suffix = "（応答中…）";
      } else if (turn.state === "complete") {
        suffix = "";
      } else if (turn.state === "interrupted") {
        suffix = "（中断・未完了）";
      } else if (turn.state === "error") {
        suffix = "（エラー）";
      }

      const modelName = escapeHtml(turn.model || "アシスタント");
      asstDiv.innerHTML = 
        '<div class="turn-assistant-header">' +
          '<span class="assistant-avatar">' +
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
              '<rect x="4" y="4" width="16" height="16" rx="2"/>' +
              '<rect x="9" y="9" width="6" height="6"/>' +
              '<line x1="9" y1="1" x2="9" y2="4"/>' +
              '<line x1="15" y1="1" x2="15" y2="4"/>' +
            '</svg>' +
          '</span>' +
          '<span>' + modelName + (suffix ? ' <span style="font-size:10px; opacity:0.8;">' + suffix + '</span>' : '') + '</span>' +
        '</div>';

      const bodyDiv = document.createElement("div");
      bodyDiv.className = "turn-assistant-body";
      bodyDiv.textContent = turn.assistant || "";
      asstDiv.appendChild(bodyDiv);

      if (isLast) {
        currentAssistantTextElem = bodyDiv;
      }

      if (turn.error) {
        const errDiv = document.createElement("div");
        errDiv.className = "turn-error";
        errDiv.textContent = turn.error;
        asstDiv.appendChild(errDiv);
      }

      // Web検索参照元
      if (turn.sources && turn.sources.length > 0) {
        const srcDiv = document.createElement("div");
        srcDiv.className = "turn-sources";
        let srcHtml = 
          '<div class="turn-sources-title">' +
            '<svg class="mini-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
              '<circle cx="12" cy="12" r="10"/>' +
              '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>' +
              '<path d="M2 12h20"/>' +
            '</svg>' +
            'Web検索の参照元' +
          '</div>';
        turn.sources.forEach(function (src, sIdx) {
          srcHtml += '[' + (sIdx + 1) + '] <a href="' + escapeHtml(src.url) + '" class="external-link">' + escapeHtml(src.title) + '</a><br>';
        });
        srcDiv.innerHTML = srcHtml;
        asstDiv.appendChild(srcDiv);
      }

      turnDiv.appendChild(asstDiv);
      dom.transcript.appendChild(turnDiv);
    });

    if (wasAtBottom) {
      scrollToBottom();
    }
  }

  function renderAttachments() {
    if (!state.draft_images || state.draft_images.length === 0) {
      dom.attachmentBar.classList.add("hidden");
      dom.attachmentBar.innerHTML = "";
      return;
    }

    dom.attachmentBar.classList.remove("hidden");
    dom.attachmentBar.innerHTML = "";

    state.draft_images.forEach(function (img, idx) {
      const preview = document.createElement("div");
      preview.className = "attachment-preview";
      preview.innerHTML = 
        '<img src="' + img.thumbnail + '" alt="' + escapeHtml(img.name) + '">' +
        '<button class="attachment-remove-btn" type="button" title="削除">×</button>';
      preview.querySelector(".attachment-remove-btn").addEventListener("click", function () {
        window.pywebview.api.remove_image(idx);
      });
      dom.attachmentBar.appendChild(preview);
    });
  }

  function renderSettings() {
    if (state.settings) {
      if (document.activeElement !== dom.endpointInput) {
        dom.endpointInput.value = state.settings.endpoint || "http://127.0.0.1:11434";
      }
      dom.contextInput.value = state.settings.num_ctx || 8192;
      dom.webSearchCheck.checked = !!state.settings.web_search;
    }

    renderModelOptions();
  }

  function renderModelOptions() {
    const currentModel = state.settings ? state.settings.model : "";
    const models = state.models || [];

    dom.modelSelect.innerHTML = "";
    if (models.length === 0) {
      if (currentModel) {
        const opt = document.createElement("option");
        opt.value = currentModel;
        opt.textContent = currentModel;
        dom.modelSelect.appendChild(opt);
      } else {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "モデル未検出（再読込してください）";
        dom.modelSelect.appendChild(opt);
      }
    } else {
      models.forEach(function (m) {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        if (m === currentModel) {
          opt.selected = true;
        }
        dom.modelSelect.appendChild(opt);
      });
    }
  }

  function renderCredits() {
    if (state.credit) {
      dom.creditBalance.textContent = state.credit.balance || "未取得";
      dom.creditUsage.textContent = state.credit.usage || "未取得";
      dom.creditStatus.textContent = state.credit.status || "";
    }
  }

  function renderStatus() {
    dom.statusLabel.textContent = state.status || "";
    dom.saveStatusLabel.textContent = state.save_status || "";

    if (state.connected_endpoint && state.models && state.models.length > 0) {
      dom.headerConnectionText.textContent = "Ollama: 準備完了 (" + state.models.length + "モデル)";
    } else if (state.connecting) {
      dom.headerConnectionText.textContent = "Ollama: 接続確認中…";
    } else {
      dom.headerConnectionText.textContent = "Ollama: 未接続";
    }
  }

  function updateControlsState() {
    const isBusy = !!state.busy;
    dom.btnSend.disabled = isBusy;
    dom.btnAttach.disabled = isBusy;
    dom.btnCancel.disabled = !state.can_cancel;

    if (state.release_failed) {
      dom.btnRetryRelease.classList.remove("hidden");
    } else {
      dom.btnRetryRelease.classList.add("hidden");
    }
  }

  // --- スクロール制御 ---
  function isScrolledToBottom() {
    const el = dom.transcript;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= 40;
  }

  function scrollToBottom() {
    dom.transcript.scrollTop = dom.transcript.scrollHeight;
  }

  // --- 入力ハンドラ設定 ---
  function setupInputHandlers() {
    dom.chatInput.addEventListener("compositionstart", function () {
      isComposing = true;
    });
    dom.chatInput.addEventListener("compositionend", function () {
      isComposing = false;
    });

    dom.chatInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        if (!isComposing) {
          e.preventDefault();
          handleSendMessage();
        }
      }
    });

    dom.chatInput.addEventListener("input", function () {
      if (draftSaveTimer) clearTimeout(draftSaveTimer);
      draftSaveTimer = setTimeout(function () {
        state.draft = dom.chatInput.value;
        if (window.pywebview && window.pywebview.api) {
          window.pywebview.api.update_draft(state.draft);
        }
      }, 300);
    });

    // クリップボードからの画像ペースト (Ctrl + V)
    dom.chatInput.addEventListener("paste", function (e) {
      const items = (e.clipboardData || e.originalEvent.clipboardData).items;
      if (!items) return;
      for (let i = 0; i < items.length; i++) {
        if (items[i].type.indexOf("image") !== -1) {
          const file = items[i].getAsFile();
          if (file) {
            e.preventDefault();
            readAndSendImage(file);
            return;
          }
        }
      }
    });

    // ドラッグ＆ドロップ対応
    const dropArea = document.querySelector(".app-layout");
    dropArea.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.stopPropagation();
    });
    dropArea.addEventListener("drop", function (e) {
      e.preventDefault();
      e.stopPropagation();
      const files = e.dataTransfer.files;
      if (files && files.length > 0) {
        for (let i = 0; i < files.length; i++) {
          if (files[i].type.startsWith("image/")) {
            readAndSendImage(files[i]);
          }
        }
      }
    });
  }

  function readAndSendImage(file) {
    if (state.busy) {
      state.status = "画像の添付は応答やモデル解放が終了してから行ってください。";
      renderStatus();
      return;
    }
    const reader = new FileReader();
    reader.onload = function (evt) {
      const base64Data = evt.target.result;
      window.pywebview.api.add_image_base64(file.name, base64Data);
    };
    reader.readAsDataURL(file);
  }

  function setupButtonHandlers() {
    dom.btnNewChat.addEventListener("click", function () {
      if (!state.busy) {
        window.pywebview.api.new_conversation();
      }
    });

    dom.btnRefreshCredits.addEventListener("click", function () {
      dom.btnRefreshCredits.disabled = true;
      window.pywebview.api.refresh_credits();
    });

    dom.btnRefreshModels.addEventListener("click", function () {
      const endpoint = dom.endpointInput.value.trim();
      window.pywebview.api.refresh_models(endpoint);
    });

    dom.modelSelect.addEventListener("change", syncSettings);
    dom.webSearchCheck.addEventListener("change", syncSettings);
    dom.contextInput.addEventListener("change", syncSettings);

    dom.btnAttach.addEventListener("click", function () {
      window.pywebview.api.choose_images();
    });

    dom.btnSend.addEventListener("click", handleSendMessage);

    dom.btnCancel.addEventListener("click", function () {
      window.pywebview.api.cancel();
    });

    dom.btnRetryRelease.addEventListener("click", function () {
      window.pywebview.api.retry_release();
    });
  }

  function syncSettings() {
    const endpoint = dom.endpointInput.value.trim();
    const model = dom.modelSelect.value;
    const num_ctx = parseInt(dom.contextInput.value, 10) || 8192;
    const web_search = dom.webSearchCheck.checked;

    state.settings = { endpoint, model, num_ctx, web_search };
    window.pywebview.api.update_settings(endpoint, model, num_ctx, web_search);
  }

  function handleSendMessage() {
    if (state.busy) return;
    const text = dom.chatInput.value.trim();
    syncSettings();
    window.pywebview.api.send_message(text);
  }

  // --- Python側からの通知受信インターフェース (window.app) ---
  window.app = {
    onStreamText: function (text) {
      if (currentAssistantTextElem) {
        const wasAtBottom = isScrolledToBottom();
        currentAssistantTextElem.textContent += text;
        if (wasAtBottom) {
          scrollToBottom();
        }
      }
    },

    onSourcesReady: function (sources) {
      if (state.turns && state.turns.length > 0) {
        state.turns[state.turns.length - 1].sources = sources;
        renderTranscript();
      }
    },

    onResult: function (data) {
      state.busy = false;
      state.can_cancel = false;
      if (data.conversations) state.conversations = data.conversations;
      if (data.active_id) state.active_id = data.active_id;

      if (state.turns && state.turns.length > 0) {
        const last = state.turns[state.turns.length - 1];
        last.state = data.state;
        last.assistant = data.text;
        last.error = data.error;
      }
      renderHistory();
      renderTranscript();
      updateControlsState();
    },

    onMessageSent: function (newState) {
      applyState(newState);
      dom.chatInput.value = "";
      scrollToBottom();
      dom.chatInput.focus();
    },

    onStatus: function (statusText) {
      state.status = statusText;
      renderStatus();
    },

    onSaveStatus: function (saveStatusText) {
      state.save_status = saveStatusText;
      renderStatus();
    },

    onBusyChanged: function (data) {
      state.busy = data.busy;
      state.can_cancel = data.can_cancel;
      state.release_failed = data.release_failed;
      updateControlsState();
      renderTranscript();
    },

    onDraftImagesChanged: function (images) {
      state.draft_images = images;
      renderAttachments();
    },

    onConversationChanged: function (newState) {
      applyState(newState);
      dom.chatInput.focus();
    },

    onModelsReady: function (data) {
      state.models = data.models;
      if (state.settings) {
        state.settings.model = data.selected_model;
      }
      state.status = data.status;
      state.connected_endpoint = data.connected_endpoint;
      renderSettings();
      renderStatus();
    },

    onConnectionFailed: function (error) {
      state.status = error;
      renderStatus();
    },

    onConnectingChanged: function (connecting) {
      state.connecting = connecting;
      dom.btnRefreshModels.disabled = connecting;
      if (connecting) {
        state.status = "Ollamaに接続しています…";
      }
      renderStatus();
    },

    onCreditUpdated: function (data) {
      state.credit.balance = data.balance;
      state.credit.usage = data.usage;
      state.credit.status = data.status;
      renderCredits();
    },

    onCreditFailed: function (data) {
      state.credit.status = data.error;
      renderCredits();
    },

    onCreditBusyChanged: function (busy) {
      dom.btnRefreshCredits.disabled = busy;
    },
  };

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  init();
})();
