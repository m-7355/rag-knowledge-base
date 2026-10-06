"use strict";

// 画面操作に使う要素の参照
const form = document.getElementById("question-form");
const questionField = document.getElementById("question");
const submitButton = document.getElementById("submit-button");
const responseSection = document.getElementById("response-section");
const requestState = document.getElementById("request-state");
const responseError = document.getElementById("response-error");
const answerText = document.getElementById("answer-text");
const answerState = document.getElementById("answer-state");
const sourcesSection = document.getElementById("sources-section");
const sourceList = document.getElementById("source-list");
const inputError = document.getElementById("input-error");
const healthStatus = document.getElementById("health-status");
const healthDot = document.getElementById("health-dot");
const syncPanel = document.getElementById("sync-panel");
const syncDot = document.getElementById("sync-dot");
const syncStatusText = document.getElementById("sync-status");
const syncDetail = document.getElementById("sync-detail");
const syncCount = document.getElementById("sync-count");
const syncLastRun = document.getElementById("sync-last-run");
const syncProgress = document.getElementById("sync-progress");
const syncProgressBar = document.getElementById("sync-progress-bar");
const syncProgressText = document.getElementById("sync-progress-text");
const syncNotice = document.getElementById("sync-notice");
const syncFailures = document.getElementById("sync-failures");
const syncFailureSummary = document.getElementById("sync-failure-summary");
const syncFailureList = document.getElementById("sync-failure-list");
const syncNowButton = document.getElementById("sync-now");
const driveConnectButton = document.getElementById("drive-connect");

let searchable = syncPanel === null;
let syncPollTimer = null;
let syncRequestPending = false;
let answerInFlight = false;

// 同期可能状態と回答要求中の両方を考慮して送信操作を有効化
function updateSubmitButton() {
  submitButton.disabled = !searchable || answerInFlight;
}

// 出典一覧の内容と表示状態の初期化
function clearSources() {
  sourceList.replaceChildren();
  sourcesSection.hidden = true;
}

// APIまたは通信エラーの画面反映
function showError(message) {
  responseError.textContent = message;
  responseError.hidden = false;
  answerText.textContent = "";
  answerState.textContent = "";
  clearSources();
}

// APIの出典データを安全なテキストとして表示
function renderSources(sources) {
  clearSources();
  if (!Array.isArray(sources) || sources.length === 0) return;

  for (const source of sources) {
    const article = document.createElement("article");
    article.className = "source-item";

    const heading = document.createElement("div");
    heading.className = "source-heading";
    const name = document.createElement(source.url ? "a" : "strong");
    name.className = "source-name";
    name.textContent = String(source.file_name ?? "");
    if (source.url && /^https:\/\/drive\.google\.com\//.test(source.url)) {
      name.href = source.url;
      name.target = "_blank";
      name.rel = "noopener noreferrer";
      name.title = "Google Driveで開く";
    }
    const page = document.createElement("span");
    page.className = "source-page";
    page.textContent = `${String(source.page_number ?? "")}ページ`;
    heading.append(name, page);

    const excerpt = document.createElement("p");
    excerpt.className = "source-excerpt";
    excerpt.textContent = String(source.excerpt ?? "");
    article.append(heading, excerpt);
    sourceList.append(article);
  }
  sourcesSection.hidden = false;
}

// APIの稼働状態に合わせた画面表示
async function checkHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    const healthy = response.ok && data.status === "ok";
    healthStatus.textContent = healthy ? "準備完了" : "依存サービス停止";
    healthDot.classList.toggle("is-ready", healthy);
    healthDot.classList.toggle("is-down", !healthy);
  } catch {
    healthStatus.textContent = "接続できません";
    healthDot.classList.add("is-down");
  }
}

