"use strict";

const state = {
  history: [], responses: new Map(), messageSeq: 0, activeResponseId: null,
  activeVisual: null, relatedVisuals: [], activeCitations: [], activeView: "crop",
  pollingTimer: null, pollingGeneration: 0, opsTimer: null, opsStartMs: 0,
  activeAbortController: null, operationSeq: 0, csrfToken: "", user: null,
  credentialsEditable: false, registrationRequired: false, setupToken: "", localRegistration: false,
};
const nativeFetch = window.fetch.bind(window);
const byId = (id) => document.getElementById(id);
const node = (tag, className = "", text = "") => {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== "") result.textContent = String(text);
  return result;
};
const numberText = (value, digits = 3) => Number.isFinite(value) ? value.toFixed(digits) : "N/A";
const boxText = (value) => Array.isArray(value) ? `[${value.map((n) => numberText(n)).join(", ")}]` : "N/A";
const errorText = (data, fallback) => typeof data?.detail === "string" ? data.detail : data?.detail ? JSON.stringify(data.detail) : fallback;

// Secrets and session credentials never enter browser persistence. Remove the
// previous client's legacy credential cache without reading or restoring it.
try { localStorage.removeItem("aetherrag_provider_cfg"); } catch (_) { /* storage may be disabled */ }
if (window.marked) window.marked.setOptions({ gfm: true, breaks: true });

function showLogin(message = "Inicia sesión para acceder al corpus documental.") {
  state.csrfToken = "";
  state.user = null;
  byId("login-message").textContent = message;
  byId("login-modal").classList.remove("hidden");
  byId("btn-session").textContent = "Iniciar sesión";
  byId("btn-logout").classList.add("hidden");
}

function acceptSession(session) {
  state.user = session.user;
  state.csrfToken = session.csrf_token || "";
  byId("login-modal").classList.add("hidden");
  byId("btn-session").textContent = `Sesión: ${session.user}`;
  byId("btn-logout").classList.toggle("hidden", ["development", "test"].includes(session.user));
}

async function apiFetch(path, options = {}) {
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/v1/")) {
    throw new Error("La API debe pertenecer al mismo origen de la aplicación.");
  }
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrfToken) {
    headers.set("X-CSRF-Token", state.csrfToken);
  }
  const response = await nativeFetch(url.href, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
    state.pollingGeneration += 1;
    showLogin("La sesión expiró. Inicia sesión y vuelve a enviar la operación pendiente.");
  }
  return response;
}

