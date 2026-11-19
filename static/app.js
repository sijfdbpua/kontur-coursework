"use strict";

const $ = (id) => document.getElementById(id);
const statuses = {new: "Новая", assigned: "Назначена", in_progress: "В работе", done: "На проверке", closed: "Закрыта"};
const priorities = {low: "Низкий", normal: "Обычный", high: "Высокий", critical: "Критический"};
const state = {user: null, csrf: "", meta: {users: [], assets: []}, tickets: [], detail: null, busy: false, loading: false, listKey: "", lastSync: null, sessionVersion: 0, detailRequest: 0, metaLoaded: false};
let toastTimer;

// User-provided text is escaped before entering a template. No server HTML is trusted.
function esc(value) { return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char])); }
function initials(name) { return String(name || "К").trim().split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase(); }
function date(value, short = false) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat("ru-RU", short ? {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"} : {day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit"}).format(parsed);
}
function badge(status) { return `<span class="badge status-${esc(status)}">${esc(statuses[status] || status)}</span>`; }
function priority(value) { return `<span class="priority priority-${esc(value)}"><span class="priority-dot" aria-hidden="true"></span>${esc(priorities[value] || value)}</span>`; }
function message(id, text) { const element = $(id); if (element) { element.textContent = text || ""; element.hidden = !text; } }
function toast(text, alert = false) { clearTimeout(toastTimer); $("toast").textContent = text; $("toast").classList.toggle("alert", alert); $("toast").hidden = false; toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500); }
function syncStatus(ok) {
  $("sync").classList.toggle("offline", !ok);
  $("sync-text").textContent = ok ? "Данные синхронизированы" : "Нет связи · повторяем";
  $("sync").title = ok && state.lastSync ? `Обновлено: ${date(state.lastSync)}` : "Проверь подключение к серверу. Несохранённые поля остаются на устройстве.";
}
async function api(path, options = {}) {
  const sessionVersion = state.sessionVersion;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  const headers = {"Accept": "application/json", ...options.headers};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  try {
    const response = await fetch(path, {...options, body: options.body === undefined ? undefined : JSON.stringify(options.body), headers, credentials: "same-origin", signal: controller.signal, cache: "no-store"});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || `Ошибка запроса (${response.status}). Попробуй ещё раз.`);
      error.status = response.status;
      if (response.status === 401 && state.user && sessionVersion === state.sessionVersion && path !== "/api/login") {
        state.user = null;
        showLogin();
        message("login-error", "Сеанс завершён. Войди снова.");
        const expiredVersion = state.sessionVersion;
        const session = await fetch("/api/session", {credentials: "same-origin", cache: "no-store", signal: controller.signal}).then((r) => r.json()).catch(() => null);
        if (session && expiredVersion === state.sessionVersion && !state.user) state.csrf = session.csrf_token;
      }
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      syncStatus(false);
      throw new Error("Сервер не отвечает. Проверь сеть и попробуй снова. Введённые данные сохранены в форме.");
    }
    throw error;
  } finally { clearTimeout(timeout); }
}