// Workerの状態に応じて初回セットアップ・差分同期・検索可能状態を描画
function renderSyncStatus(status) {
  const running = ["checking", "syncing", "authorizing"].includes(status.state);
  const stateLabels = {
    checking: "同期状態を確認中",
    syncing: "バックグラウンド同期中",
    authorizing: "Google Driveに接続中",
    idle: "同期確認済み",
    error: "同期に問題があります",
  };
  syncStatusText.textContent = stateLabels[status.state] || "同期状態を確認中";
  syncDot.classList.toggle("is-ready", status.searchable && !running);
  syncDot.classList.toggle("is-down", status.state === "error" && !status.searchable);
  syncDot.classList.toggle("is-syncing", running);

  searchable = Boolean(status.searchable);
  questionField.disabled = !searchable;
  updateSubmitButton();
  questionField.placeholder = searchable
    ? "質問を入力してください"
    : "初回同期が完了すると質問できます";

  if (status.initial_sync) {
    syncDetail.textContent = "初回セットアップ中です。資料の索引ができるまで質問できません。";
  } else if (status.state === "syncing" && searchable) {
    syncDetail.textContent = "既存の資料を検索しながら、変更されたファイルを更新しています。";
  } else if (status.drive_state === "auth_required") {
    syncDetail.textContent = "Google Driveへのアクセス許可が必要です。接続後に自動同期します。";
  } else if (status.message) {
    syncDetail.textContent = status.message;
  } else if (!searchable) {
    syncDetail.textContent = "検索可能な資料がありません。同期元を接続して同期してください。";
  } else {
    syncDetail.textContent = "同期は質問処理と独立して実行されます。更新中も検索できます。";
  }

  syncCount.textContent = `${Number(status.ready_documents) || 0} 件の資料を検索できます`;
  if (status.last_synced_at) {
    const lastRun = new Date(status.last_synced_at);
    syncLastRun.textContent = `最終確認 ${lastRun.toLocaleString("ja-JP")}`;
  } else {
    syncLastRun.textContent = "";
  }

  const progress = status.progress || {};
  const total = Number(progress.total) || 0;
  const done = Number(progress.done) || 0;
  syncProgress.hidden = !running || total === 0;
  if (total > 0) {
    syncProgressBar.max = total;
    syncProgressBar.value = Math.min(done, total);
    const percent = Math.floor((done / total) * 100);
    const current = progress.current ? ` · ${progress.current}` : "";
    syncProgressText.textContent = `${progress.source || "資料"} ${done} / ${total} 件 (${percent}%)${current}`;
  }

  syncNotice.hidden = searchable || !status.initial_sync;
  syncNotice.textContent = status.initial_sync
    ? "初回同期が完了すると検索できます"
    : "";
  syncNowButton.disabled = running || syncRequestPending;
  driveConnectButton.hidden = !["auth_required", "error"].includes(status.drive_state);
  driveConnectButton.disabled = running || syncRequestPending;
  driveConnectButton.title = status.drive_message || "Google Driveへのアクセスを許可します";

  const failures = Array.isArray(status.failures) ? status.failures : [];
  syncFailureList.replaceChildren();
  for (const failure of failures) {
    const item = document.createElement("li");
    item.textContent = `${String(failure.path ?? "資料")}: ${String(failure.message ?? "同期に失敗しました")}`;
    syncFailureList.append(item);
  }
  const failureCount = Number(status.last_summary?.failed) || failures.length;
  syncFailures.hidden = failures.length === 0;
  syncFailureSummary.textContent = `失敗した資料 ${failureCount} 件`;
  if (status.last_summary && !running) {
    const summary = status.last_summary;
    syncDetail.textContent = `更新 ${summary.indexed} 件 · 変更なし ${summary.unchanged} 件 · 削除 ${summary.removed} 件 · 失敗 ${summary.failed} 件`;
  }
}

// 同期状態を定期取得し、進捗の表示だけを更新
async function pollSyncStatus() {
  if (!syncPanel) return;
  try {
    const response = await fetch("/api/sync/status", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error("同期状態を取得できませんでした。");
    renderSyncStatus(await response.json());
  } catch {
    syncStatusText.textContent = "同期状態を取得できません";
    syncDetail.textContent = "アプリケーションとの接続を確認してください。";
  } finally {
    syncPollTimer = window.setTimeout(pollSyncStatus, 1500);
  }
}

// 同期やOAuth認可はバックグラウンドWorkerへ依頼し、画面をブロックしない
async function requestSync(endpoint) {
  syncRequestPending = true;
  syncNowButton.disabled = true;
  driveConnectButton.disabled = true;
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: "{}",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "同期を開始できませんでした。");
  } catch (error) {
    syncDetail.textContent = error instanceof Error ? error.message : "同期を開始できませんでした。";
  } finally {
    syncRequestPending = false;
    await pollSyncStatusOnce();
  }
}

async function pollSyncStatusOnce() {
  if (!syncPanel) return;
  try {
    const response = await fetch("/api/sync/status", { headers: { Accept: "application/json" } });
    if (response.ok) renderSyncStatus(await response.json());
  } catch {
    // 次の定期ポーリングで接続状態を再確認する
  }
}

if (syncPanel) {
  syncPanel.hidden = false;
  syncNowButton.addEventListener("click", () => requestSync("/api/sync"));
  driveConnectButton.addEventListener("click", () => requestSync("/api/drive/connect"));
  pollSyncStatus();
}

// 質問の事前検証と非同期送信の状態管理
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (answerInFlight) return;
  inputError.textContent = "";
  questionField.removeAttribute("aria-invalid");
  const question = questionField.value.trim();

  if (!searchable) {
    inputError.textContent = "初回同期が完了すると質問できます。";
    return;
  }

  if (!question) {
    inputError.textContent = "質問を入力してください。";
    questionField.setAttribute("aria-invalid", "true");
    questionField.focus();
    return;
  }
  if (question.length > questionField.maxLength) {
    inputError.textContent = `質問は${questionField.maxLength}文字以内で入力してください。`;
    questionField.setAttribute("aria-invalid", "true");
    questionField.focus();
    return;
  }

  responseSection.hidden = false;
  responseError.hidden = true;
  answerText.textContent = "";
  answerState.textContent = "";
  clearSources();
  requestState.hidden = false;
  answerInFlight = true;
  updateSubmitButton();
  form.setAttribute("aria-busy", "true");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "回答を取得できませんでした。");
    }
    if (typeof data.answer !== "string") {
      throw new Error("回答形式を確認できませんでした。");
    }

    answerText.textContent = data.answer;
    answerState.textContent = data.abstained ? "根拠不足" : "回答";
    answerState.classList.toggle("is-abstained", Boolean(data.abstained));
    renderSources(data.sources);
  } catch (error) {
    showError(error instanceof Error ? error.message : "通信に失敗しました。");
  } finally {
    requestState.hidden = true;
    answerInFlight = false;
    updateSubmitButton();
    form.removeAttribute("aria-busy");
  }
});

checkHealth();