async function restoreSession() {
  try {
    const setupResponse = await nativeFetch("/api/v1/auth/setup", { credentials: "same-origin" });
    if (setupResponse.ok) {
      const setup = await setupResponse.json();
      state.registrationRequired = setup.registration_required === true;
      state.localRegistration = setup.local_registration_available === true;
    }
    byId("register-form").classList.toggle("hidden", !state.registrationRequired);
    byId("login-form").classList.toggle("hidden", state.registrationRequired);
    byId("login-title").textContent = state.registrationRequired ? "Crear propietario" : "Iniciar sesión";
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    if (fragment.has("setup")) {
      state.setupToken = fragment.get("setup") || "";
      byId("btn-register").disabled = state.setupToken.length < 20;
      byId("setup-auth-message").textContent = state.setupToken.length >= 20
        ? "Instalación autorizada por este equipo. Elige tu usuario y contraseña."
        : "Falta la autorización. Ejecuta setup-local.ps1; no necesitas inventar ningún código.";
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    const version = await nativeFetch("/api/v1/system/version");
    if (version.ok) {
      const data = await version.json();
      byId("label-provider").textContent = data.environment + " · v" + data.version + " · build " + data.build_id;
    }
    if (state.registrationRequired) {
      byId("btn-register").disabled = !state.localRegistration && state.setupToken.length < 20;
      byId("setup-auth-message").textContent = state.localRegistration
        ? "Estás en tu equipo. Elige tu usuario y una contraseña propia; no necesitas ningún código."
        : "Abre la aplicación desde localhost en el equipo donde la ejecutas para crear el propietario.";
      showLogin("Primera configuración: crea tu propietario."); return;
    }
    const response = await apiFetch("/api/v1/auth/session");
    if (!response.ok) return;
    acceptSession(await response.json());
    await Promise.all([refreshSystemStatus(), refreshDocuments()]);
  } catch (error) { reportOpsProblem("No se pudo comprobar el acceso", String(error)); }
}

function setVisualOpsState(mode, label) {
  ["global-activity-bar", "pill-activity", "live-ops-banner"].forEach((id) => byId(id).setAttribute("data-state", mode));
  document.querySelectorAll(".thinking-inline").forEach((el) => {
    el.setAttribute("data-state", mode);
    el.closest(".msg-assistant")?.setAttribute("data-ops-state", mode);
  });
  byId("label-activity").textContent = label;
  byId("timer-activity").classList.toggle("hidden", mode === "idle");
  byId("btn-cancel-ops").classList.toggle("hidden", !["working", "warning"].includes(mode));
}

function startOpsMonitor(title, detail = "Recuperación híbrida RRF (vectores densos + BM25)…") {
  clearInterval(state.opsTimer);
  state.opsStartMs = performance.now();
  setVisualOpsState("working", "PROCESANDO");
  byId("live-ops-banner").classList.remove("hidden");
  byId("btn-dismiss-ops").classList.add("hidden");
  updateOpsBannerText(title, detail);
  state.opsTimer = setInterval(() => {
    const seconds = (performance.now() - state.opsStartMs) / 1000;
    const elapsed = `${seconds.toFixed(1)}s`;
    byId("live-ops-elapsed").textContent = elapsed;
    byId("timer-activity").textContent = elapsed;
    const stageMsg = seconds < 1.4
      ? "Recuperación híbrida RRF (vectores densos + BM25)…"
      : seconds < 6.0
        ? "Sintetizando respuesta y verificando citas con el modelo…"
        : "Espera prolongada · Generando salida estructurada del modelo…";
    document.querySelectorAll(".thinking-stage-text").forEach((el) => { el.textContent = stageMsg; });
    document.querySelectorAll(".thinking-elapsed-pill").forEach((el) => { el.textContent = elapsed; });
    if (seconds >= 6.0 && byId("live-ops-banner").getAttribute("data-state") === "working") {
      setVisualOpsState("warning", "ESPERA PROLONGADA");
      updateOpsBannerText(title, "Latencia extendida del proveedor · Verificando consistencia…");
    }
  }, 100);
}

function updateOpsBannerText(title, detail) {
  if (title) byId("live-ops-title").textContent = title;
  if (detail) byId("live-ops-detail").textContent = detail;
}

function finishOpsMonitor() {
  clearInterval(state.opsTimer);
  state.opsTimer = null;
  setVisualOpsState("idle", "LISTO");
  byId("live-ops-banner").classList.add("hidden");
}

function reportOpsProblem(title, detail) {
  clearInterval(state.opsTimer);
  state.opsTimer = null;
  setVisualOpsState("error", "REQUIERE ATENCIÓN");
  byId("live-ops-banner").classList.remove("hidden");
  byId("btn-dismiss-ops").classList.remove("hidden");
  updateOpsBannerText(title, detail);
}

function setSendButtonMode(running) {
  byId("btn-send").classList.toggle("is-cancelling", running);
  byId("btn-send").type = running ? "button" : "submit";
  byId("btn-send").textContent = running ? "Detener espera (Esc)" : "Consultar RAG";
}

function cancelActiveOperation() {
  state.operationSeq += 1;
  if (state.activeAbortController) state.activeAbortController.abort();
  state.activeAbortController = null;
  if (state.pollingTimer) {
    clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
    byId("job-status-pill").textContent = "SEGUIMIENTO DETENIDO";
    byId("job-stage-message").textContent = "Se detuvo el seguimiento en este navegador. El trabajo puede continuar en el servidor.";
  }
  state.pollingGeneration += 1;
  setSendButtonMode(false);
  finishOpsMonitor();
}

function beginOperation(title) {
  cancelActiveOperation();
  const controller = new AbortController();
  state.activeAbortController = controller;
  startOpsMonitor(title);
  return { controller, id: state.operationSeq };
}

async function refreshSystemStatus() {
  const response = await apiFetch("/api/v1/system/status");
  if (!response.ok) return;
  const data = await response.json();
  state.credentialsEditable = data.credentials_editable === true;
  for (const id of ["input-gemini-token", "input-openai-token", "input-openai-endpoint", "input-ollama-endpoint", "input-anthropic-token"]) {
    byId(id).disabled = !state.credentialsEditable;
    if (!state.credentialsEditable) {
      byId(id).value = "";
      byId(id).placeholder = "Configurado de forma privada en el servidor";
    }
  }
  byId("provider-config-note").textContent = state.credentialsEditable
    ? "Configura como propietario: claves privadas persistentes, sin incluirlas en Git."
    : "Servidor: claves y endpoints se configuran en .env y requieren recrear API/worker. Aquí solo eliges proveedor y modelo.";
  byId("btn-preset-groq-qwen").disabled = !state.credentialsEditable && data.openai_backend !== "groq";
  byId("btn-preset-groq").disabled = !state.credentialsEditable && data.openai_backend !== "groq";
  byId("btn-preset-openai").disabled = !state.credentialsEditable && data.openai_backend !== "openai";
  byId("label-provider").textContent = (data.environment || "test") + " · v" + (data.app_version || "4") + " · " + (data.build_id || "fixture") + " · " + data.llm_model;
  byId("dot-provider").className = data.provider_ready === false ? "status-dot err" : "status-dot ok";
  byId("label-vector").textContent = `${data.vector_backend} (${data.total_chunks} chunks)`;
  byId("label-cb").textContent = String(data.circuit_breaker_state).toUpperCase();
  byId("dot-cb").className = `status-dot ${data.circuit_breaker_state === "closed" ? "ok" : "err"}`;
  byId("embedding-model-note").textContent = `Embeddings fijos: ${data.embedding_model}. Cambiar el generador no modifica el índice.`;
  if (data.openai_base_url) byId("input-openai-endpoint").value = data.openai_base_url;
  if (data.ollama_base_url) byId("input-ollama-endpoint").value = data.ollama_base_url;
  byId("input-supports-images").checked = data.supports_images === true;
  byId("input-response-format").value = data.response_format || "auto";
  const cleanModel = String(data.llm_model || "").replace(/^(openai|groq|gemini|ollama|anthropic)\//, "");
  if (data.llm_provider === "openai" && cleanModel) byId("input-openai-model").value = cleanModel;
  else if (data.llm_provider === "gemini" && cleanModel) byId("input-gemini-model").value = cleanModel;
  else if (data.llm_provider === "ollama" && cleanModel) byId("input-ollama-model").value = cleanModel;
  if (data.llm_provider === "anthropic" && cleanModel) byId("input-anthropic-model").value = cleanModel;
  const repair = byId("btn-recover-index");
  repair.classList.toggle("hidden", data.index_compatible !== false);
  repair.disabled = data.migration_status === "running";
  repair.textContent = repair.disabled ? "Recuperando índice…" : "Recuperar índice anterior";
  const radio = document.querySelector(`input[name="llm_provider"][value="${data.llm_provider}"]`);
  if (radio) { radio.checked = true; syncProviderFields(data.llm_provider); }
}

function syncProviderFields(selected) {
  ["gemini", "ollama", "openai", "anthropic"].forEach((provider) => byId(`fields-${provider}`).classList.toggle("hidden", selected !== provider));
}

async function refreshDocuments() {
  const response = await apiFetch("/api/v1/documents");
  if (!response.ok) return;
  const documents = await response.json();
  byId("doc-count-badge").textContent = `${documents.length} PDFs`;
  const list = byId("doc-list");
  list.replaceChildren();
  if (!documents.length) list.append(node("div", "citations-placeholder", "No hay documentos. Carga un PDF o el manual de ejemplo."));
  for (const doc of documents) {
    const card = node("div", "doc-card");
    const top = node("div", "doc-card-top");
    const name = node("div", "doc-name", `📄 ${doc.file_name}`);
    name.title = doc.file_name;
    const remove = node("button", "doc-delete-btn", "✕");
    remove.type = "button";
    remove.title = "Eliminar documento";
    remove.addEventListener("click", () => deleteDocument(doc.document_id));
    top.append(name, remove);
    const metrics = node("div", "doc-metrics");
    for (const [key, label] of [["page_count", "Páginas"], ["chunk_count", "Chunks"], ["visual_count", "Figuras"], ["table_count", "Fragmentos de tabla"]]) {
      const item = node("div", "doc-metric");
      item.append(node("strong", "", doc[key]), node("span", "", label));
      metrics.append(item);
    }
    const hash = node("div", "doc-hash mono");
    hash.append(node("span", "", `SHA256: ${String(doc.sha256).slice(0, 10)}…`), node("span", "", doc.status));
    card.append(top, metrics, hash);
    list.append(card);
  }
}

async function deleteDocument(documentId) {
  try {
    const response = await apiFetch(`/api/v1/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" });
    if (!response.ok) throw new Error(errorText(await response.json(), `HTTP ${response.status}`));
    await Promise.all([refreshDocuments(), refreshSystemStatus()]);
  } catch (error) { reportOpsProblem("No se pudo eliminar el documento", String(error)); }
}

function showJobError(message) {
  byId("job-status-pill").textContent = "ERROR";
  byId("job-status-pill").className = "job-status-pill failed";
  byId("job-stage-message").textContent = message;
  reportOpsProblem("No se pudo completar la ingesta", message);
}

async function uploadPdfFile(file) {
  const operation = beginOperation(`Enviando PDF: ${file.name}`);
  byId("job-tracker").classList.remove("hidden");
  byId("job-filename").textContent = file.name;
  byId("job-stage-message").textContent = "Enviando archivo al servidor…";
  byId("job-progress-fill").style.width = "0%";
  byId("job-progress-pct").textContent = "0%";
  const form = new FormData();
  form.append("file", file);
  try {
    const response = await apiFetch("/api/v1/documents/ingest", { method: "POST", body: form, signal: operation.controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, `HTTP ${response.status}`));
    if (!operation.controller.signal.aborted) startJobPolling(data.job_id, data.file_name);
  } catch (error) {
    if (error.name !== "AbortError") showJobError(String(error));
  } finally {
    if (state.activeAbortController === operation.controller) state.activeAbortController = null;
  }
}

async function seedDemoManual() {
  const current = await apiFetch("/api/v1/system/status");
  if (current.ok && (await current.json()).index_compatible === false) {
    reportOpsProblem("El índice necesita recuperación", "Usa Recuperar índice anterior; se conservarán tus documentos.");
    return;
  }
  const operation = beginOperation("Solicitando manual de demostración");
  byId("job-tracker").classList.remove("hidden");
  try {
    const response = await apiFetch("/api/v1/documents/seed-sample?force=true", { method: "POST", signal: operation.controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, `HTTP ${response.status}`));
    if (!operation.controller.signal.aborted) startJobPolling(data.job_id, data.file_name);
  } catch (error) {
    if (error.name !== "AbortError") showJobError(String(error));
  } finally {
    if (state.activeAbortController === operation.controller) state.activeAbortController = null;
  }
}

function startJobPolling(jobId, fileName) {
  clearTimeout(state.pollingTimer);
  const generation = ++state.pollingGeneration;
  byId("job-tracker").classList.remove("hidden");
  byId("job-id-label").textContent = jobId;
  byId("job-filename").textContent = fileName;
  const poll = async () => {
    try {
      const response = await apiFetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== state.pollingGeneration) return;
      if (!response.ok) throw new Error(`No se pudo consultar el trabajo (HTTP ${response.status}).`);
      const job = await response.json();
      if (generation !== state.pollingGeneration) return;
      const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
      byId("job-status-pill").textContent = job.status;
      byId("job-status-pill").className = `job-status-pill ${["pending", "processing", "completed", "failed"].includes(job.status) ? job.status : ""}`;
      byId("job-progress-fill").style.width = `${progress}%`;
      byId("job-progress-pct").textContent = `${Math.round(progress)}%`;
      byId("job-stage-message").textContent = job.error || job.message;
      updateOpsBannerText(`Seguimiento de ingesta · ${fileName}`, job.message);
      if (["completed", "failed"].includes(job.status)) {
        state.pollingTimer = null;
        if (job.status === "failed") reportOpsProblem("La ingesta falló", job.error || job.message);
        else finishOpsMonitor();
        await Promise.all([refreshDocuments(), refreshSystemStatus()]);
        return;
      }
      state.pollingTimer = setTimeout(poll, 750);
    } catch (error) {
      if (generation === state.pollingGeneration) reportOpsProblem("Seguimiento interrumpido", String(error));
      state.pollingTimer = null;
    }
  };
  // Sequential polling avoids overlapping requests and late updates after stop.
  state.pollingTimer = setTimeout(poll, 0);
}

function syncChatScrollControls() {
  const stream = byId("chat-stream");
  const countBadge = byId("chat-msg-count");
  const jumpBtn = byId("btn-jump-latest");
  if (!stream) return;
  const answers = stream.querySelectorAll(".msg-assistant[data-message-id]");
  if (countBadge) {
    const n = answers.length;
    countBadge.textContent = `${n} ${n === 1 ? "respuesta" : "respuestas"}`;
  }
  if (jumpBtn) {
    const distanceFromBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight;
    jumpBtn.classList.toggle("hidden", distanceFromBottom <= 72);
  }
}

function toggleQuickPrompts(forceCollapse) {
  const wrap = byId("quick-prompts");
  const btn = byId("btn-toggle-prompts");
  if (!wrap || !btn) return;
  const collapsed = typeof forceCollapse === "boolean" ? forceCollapse : !wrap.classList.contains("collapsed");
  wrap.classList.toggle("collapsed", collapsed);
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.textContent = collapsed ? "Mostrar sugerencias ▾" : "Ocultar sugerencias ▴";
}

function scrollToChatEdge(position) {
  const stream = byId("chat-stream");
  if (!stream) return;
  stream.scrollTo({ top: position === "top" ? 0 : stream.scrollHeight, behavior: "smooth" });
}

function navigateChatMessage(direction) {
  const stream = byId("chat-stream");
  if (!stream) return;
  const cards = Array.from(stream.querySelectorAll(".msg-assistant[data-message-id]"));
  if (!cards.length) return;
  let idx = cards.findIndex((el) => el.dataset.messageId === state.activeResponseId);
  if (idx === -1) idx = direction > 0 ? 0 : cards.length - 1;
  else idx = Math.max(0, Math.min(cards.length - 1, idx + direction));
  const target = cards[idx];
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "nearest" });
  const msgId = target.dataset.messageId;
  const data = state.responses.get(msgId);
  if (data) updateRightPanel(data, msgId);
}

async function submitQuestion(question) {
  const operation = beginOperation("Consulta enviada al servidor");
  const stream = byId("chat-stream");
  stream.querySelector(".welcome-card")?.remove();
  toggleQuickPrompts(true);
  stream.append(node("div", "msg msg-user", question));
  const message = node("div", "msg msg-assistant");
  message.setAttribute("data-ops-state", "working");
  const thinking = node("div", "thinking-inline");
  thinking.setAttribute("data-state", "working");
  const left = node("div", "thinking-inline-left");
  const bars = node("span", "thinking-bars");
  bars.setAttribute("aria-hidden", "true");
  bars.append(node("i"), node("i"), node("i"));
  left.append(bars, node("span", "mono thinking-stage-text", "Recuperación híbrida RRF (vectores densos + BM25)…"));
  const right = node("div", "thinking-inline-right");
  right.append(node("span", "mono thinking-elapsed-pill", "0.0s"));
  const stop = node("button", "thinking-cancel-btn", "Detener espera");
  stop.type = "button";
  stop.addEventListener("click", cancelActiveOperation);
  right.append(stop);
  thinking.append(left, right);
  message.append(thinking);
  stream.append(message);
  setSendButtonMode(true);
  stream.scrollTop = stream.scrollHeight;
  syncChatScrollControls();
  try {
    const response = await apiFetch("/api/v1/chat/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: state.history.slice(-6) }), signal: operation.controller.signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, `HTTP ${response.status}`));
    if (operation.controller.signal.aborted) return;
    state.history.push({ role: "user", content: question.slice(0, 4000) }, { role: "assistant", content: String(data.answer).slice(0, 4000) });
    const messageId = `answer-${++state.messageSeq}`;
    state.responses.set(messageId, data);
    message.removeAttribute("data-ops-state");
    renderAssistantMessage(message, data, messageId);
    updateRightPanel(data, messageId);
    if (operation.id === state.operationSeq) finishOpsMonitor();
    await refreshSystemStatus().catch(() => {});
  } catch (error) {
    message.replaceChildren();
    if (error.name === "AbortError") {
      message.removeAttribute("data-ops-state");
      message.append(node("span", "badge-cancelled", "ESPERA DETENIDA"), node("p", "", "Se dejó de esperar en este navegador. La solicitud puede continuar en el servidor."));
      if (!byId("chat-input").value.trim()) byId("chat-input").value = question;
    } else {
      message.setAttribute("data-ops-state", "error");
      const errHeader = node("div", "thinking-error-header");
      errHeader.append(node("span", "thinking-error-dot"), node("span", "badge-error-state mono", "REQUIERE ATENCIÓN"));
      const retryBtn = node("button", "retry-query-btn", "↻ Reintentar consulta");
      retryBtn.type = "button";
      retryBtn.addEventListener("click", () => submitQuestion(question));
      message.append(errHeader, node("p", "", String(error)), retryBtn);
      if (operation.id === state.operationSeq) reportOpsProblem("La consulta no se completó", String(error));
    }
  } finally {
    if (state.activeAbortController === operation.controller) {
      state.activeAbortController = null;
      setSendButtonMode(false);
    }
    stream.scrollTop = stream.scrollHeight;
    syncChatScrollControls();
  }
}

function safeImageUrl(value) {
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && /^\/api\/v1\/(assets|pages)\//.test(url.pathname) ? url.href : "";
  } catch (_) { return ""; }
}

function setImage(image, value) {
  const url = safeImageUrl(value);
  if (url) image.src = url;
  else image.removeAttribute("src");
}

function renderMarkdown(answer, citations, messageId) {
  const wrapper = node("div", "md-body");
  if (!window.marked || !window.DOMPurify) {
    wrapper.textContent = answer;
    return wrapper;
  }
  wrapper.append(window.DOMPurify.sanitize(window.marked.parse(String(answer)), {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "ul", "ol", "li", "blockquote", "pre", "code", "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td", "hr", "a"],
    ALLOWED_ATTR: ["href", "title", "colspan", "rowspan"], ALLOW_DATA_ATTR: false,
  }));
  wrapper.querySelectorAll("a").forEach((anchor) => {
    try {
      const url = new URL(anchor.getAttribute("href"), location.origin);
      if (!["https:", "http:"].includes(url.protocol)) anchor.removeAttribute("href");
      else { anchor.rel = "noopener noreferrer"; anchor.target = "_blank"; }
    } catch (_) { anchor.removeAttribute("href"); }
  });
  const refs = new Set(citations.map((c) => Number(c.ref)));
  const walker = document.createTreeWalker(wrapper, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  for (const text of texts) {
    if (text.parentElement.closest("code,pre,a")) continue;
    const matcher = /\[(\d+)\]/g;
    let match, start = 0;
    const fragment = document.createDocumentFragment();
    while ((match = matcher.exec(text.textContent))) {
      fragment.append(document.createTextNode(text.textContent.slice(start, match.index)));
      const ref = Number(match[1]);
      if (refs.has(ref)) {
        const button = node("button", "inline-cite-btn", `[${ref}]`);
        button.type = "button";
        button.dataset.messageId = messageId;
        button.dataset.ref = String(ref);
        button.addEventListener("click", () => focusCitation(ref, messageId));
        fragment.append(button);
      } else fragment.append(document.createTextNode(match[0]));
      start = matcher.lastIndex;
    }
    if (start) {
      fragment.append(document.createTextNode(text.textContent.slice(start)));
      text.replaceWith(fragment);
    }
  }
  return wrapper;
}

function visualStatus(visual, data = state.responses.get(state.activeResponseId)) {
  const meta = data?.metadata || {};
  if ((meta.used_asset_ids || []).includes(visual.asset_id)) return "Enviada; señalada por el modelo";
  if ((meta.sent_asset_ids || []).includes(visual.asset_id)) return "Enviada al modelo";
  return "Relacionada; no enviada al modelo";
}

function buildMetricPill(label, value, subtext, barRatio = null, tone = "cyan") {
  const pill = node("div", "metric-pill");
  pill.append(node("span", "metric-label", label));
  pill.append(node("strong", "metric-val mono", value));
  if (barRatio !== null && Number.isFinite(Number(barRatio))) {
    const track = node("div", "metric-bar-track");
    const fill = node("div", `metric-bar-fill tone-${tone}`);
    fill.style.width = `${Math.round(Math.max(0, Math.min(1, Number(barRatio))) * 100)}%`;
    track.append(fill);
    pill.append(track);
  }
  if (subtext) pill.append(node("span", "metric-sub", subtext));
  return pill;
}

function renderAssistantMessage(container, data, messageId) {
  container.replaceChildren();
  container.dataset.messageId = messageId;
  container.classList.toggle("abstained", Boolean(data.abstained));
  const top = node("div", "msg-meta-top");
  const topLeft = node("div", "msg-meta-left");
  const mode = data.answer_mode === "extractive_evidence" ? "EVIDENCIA EXTRACTIVA · SIN INFERENCIA VISUAL" : "RESPUESTA CON REFERENCIAS";
  topLeft.append(node("span", data.abstained ? "badge-abstained" : "badge-grounded", data.abstained ? "ABSTENCIÓN" : mode));
  topLeft.append(node("span", "mono msg-meta-summary", `${data.model} · ${data.latency_ms} ms · cobertura declarada: ${data.evidence_coverage}`));
  const topRight = node("div", "msg-meta-right");
  const inspectBtn = node("button", "msg-inspect-btn", "Inspeccionar →");
  inspectBtn.type = "button";
  inspectBtn.title = "Cargar citas y diagramas de esta respuesta en el panel derecho";
  inspectBtn.addEventListener("click", () => updateRightPanel(data, messageId));
  topRight.append(inspectBtn);
  top.append(topLeft, topRight);
  container.append(top, renderMarkdown(data.answer, data.citations || [], messageId));

  if (data.primary_visual && !data.abstained) {
    const visual = data.primary_visual;
    const card = node("button", "inline-visual-card");
    card.type = "button";
    const image = node("img");
    image.alt = visual.caption || "Diagrama relacionado";
    setImage(image, visual.image_url);
    const info = node("div", "inline-visual-info");
    info.append(node("strong", "", `${visual.document_name}, página ${visual.page}`), node("div", "", visual.caption), node("div", "mono", `${visualStatus(visual, data)} · d_bbox=${numberText(visual.distance)}`));
    card.append(image, info);
    card.addEventListener("click", () => { updateRightPanel(data, messageId); openLightbox(visual.image_url, visual.caption); });
    container.append(card);
  }

  const citations = data.citations || [];
  const sources = node("div", "msg-sources-bar");
  for (const citation of citations) {
    const button = node("button", "source-chip", `[${citation.ref}] ${citation.document_name}, página ${citation.page}`);
    button.type = "button";
    button.dataset.messageId = messageId;
    button.dataset.ref = String(citation.ref);
    button.addEventListener("click", () => focusCitation(citation.ref, messageId));
    sources.append(button);
  }
  container.append(sources);

  const metrics = data.metrics || {};
  const meta = data.metadata || {};
  const timings = data.timings_ms || {};
  const retrievedCount = Number.isFinite(Number(data.retrieved)) ? Number(data.retrieved) : citations.length;
  const acceptedClaims = meta.accepted_claims ?? citations.length;
  const rejectedClaims = meta.rejected_claims ?? 0;
  const retrievalMs = timings.retrieval_ms ?? 0;
  const generationMs = timings.generation_ms ?? Math.max(0, (Number(data.latency_ms) || 0) - retrievalMs);
  const totalMs = Number(data.latency_ms) || (retrievalMs + generationMs);
  const lexicalVal = metrics.lexical_score ?? metrics.max_bm25_score;
  const imagesSent = metrics.images_sent ?? (meta.sent_asset_ids || []).length;

  const metricsGrid = node("div", "rag-metrics-grid");
  metricsGrid.setAttribute("aria-label", "Métricas de verificación del modelo y recuperación");
  metricsGrid.append(
    buildMetricPill(
      "COSENO MÁX / MEDIA",
      `${numberText(metrics.max_cosine_similarity)} · μ=${numberText(metrics.mean_cosine_similarity)}`,
      "Similitud semántica densa (L2)",
      metrics.max_cosine_similarity,
      "cyan",
    ),
    buildMetricPill(
      "SCORE LÉXICO BM25",
      numberText(lexicalVal),
      "Coincidencia exacta de tokens",
      lexicalVal != null ? Math.min(1, Number(lexicalVal) / 8) : null,
      "emerald",
    ),
    buildMetricPill(
      "FUSIÓN HÍBRIDA RRF",
      numberText(metrics.top_rrf_score, 5),
      "Reciprocal Rank Fusion (k=60)",
      metrics.top_rrf_score != null ? Math.min(1, Number(metrics.top_rrf_score) / 0.033) : null,
      "indigo",
    ),
    buildMetricPill(
      "SOLAPAMIENTO LÉXICO",
      numberText(metrics.lexical_overlap_score),
      "Términos anclados en evidencia",
      metrics.lexical_overlap_score,
      "emerald",
    ),
    buildMetricPill(
      "USO DE CONTEXTO",
      `${citations.length}/${retrievedCount} (${numberText(metrics.context_utilization)})`,
      "Chunks citados vs recuperados",
      metrics.context_utilization,
      "cyan",
    ),
    buildMetricPill(
      "GUARDRAILS CLAIMS",
      `${acceptedClaims} ok · ${rejectedClaims} filtradas`,
      `Validez ref: ${numberText(metrics.reference_validity)}`,
      metrics.reference_validity != null ? metrics.reference_validity : (acceptedClaims > 0 ? 1 : 0),
      rejectedClaims > 0 ? "amber" : "emerald",
    ),
    buildMetricPill(
      "LATENCIA PIPELINE",
      `R:${retrievalMs}ms · G:${generationMs}ms`,
      `Total end-to-end: ${totalMs} ms`,
      totalMs > 0 ? generationMs / totalMs : 0,
      "indigo",
    ),
    buildMetricPill(
      "ALINEACIÓN BBOX / VLM",
      `Prox: ${numberText(metrics.visual_proximity_score)} · ${imagesSent} img`,
      meta.supports_images ? "Inferencia visual activa" : "Mapeo geométrico BBox",
      metrics.visual_proximity_score,
      "cyan",
    ),
  );
  container.append(metricsGrid);

  const details = node("details", "operational-scores");
  details.append(node("summary", "", "Diagnóstico detallado de recuperación y auditoría del modelo (no mide veracidad)"));
  details.append(node("p", "mono", `Coseno: ${numberText(metrics.max_cosine_similarity)} · Léxico: ${numberText(lexicalVal)} · RRF: ${numberText(metrics.top_rrf_score, 5)}`));
  const traceRow = node("div", "audit-trace-row mono");
  traceRow.textContent = `Checks: ${meta.checks || "references_spans_numeric_consistency"} · Modo: ${data.answer_mode || "grounded_answer"} · Perfil Emb: ${meta.embedding_profile_id || "activo"} · Gen: ${meta.generation_id || "activa"}`;
  details.append(traceRow);

  if (citations.length) {
    const tableWrap = node("div", "audit-citations-wrap");
    for (const c of citations) {
      const row = node("div", "audit-citation-row mono");
      row.append(
        node("span", "audit-cite-ref", `[${c.ref}] ${c.modality || c.kind || "text"}`),
        node("span", "audit-cite-doc", `${c.document_name} (p.${c.page}) · ${c.section}`),
        node("span", "audit-cite-scores", `Cos=${numberText(c.dense_score)} · BM25=${numberText(c.sparse_score)} · RRF=${numberText(c.score, 5)} · BBox=${boxText(c.bbox)}`),
      );
      tableWrap.append(row);
    }
    details.append(tableWrap);
  }

  details.append(node("p", "", "Las referencias y los scores ayudan a inspeccionar la recuperación. La correspondencia semántica de cada afirmación requiere revisión."));
  container.append(details);
}

function updateRightPanel(data, messageId) {
  state.activeResponseId = messageId;
  document.querySelectorAll(".msg-assistant[data-message-id]").forEach((el) => {
    el.classList.toggle("active-inspected", el.dataset.messageId === messageId);
  });
  state.activeCitations = data.citations || [];
  const visuals = [data.primary_visual, ...(data.related_visuals || [])].filter(Boolean);
  state.relatedVisuals = visuals.filter((v, i) => visuals.findIndex((other) => other.asset_id === v.asset_id && other.image_url === v.image_url) === i);
  state.activeVisual = data.primary_visual || null;
  renderCitationsList();
  if (state.activeVisual) renderVisualStage(state.activeVisual);
  else if (state.activeCitations.length) renderCitationOnlyPageOverlay(state.activeCitations[0]);
  else clearVisualStage();
}

function renderVisualStage(visual) {
  state.activeVisual = visual;
  byId("visual-empty").classList.add("hidden");
  byId("visual-content").classList.remove("hidden");
  setImage(byId("visual-crop-img"), visual.image_url);
  setImage(byId("visual-page-img"), visual.page_image_url);
  byId("vis-source-pill").textContent = `${visual.document_name}, página ${visual.page}`;
  byId("vis-distance-pill").textContent = `d_bbox = ${numberText(visual.distance)} (${visual.reason})`;
  byId("vis-caption").textContent = `${visual.caption || "Diagrama"} · ${visualStatus(visual)}`;
  const anchor = state.activeCitations.find((c) => c.chunk_id === visual.anchor_chunk_id);
  const samePage = anchor ? anchor.page_image_url === visual.page_image_url : visual.distance !== null;
  const textBox = samePage ? visual.anchor_bbox : null;
  byId("vis-coords").textContent = `BBox figura: ${boxText(visual.bbox)} · Texto: ${textBox ? boxText(textBox) : "Referencia en otra página; sin distancia física"}`;
  byId("legend-fig-bbox").textContent = boxText(visual.bbox);
  byId("legend-txt-bbox").textContent = boxText(textBox);
  drawBoundingBoxesSvg(visual.bbox, textBox);
  byId("related-strip").replaceChildren();
  for (const related of state.relatedVisuals) {
    const thumb = node("button", `related-thumb ${related.image_url === visual.image_url ? "active" : ""}`);
    thumb.type = "button";
    thumb.title = `${related.caption || "Diagrama"} · ${visualStatus(related)}`;
    const img = node("img");
    img.alt = related.caption || "Diagrama relacionado";
    setImage(img, related.image_url);
    thumb.append(img);
    thumb.addEventListener("click", () => renderVisualStage(related));
    byId("related-strip").append(thumb);
  }
}

function drawBoundingBoxesSvg(figBox, textBox) {
  const svg = byId("bbox-svg");
  svg.replaceChildren();
  for (const [box, color] of [[textBox, "#06b6d4"], [figBox, "#10b981"]]) {
    if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite)) continue;
    const [x0, y0, x1, y1] = box.map((value) => Math.max(0, Math.min(1, value)) * 1000);
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    for (const [key, value] of Object.entries({ x: x0, y: y0, width: Math.max(0, x1 - x0), height: Math.max(0, y1 - y0), fill: color, "fill-opacity": .15, stroke: color, "stroke-width": 8 })) rect.setAttribute(key, String(value));
    svg.append(rect);
  }
}

function renderCitationOnlyPageOverlay(citation) {
  state.activeVisual = null;
  byId("visual-empty").classList.add("hidden");
  byId("visual-content").classList.remove("hidden");
  byId("visual-crop-img").removeAttribute("src");
  setImage(byId("visual-page-img"), citation.page_image_url);
  byId("vis-source-pill").textContent = `${citation.document_name}, página ${citation.page}`;
  byId("vis-distance-pill").textContent = `Cita [${citation.ref}] · sin figura vinculada`;
  byId("vis-caption").textContent = `${citation.section} · ${citation.snippet}`;
  byId("vis-coords").textContent = `BBox de la cita: ${boxText(citation.bbox)}`;
  byId("legend-fig-bbox").textContent = "N/A";
  byId("legend-txt-bbox").textContent = boxText(citation.bbox);
  byId("related-strip").replaceChildren();
  drawBoundingBoxesSvg(null, citation.bbox);
  setVisualTab("page");
}

function renderCitationsList() {
  const list = byId("citations-list");
  list.replaceChildren();
  byId("citations-count-badge").textContent = `${state.activeCitations.length} citas`;
  byId("btn-clear-citations").classList.toggle("hidden", !state.activeCitations.length);
  if (!state.activeCitations.length) list.append(node("div", "citations-placeholder", "Selecciona una respuesta o una referencia para inspeccionar su procedencia."));
  for (const citation of state.activeCitations) {
    const card = node("button", "citation-card");
    card.type = "button";
    card.dataset.ref = String(citation.ref);
    const top = node("div", "citation-top");
    top.append(node("span", "citation-source", `[${citation.ref}] ${citation.document_name}, página ${citation.page}`), node("span", "citation-score mono", `Cos=${numberText(citation.dense_score)} · Léxico=${numberText(citation.sparse_score)} · RRF=${numberText(citation.score, 5)}`));
    card.append(top, node("div", "citation-section", `${citation.section} (${citation.kind})`), node("div", "citation-snippet", citation.snippet), node("div", "visual-coords mono", `BBox: ${boxText(citation.bbox)}`));
    const messageId = state.activeResponseId;
    card.addEventListener("click", () => focusCitation(citation.ref, messageId));
    list.append(card);
  }
}

function focusCitation(ref, messageId = state.activeResponseId) {
  const data = state.responses.get(messageId);
  if (!data) return;
  const citation = (data.citations || []).find((item) => item.ref === Number(ref));
  if (!citation) return;
  updateRightPanel(data, messageId);
  const card = Array.from(byId("citations-list").children).find((el) => el.dataset.ref === String(ref));
  if (card) { card.classList.add("active"); card.scrollIntoView({ behavior: "smooth", block: "nearest" }); }
  const linked = state.relatedVisuals.find((visual) => visual.anchor_chunk_id === citation.chunk_id);
  if (linked) { renderVisualStage(linked); setVisualTab("page"); }
  else renderCitationOnlyPageOverlay(citation);
}

function clearVisualStage() {
  state.activeVisual = null;
  byId("visual-empty").classList.remove("hidden");
  byId("visual-content").classList.add("hidden");
  byId("visual-crop-img").removeAttribute("src");
  byId("visual-page-img").removeAttribute("src");
  byId("bbox-svg").replaceChildren();
  byId("related-strip").replaceChildren();
}

function clearRightInspector() {
  state.activeResponseId = null;
  state.activeCitations = [];
  state.relatedVisuals = [];
  clearVisualStage();
  renderCitationsList();
}

function setVisualTab(view) {
  state.activeView = view;
  byId("tab-crop").classList.toggle("active", view === "crop");
  byId("tab-page").classList.toggle("active", view === "page");
  byId("view-crop-wrap").classList.toggle("hidden", view !== "crop");
  byId("view-page-wrap").classList.toggle("hidden", view !== "page");
}

function openLightbox(url, caption) {
  if (!safeImageUrl(url)) return;
  setImage(byId("lightbox-img"), url);
  byId("lightbox-caption").textContent = caption || "";
  byId("lightbox-modal").classList.remove("hidden");
}

document.addEventListener("DOMContentLoaded", () => {
  restoreSession();
  const dropzone = byId("dropzone"), fileInput = byId("file-input");
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (event) => { event.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); if (event.dataTransfer.files[0]) uploadPdfFile(event.dataTransfer.files[0]); });
  fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadPdfFile(fileInput.files[0]); fileInput.value = ""; });
  byId("btn-seed-demo").addEventListener("click", seedDemoManual);
  document.querySelectorAll(".prompt-chip").forEach((chip) => chip.addEventListener("click", () => submitQuestion(chip.dataset.query)));
  byId("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = byId("chat-input").value.trim();
    if (question) { byId("chat-input").value = ""; submitQuestion(question); }
  });
  byId("btn-send").addEventListener("click", (event) => {
    if (state.activeAbortController) { event.preventDefault(); cancelActiveOperation(); }
  });
  byId("chat-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (!state.activeAbortController) byId("chat-form").requestSubmit(); } });
  byId("btn-clear-chat").addEventListener("click", () => {
    cancelActiveOperation();
    state.history = [];
    state.responses.clear();
    byId("chat-stream").replaceChildren();
    toggleQuickPrompts(false);
    clearRightInspector();
    syncChatScrollControls();
  });
  byId("chat-stream").addEventListener("scroll", syncChatScrollControls, { passive: true });
  byId("btn-toggle-prompts")?.addEventListener("click", () => toggleQuickPrompts());
  byId("btn-scroll-top")?.addEventListener("click", () => scrollToChatEdge("top"));
  byId("btn-scroll-bottom")?.addEventListener("click", () => scrollToChatEdge("bottom"));
  byId("btn-jump-latest")?.addEventListener("click", () => scrollToChatEdge("bottom"));
  byId("btn-prev-msg")?.addEventListener("click", () => navigateChatMessage(-1));
  byId("btn-next-msg")?.addEventListener("click", () => navigateChatMessage(1));
  byId("btn-clear-inspector").addEventListener("click", clearRightInspector);
  byId("btn-clear-citations").addEventListener("click", () => { state.activeCitations = []; renderCitationsList(); });
  byId("btn-cancel-ops").addEventListener("click", cancelActiveOperation);
  byId("btn-dismiss-ops").addEventListener("click", finishOpsMonitor);
  byId("tab-crop").addEventListener("click", () => setVisualTab("crop"));
  byId("tab-page").addEventListener("click", () => setVisualTab("page"));
  byId("btn-zoom-image").addEventListener("click", () => { if (state.activeVisual) openLightbox(state.activeVisual.image_url, state.activeVisual.caption); });
  byId("btn-close-lightbox").addEventListener("click", () => byId("lightbox-modal").classList.add("hidden"));
  byId("btn-theme-toggle").addEventListener("click", () => { document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; });
  byId("btn-open-settings").addEventListener("click", () => byId("settings-modal").classList.remove("hidden"));
  ["btn-close-settings", "btn-cancel-settings"].forEach((id) => byId(id).addEventListener("click", () => byId("settings-modal").classList.add("hidden")));
  document.querySelectorAll('input[name="llm_provider"]').forEach((radio) => radio.addEventListener("change", () => syncProviderFields(radio.value)));
  byId("btn-preset-groq-qwen").addEventListener("click", () => {
    byId("input-openai-endpoint").value = "https://api.groq.com/openai/v1";
    byId("input-openai-model").value = "qwen/qwen3.8-27b";
  });
  byId("btn-preset-groq").addEventListener("click", () => {
    byId("input-openai-endpoint").value = "https://api.groq.com/openai/v1";
    byId("input-openai-model").value = "llama-3.3-70b-versatile";
  });
  byId("btn-preset-openai").addEventListener("click", () => {
    byId("input-openai-endpoint").value = "https://api.openai.com/v1";
    byId("input-openai-model").value = "gpt-4o-mini";
  });
  byId("btn-preset-xai").addEventListener("click", () => {
    byId("input-openai-endpoint").value = "https://api.x.ai/v1";
    byId("input-openai-model").value = "";
    byId("input-openai-model").placeholder = "Modelo exacto disponible en tu cuenta xAI";
  });
  byId("btn-preset-lmstudio").addEventListener("click", () => {
    byId("input-openai-endpoint").value = "http://localhost:1234/v1";
    byId("input-openai-model").value = "";
    byId("input-openai-model").placeholder = "Identificador del modelo cargado en tu servidor";
  });
  byId("provider-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const provider = document.querySelector('input[name="llm_provider"]:checked').value;
    const payload = { llm_provider: provider };
    if (provider !== "local") payload[`${provider}_model`] = byId(`input-${provider}-model`).value.trim();
    if (state.credentialsEditable && provider === "gemini" && byId("input-gemini-token").value.trim()) payload.gemini_api_key = byId("input-gemini-token").value.trim();
    if (state.credentialsEditable && provider === "ollama" && byId("input-ollama-endpoint").value.trim()) payload.ollama_base_url = byId("input-ollama-endpoint").value.trim();
    if (state.credentialsEditable && provider === "openai") {
      if (byId("input-openai-endpoint").value.trim()) payload.openai_base_url = byId("input-openai-endpoint").value.trim();
      if (byId("input-openai-token").value.trim()) payload.openai_api_key = byId("input-openai-token").value.trim();
    }
    if (state.credentialsEditable && provider === "anthropic" && byId("input-anthropic-token").value.trim()) payload.anthropic_api_key = byId("input-anthropic-token").value.trim();
    if (provider !== "local") {
      payload.llm_supports_images = byId("input-supports-images").checked;
      payload.openai_response_format = byId("input-response-format").value;
      payload.approve_endpoint = byId("input-approve-endpoint").checked;
    }
    byId("input-anthropic-token").value = "";
    byId("input-gemini-token").value = "";
    byId("input-openai-token").value = "";
    const operation = beginOperation("Aplicando generador configurado en el servidor");
    byId("settings-modal").classList.add("hidden");
    try {
      const response = await apiFetch("/api/v1/system/provider", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: operation.controller.signal });
      if (!response.ok) throw new Error(errorText(await response.json(), `HTTP ${response.status}`));
      if (operation.id === state.operationSeq) finishOpsMonitor();
      await refreshSystemStatus();
    } catch (error) { if (error.name !== "AbortError") reportOpsProblem("No se cambió el generador", String(error)); }
    finally { if (state.activeAbortController === operation.controller) state.activeAbortController = null; }
  });
  byId("register-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.localRegistration && state.setupToken.length < 20) {
      byId("login-message").textContent = "Abre la aplicación desde localhost en el equipo donde la ejecutas para crear el propietario.";
      return;
    }
    const password = byId("register-password").value;
    const username = byId("register-username").value.trim();
    if (password !== byId("register-confirm").value) { byId("login-message").textContent = "Las contraseñas no coinciden."; return; }
    byId("btn-register").disabled = true;
    try {
      const response = await nativeFetch("/api/v1/auth/register", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, setup_token: state.setupToken }) });
      const result = await response.json();
      if (!response.ok) throw new Error(errorText(result, "No se pudo crear el propietario"));
      state.registrationRequired = false;
      byId("register-form").classList.add("hidden"); byId("login-form").classList.remove("hidden");
      state.setupToken = "";
      const login = await nativeFetch("/api/v1/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      const session = await login.json();
      if (!login.ok) throw new Error("Propietario creado. Inicia sesión.");
      acceptSession(session);
      await Promise.all([refreshSystemStatus(), refreshDocuments()]);
    } catch (error) { byId("login-message").textContent = String(error); }
    finally { byId("register-password").value = ""; byId("register-confirm").value = ""; byId("btn-register").disabled = false; }
  });
  byId("btn-recover-index").addEventListener("click", async () => {
    try {
      const response = await apiFetch("/api/v1/system/recover-index", { method: "POST" });
      if (!response.ok) throw new Error(errorText(await response.json(), "Recuperación rechazada"));
      byId("btn-recover-index").disabled = true;
      const timer = setInterval(async () => {
        try {
          const status = await apiFetch("/api/v1/system/status");
          if (!status.ok) return;
          const data = await status.json();
          await refreshSystemStatus();
          if (data.migration_status !== "running") {
            clearInterval(timer);
            if (data.index_compatible) { finishOpsMonitor(); await refreshDocuments(); }
            else reportOpsProblem("La recuperación no terminó", "Se conservaron los datos; revisa las fuentes y vuelve a intentar.");
          }
        } catch (_) { clearInterval(timer); }
      }, 1500);
      startOpsMonitor("Recuperando índice", "Reconstrucción aislada; no cierres la aplicación.");
    } catch (error) { reportOpsProblem("Recuperación no iniciada", String(error)); }
  });
  byId("btn-test-provider").addEventListener("click", async () => {
    try {
      const response = await apiFetch("/api/v1/system/test-provider", { method: "POST" });
      const data = await response.json();
      if (!response.ok) throw new Error(errorText(data, "No se pudo probar el proveedor"));
      byId("provider-test-message").textContent = data.message;
    } catch (error) { byId("provider-test-message").textContent = String(error); }
  });
  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = byId("btn-login");
    submit.disabled = true;
    const password = byId("login-password");
    byId("live-ops-banner").classList.add("hidden");
    try {
      const response = await nativeFetch("/api/v1/auth/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: byId("login-username").value, password: password.value }) });
      const data = await response.json();
      if (!response.ok) throw new Error(errorText(data, "No se pudo iniciar sesión."));
      acceptSession(data);
      await Promise.all([refreshSystemStatus(), refreshDocuments()]);
    } catch (error) { byId("login-message").textContent = String(error); }
    finally { password.value = ""; submit.disabled = false; }
  });
  byId("btn-close-login").addEventListener("click", () => byId("login-modal").classList.add("hidden"));
  byId("btn-cancel-login").addEventListener("click", () => byId("login-modal").classList.add("hidden"));
  byId("login-modal").addEventListener("click", (event) => {
    if (event.target === byId("login-modal")) byId("login-modal").classList.add("hidden");
  });
  byId("btn-session").addEventListener("click", () => { if (!state.user || state.user === "development") showLogin(); });
  byId("btn-logout").addEventListener("click", async () => {
    try {
      const response = await apiFetch("/api/v1/auth/logout", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      cancelActiveOperation();
      state.history = []; state.responses.clear();
      byId("chat-stream").replaceChildren(); byId("doc-list").replaceChildren();
      clearRightInspector();
      showLogin("Sesión cerrada.");
    } catch (error) { reportOpsProblem("No se pudo cerrar sesión", String(error)); }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (!byId("lightbox-modal").classList.contains("hidden")) byId("lightbox-modal").classList.add("hidden");
    else if (!byId("settings-modal").classList.contains("hidden")) byId("settings-modal").classList.add("hidden");
    else if (!byId("login-modal").classList.contains("hidden")) byId("login-modal").classList.add("hidden");
    else cancelActiveOperation();
  });
});