function showLogin() {
  state.sessionVersion++; state.detailRequest++; state.metaLoaded = false; state.loading = false;
  $("boot").hidden = true; $("workspace").hidden = true; $("login-screen").hidden = false;
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  state.detail = null; state.tickets = []; state.listKey = "";
  state.meta = {users: [], assets: []}; state.lastSync = null;
  $("detail-content").replaceChildren(); $("ticket-list").replaceChildren();
  $("create-form").reset(); $("toast").hidden = true;
  message("global-error", ""); $("sync-text").textContent = "Подключаемся…";
}
async function showWorkspace() {
  state.sessionVersion++; state.metaLoaded = false; state.loading = false;
  $("boot").hidden = true; $("login-screen").hidden = true; $("workspace").hidden = false;
  const dispatcher = state.user.role === "dispatcher";
  $("user-name").textContent = state.user.name; $("user-avatar").textContent = initials(state.user.name);
  $("user-role").textContent = dispatcher ? "Диспетчер" : "Техник";
  $("page-eyebrow").textContent = dispatcher ? "ПУЛЬТ ДИСПЕТЧЕРА" : "РАБОЧЕЕ МЕСТО ТЕХНИКА";
  $("page-title").textContent = dispatcher ? "Обзор заявок" : "Мои задачи";
  $("queue-metric").dataset.metric = dispatcher ? "new" : "assigned";
  $("queue-label").textContent = dispatcher ? "Без назначения" : "Назначены";
  $("queue-caption").textContent = dispatcher ? "ожидают ответственного" : "ожидают начала работы";
  $("nav-title").textContent = dispatcher ? "Обзор заявок" : "Мои задачи";
  $("breadcrumb-role").textContent = dispatcher ? "Обслуживание" : "Мои задачи";
  $("page-subtitle").textContent = dispatcher ? "Назначай исполнителей и проверяй результаты работ." : "Твои заявки, оборудование и результаты работы.";
  $("list-hint").textContent = dispatcher ? "От создания до подтверждения результата" : "Здесь только заявки, назначенные тебе";
  $("create").hidden = !dispatcher; $("export").hidden = !dispatcher;
  $("footer-date").textContent = new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long", year: "numeric"}).format(new Date());
  state.listKey = "";
  try { await refresh(true); }
  catch (error) { message("global-error", error.message); }
}
async function boot() {
  $("boot-retry").hidden = true; message("boot-error", "");
  try {
    const session = await api("/api/session"); state.user = session.user; state.csrf = session.csrf_token;
    if (state.user) await showWorkspace(); else showLogin();
  } catch (error) { message("boot-error", error.message); $("boot-retry").hidden = false; }
}

function filteredTickets() {
  const query = $("search").value.trim().toLocaleLowerCase("ru");
  const status = $("status-filter").value;
  const selectedPriority = $("priority-filter").value;
  return state.tickets.filter((ticket) => {
    const matchesStatus = !status || (status === "active" ? !["done", "closed"].includes(ticket.status) : status === ticket.status);
    const text = [ticket.id, ticket.title, ticket.description, ticket.asset_name, ticket.location, ticket.assignee_name].join(" ").toLocaleLowerCase("ru");
    return matchesStatus && (!selectedPriority || selectedPriority === ticket.priority) && (!query || text.includes(query.replace(/^#/, "")));
  });
}
function renderTickets() {
  if (!state.user) return;
  const tickets = filteredTickets();
  const selectedStatus = $("status-filter").value;
  $("metric-active").textContent = state.tickets.filter((ticket) => !["done", "closed"].includes(ticket.status)).length;
  $("metric-new").textContent = state.tickets.filter((ticket) => ticket.status === $("queue-metric").dataset.metric).length;
  $("metric-progress").textContent = state.tickets.filter((ticket) => ticket.status === "in_progress").length;
  $("metric-done").textContent = state.tickets.filter((ticket) => ticket.status === "done").length;
  $("nav-count").textContent = state.tickets.length;
  $("list-count").textContent = tickets.length;
  $("shown-count").textContent = `Показано ${tickets.length} из ${state.tickets.length}`;
  $("clear-filters").hidden = !($("search").value || selectedStatus || $("priority-filter").value);
  document.querySelectorAll("[data-metric]").forEach((button) => button.classList.toggle("selected", button.dataset.metric === selectedStatus));
  const key = JSON.stringify(tickets);
  if (key === state.listKey) return;
  state.listKey = key;
  const focused = document.activeElement?.dataset?.ticket;
  if (!tickets.length) {
    $("ticket-list").innerHTML = `<div class="empty"><strong>${state.tickets.length ? "Ничего не найдено" : "Пока нет заявок"}</strong>${state.tickets.length ? "Попробуй изменить поиск или сбросить фильтры." : state.user.role === "dispatcher" ? "Создай первую заявку на обслуживание оборудования." : "Назначенные диспетчером задачи появятся здесь автоматически."}</div>`;
    return;
  }
  $("ticket-list").innerHTML = tickets.map((ticket) => `<button class="ticket-row" data-ticket="${esc(ticket.id)}" aria-label="Открыть заявку ${esc(ticket.id)}: ${esc(ticket.title)}"><span class="ticket-main"><span class="ticket-number">КН-${String(ticket.id).padStart(4, "0")}</span><span class="ticket-title">${esc(ticket.title)}</span><span class="ticket-asset">${esc(ticket.asset_name)} <span aria-hidden="true">·</span> ${esc(ticket.location)}</span></span>${badge(ticket.status)}${priority(ticket.priority)}<span class="assignee">${ticket.assignee_name ? `<span class="avatar" aria-hidden="true">${esc(initials(ticket.assignee_name))}</span><span class="assignee-name">${esc(ticket.assignee_name)}</span>` : "Не назначен"}</span><span class="row-arrow" aria-hidden="true">›</span></button>`).join("");
  if (focused) Array.from($("ticket-list").querySelectorAll("[data-ticket]")).find((button) => button.dataset.ticket === focused)?.focus({preventScroll: true});
}
async function refresh(manual = false) {
  if (!state.user || state.loading) return;
  const sessionVersion = state.sessionVersion;
  state.loading = true;
  $("refresh").disabled = true;
  try {
    if (!state.metaLoaded) {
      const meta = await api("/api/meta");
      if (sessionVersion !== state.sessionVersion || !state.user) return;
      state.meta = meta; state.metaLoaded = true;
    }
    const result = await api("/api/tickets");
    if (!state.user || sessionVersion !== state.sessionVersion) return;
    state.tickets = result.tickets; renderTickets();
    if ($("detail-dialog").open && state.detail && !state.busy) {
      const detailRequest = state.detailRequest;
      const fresh = await api(`/api/tickets/${state.detail.ticket.id}`);
      if (sessionVersion !== state.sessionVersion) return;
      if (!state.busy && detailRequest === state.detailRequest && state.detail && fresh.ticket.id === state.detail.ticket.id) {
        // Keep form values and focus untouched. Explicit reload prevents silent lost edits.
        if (fresh.ticket.version > state.detail.ticket.version) showStale();
        renderEvents(fresh.events);
      }
    }
    state.lastSync = new Date().toISOString(); syncStatus(true); message("global-error", "");
  } catch (error) {
    if (sessionVersion === state.sessionVersion) {
      syncStatus(false);
      if (manual || error.status === 403 || error.status === 404) message("global-error", error.message);
    }
  } finally { if (sessionVersion === state.sessionVersion) { state.loading = false; $("refresh").disabled = false; } }
}
function resetFilters() { $("search").value = ""; $("status-filter").value = ""; $("priority-filter").value = ""; renderTickets(); }
function technicianOptions(selected, allowEmpty = true) {
  return `${allowEmpty ? `<option value="">Не назначен</option>` : ""}${state.meta.users.filter((user) => user.role === "technician").map((user) => `<option value="${esc(user.id)}"${Number(selected) === user.id ? " selected" : ""}>${esc(user.name)}</option>`).join("")}`;
}
function priorityOptions(selected) { return Object.entries(priorities).map(([value, label]) => `<option value="${value}"${value === selected ? " selected" : ""}>${label}</option>`).join(""); }
function showStale() { if ($("stale-notice")) $("stale-notice").hidden = false; }
function renderEvents(events) {
  if (!$("timeline")) return;
  const key = JSON.stringify(events);
  if ($("timeline").dataset.key === key) return;
  $("timeline").dataset.key = key;
  $("timeline").innerHTML = events.map((event) => `<li><div class="event-heading"><strong>${esc(event.actor_name || "Система")}</strong><time datetime="${esc(event.created_at)}">${esc(date(event.created_at, true))}</time></div><p class="event-body">${esc(event.body)}</p></li>`).join("");
}
function renderDetail(result, commentDraft = "", resolutionDraft = "") {
  state.detail = result;
  const ticket = result.ticket;
  const dispatcher = state.user.role === "dispatcher";
  const canAssign = dispatcher && ["new", "assigned"].includes(ticket.status);
  const assignedToMe = ticket.assignee_id === state.user.id;
  let action = "";
  if (!dispatcher && assignedToMe && ticket.status === "assigned") action = `<button class="button primary" data-status="in_progress">Начать работу →</button>`;
  if (!dispatcher && assignedToMe && ticket.status === "in_progress") action = `<form id="resolution-form"><label for="resolution">Результат работы</label><textarea id="resolution" required maxlength="4000" rows="3" placeholder="Что сделано и как проверена работоспособность?"></textarea><div class="action-buttons"><button type="submit" class="button primary">✓ Завершить работу</button></div><p class="helper">После завершения диспетчер проверит результат и закроет заявку.</p></form>`;
  if (dispatcher && ticket.status === "done") action = `<div class="action-buttons"><button class="button primary" data-status="closed">✓ Подтвердить и закрыть</button><button class="button secondary" data-status="assigned">Вернуть в работу</button></div>`;
  if (dispatcher && ticket.status === "closed") action = `<button class="button secondary" data-status="assigned">Открыть повторно</button>`;
  if (!dispatcher && ["done", "closed"].includes(ticket.status)) action = `<p class="helper">${ticket.status === "done" ? "Результат передан диспетчеру. Заявка ожидает проверки." : "Диспетчер подтвердил выполнение. Заявка закрыта."}</p>`;
  $("detail-content").innerHTML = `<div class="detail-top"><div class="dialog-heading"><div><span class="eyebrow muted">ЗАЯВКА КН-${String(ticket.id).padStart(4, "0")}</span><h2 id="detail-title">${esc(ticket.title)}</h2></div><button class="icon-button" id="close-detail" aria-label="Закрыть заявку">×</button></div><div class="detail-badges">${badge(ticket.status)}${priority(ticket.priority)}</div></div><div class="detail-body"><div id="stale-notice" class="stale-notice" role="status" hidden>Заявка изменена на другом устройстве. Чтобы не перезаписать чужую работу, обнови карточку. Черновик комментария сохранится; результат работы — если заявка ещё выполняется.<br><button id="reload-detail" class="button small secondary">Обновить карточку</button></div><dl class="detail-facts"><div><dt>Оборудование</dt><dd>${esc(ticket.asset_name)}</dd></div><div><dt>Расположение</dt><dd>${esc(ticket.location)}</dd></div><div><dt>Ответственный</dt><dd>${esc(ticket.assignee_name || "Не назначен")}</dd></div><div><dt>Создана</dt><dd>${esc(date(ticket.created_at))}</dd></div></dl><h3 class="section-title">Описание проблемы</h3><p class="description">${esc(ticket.description)}</p>${ticket.resolution ? `<section class="detail-section"><h3 class="section-title">Результат работы</h3><p class="description">${esc(ticket.resolution)}</p></section>` : ""}${dispatcher && ticket.status !== "closed" ? `<section class="detail-section"><h3 class="section-title">Управление заявкой</h3><form id="assignment-form"><div class="form-grid"><div><label for="detail-assignee">Ответственный</label><select id="detail-assignee"${canAssign ? "" : " disabled"}>${technicianOptions(ticket.assignee_id)}</select></div><div><label for="detail-priority">Приоритет</label><select id="detail-priority">${priorityOptions(ticket.priority)}</select></div></div><div class="action-buttons"><button class="button secondary" type="submit">Сохранить изменения</button></div>${canAssign ? "" : `<p class="helper">Ответственного можно менять только до начала выполнения.</p>`}</form></section>` : ""}${action ? `<section class="detail-section"><h3 class="section-title">${dispatcher ? "Проверка результата" : "Следующий шаг"}</h3>${action}</section>` : ""}<p id="detail-error" class="error" role="alert" hidden></p><section class="detail-section"><h3 class="section-title">История заявки</h3><ol id="timeline" class="timeline"></ol></section><section class="detail-section"><form id="comment-form" class="comment-form"><label for="comment">Комментарий команде</label><textarea id="comment" rows="3" required maxlength="2000" placeholder="Уточнение, вопрос или заметка по заявке">${esc(commentDraft)}</textarea><button class="button secondary" type="submit">Отправить комментарий</button></form></section></div>`;
  if ($("resolution")) $("resolution").value = resolutionDraft;
  renderEvents(result.events);
  $("close-detail").addEventListener("click", () => $("detail-dialog").close());
  $("reload-detail").addEventListener("click", () => openDetail(ticket.id, true));
  $("assignment-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {priority: $("detail-priority").value};
    if (canAssign) body.assignee_id = $("detail-assignee").value ? Number($("detail-assignee").value) : null;
    await patchTicket(body, "Изменения сохранены");
  });
  $("detail-content").querySelectorAll("[data-status]").forEach((button) => button.addEventListener("click", () => patchTicket({status: button.dataset.status}, "Статус обновлён")));
  $("resolution-form")?.addEventListener("submit", async (event) => { event.preventDefault(); const resolution = $("resolution").value.trim(); if (!resolution) { message("detail-error", "Опиши результат выполненной работы."); return; } await patchTicket({status: "done", resolution}, "Работа передана на проверку"); });
  $("comment-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (state.busy) return;
    const body = $("comment").value.trim(); if (!body) { message("detail-error", "Введи текст комментария."); return; }
    await detailAction(async () => {
      await api(`/api/tickets/${ticket.id}/comments`, {method: "POST", body: {body}});
      $("comment").value = "";
      const fresh = await api(`/api/tickets/${ticket.id}`);
      if (fresh.ticket.version > state.detail.ticket.version) showStale();
      renderEvents(fresh.events); toast("Комментарий добавлен");
    });
  });
}
async function openDetail(id, preserveDraft = false) {
  if (state.busy) return;
  const requestId = ++state.detailRequest;
  const sessionVersion = state.sessionVersion;
  if (!$("detail-dialog").open) {
    state.detail = null; $("detail-content").innerHTML = `<div class="loading-label">Загружаем заявку…</div>`; $("detail-dialog").showModal();
  }
  try {
    const result = await api(`/api/tickets/${id}`);
    if (!$("detail-dialog").open || requestId !== state.detailRequest || sessionVersion !== state.sessionVersion || !state.user) return;
    const commentDraft = preserveDraft ? $("comment")?.value || "" : "";
    const resolutionDraft = preserveDraft ? $("resolution")?.value || "" : "";
    renderDetail(result, commentDraft, resolutionDraft);
    if (!preserveDraft) $("close-detail").focus({preventScroll: true});
  } catch (error) {
    if (requestId !== state.detailRequest || sessionVersion !== state.sessionVersion) return;
    if (state.detail) message("detail-error", error.message);
    else { $("detail-dialog").close(); toast(error.message, true); }
  }
}
async function detailAction(action) {
  if (state.busy) return;
  state.detailRequest++; // Invalidate reads begun before this mutation.
  state.busy = true; message("detail-error", "");
  $("detail-content").querySelectorAll("button").forEach((button) => { button.disabled = true; });
  try { await action(); }
  catch (error) { message("detail-error", error.message); if (error.status === 409) showStale(); }
  finally { state.busy = false; $("detail-content").querySelectorAll("button").forEach((button) => { button.disabled = false; }); }
}
async function patchTicket(body, successText) {
  if (!state.detail) return;
  const ticket = state.detail.ticket;
  await detailAction(async () => {
    await api(`/api/tickets/${ticket.id}`, {method: "PATCH", body: {version: ticket.version, ...body}});
    const fresh = await api(`/api/tickets/${ticket.id}`);
    renderDetail(fresh, $("comment")?.value || ""); toast(successText);
    const listing = await api("/api/tickets"); state.tickets = listing.tickets; renderTickets();
  });
}

$("boot-retry").addEventListener("click", boot);
$("password-toggle").addEventListener("click", () => {
  const visible = $("password").type === "password"; $("password").type = visible ? "text" : "password";
  $("password-toggle").textContent = visible ? "Скрыть" : "Показать";
  $("password-toggle").setAttribute("aria-label", visible ? "Скрыть пароль" : "Показать пароль");
  $("password-toggle").setAttribute("aria-pressed", String(visible));
});
document.querySelectorAll("[data-demo]").forEach((button) => button.addEventListener("click", () => {
  const role = button.dataset.demo; $("username").value = role;
  $("password").value = {dispatcher: "demo-dispatch-2026", tech: "demo-tech-2026", tech2: "demo-tech2-2026"}[role];
  message("login-error", ""); $("login-form").querySelector("[type=submit]").focus();
}));
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; message("login-error", "");
  try {
    state.csrf = (await api("/api/session")).csrf_token;
    const result = await api("/api/login", {method: "POST", body: {username: $("username").value.trim(), password: $("password").value}});
    state.user = result.user; state.csrf = result.csrf_token; $("password").value = ""; resetFilters(); await showWorkspace();
  } catch (error) { message("login-error", error.message); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  $("logout").disabled = true;
  try { const result = await api("/api/logout", {method: "POST", body: {}}); state.user = null; state.csrf = result.csrf_token || ""; showLogin(); if (!state.csrf) state.csrf = (await api("/api/session")).csrf_token; }
  catch (error) { toast(error.message, true); }
  finally { $("logout").disabled = false; }
});
$("search").addEventListener("input", renderTickets);
$("status-filter").addEventListener("change", renderTickets);
$("priority-filter").addEventListener("change", renderTickets);
$("clear-filters").addEventListener("click", resetFilters);
$("nav-all").addEventListener("click", resetFilters);
$("refresh").addEventListener("click", () => refresh(true));
document.querySelectorAll("[data-metric]").forEach((button) => button.addEventListener("click", () => { $("status-filter").value = $("status-filter").value === button.dataset.metric ? "" : button.dataset.metric; renderTickets(); }));
$("ticket-list").addEventListener("click", (event) => { const button = event.target.closest("[data-ticket]"); if (button) openDetail(Number(button.dataset.ticket)); });
$("create").addEventListener("click", () => {
  if (!state.metaLoaded) { toast("Справочники ещё не загружены. Нажми «Обновить» и попробуй снова.", true); return; }
  $("ticket-asset").innerHTML = `<option value="" disabled selected>Выбери оборудование</option>${state.meta.assets.map((asset) => `<option value="${esc(asset.id)}">${esc(asset.name)} · ${esc(asset.location)}</option>`).join("")}`;
  $("ticket-assignee").innerHTML = technicianOptions(null);
  $("create-form").reset(); message("create-error", ""); $("create-dialog").showModal();
});
$("create-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; message("create-error", "");
  try {
    const body = {title: $("ticket-title").value.trim(), description: $("ticket-description").value.trim(), asset_id: Number($("ticket-asset").value), priority: $("ticket-priority").value, assignee_id: $("ticket-assignee").value ? Number($("ticket-assignee").value) : null};
    const result = await api("/api/tickets", {method: "POST", body});
    $("create-dialog").close(); resetFilters(); await refresh(true); toast("Заявка создана"); await openDetail(result.ticket.id);
  } catch (error) { message("create-error", error.message); }
  finally { button.disabled = false; }
});
document.querySelectorAll(".close-dialog").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("detail-dialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
$("detail-dialog").addEventListener("close", () => { state.detail = null; state.detailRequest++; });
$("create-dialog").addEventListener("cancel", (event) => { if ($("create-form").querySelector("[type=submit]").disabled) event.preventDefault(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
window.addEventListener("online", () => refresh(true));
window.addEventListener("offline", () => syncStatus(false));
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
boot();
