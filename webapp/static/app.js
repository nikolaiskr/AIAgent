/* ============================================================
   AI Курсовая — Mini App клиент.
   Нео-брутализм, vanilla JS, хеш-роутинг, поллинг job'ов.
   ============================================================ */
"use strict";

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#fff9ee");
  tg.setBackgroundColor("#fff9ee");
}

const DEV = new URLSearchParams(location.search).has("dev");

// Базовый адрес API. По умолчанию — тот же домен (сервер бота).
// При размещении фронта на GitHub Pages задаётся в /static/config.js (window.API_BASE).
const API_BASE = String(window.API_BASE || "").replace(/\/+$/, "");

/* ---------------- API ---------------- */

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (tg && tg.initData) h["X-Telegram-Init-Data"] = tg.initData;
  if (DEV) h["X-Dev-User"] = "1";
  return h;
}

async function api(path, opts) {
  opts = opts || {};
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers: authHeaders(opts.headers) }));
  let data = null;
  try { data = await res.json(); } catch (e) { /* не JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error((data && data.error) || ("Ошибка " + res.status));
    err.code = data && data.code;
    err.status = res.status;
    throw err;
  }
  return data;
}

const postJSON = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});
const putJSON = (path, body) => api(path, {
  method: "PUT",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body || {}),
});

/* ---------------- утилиты DOM ---------------- */

function h(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = text;
  return el;
}

function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }

function esc(s) { return String(s == null ? "" : s); }

/* ---------------- тосты ---------------- */

function toast(message, type, ms) {
  const box = document.getElementById("toasts");
  const el = h("div", "toast " + (type || "info"));
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 220);
  }, ms || 3600);
}

/* ---------------- глобальное состояние ---------------- */

const state = {
  me: null,
  project: null,
  tab: "sections",
  activeJobs: new Map(), // job_id -> {card, lastStatus}
};

const TYPE_ICONS = { coursework: "📚", thesis: "🎓", lab: "🧪", practical: "📝", task: "🧮", other: "❓" };
const TYPE_NAMES = { coursework: "Курсовая", thesis: "ВКР", lab: "Лабораторная", practical: "Практическая", task: "Задание", other: "Другое" };
const ROLE_NAMES = {
  methodology: "📘 Методичка", assignment: "🎯 Задание", source_data: "📊 Данные",
  additional: "📎 Доп.", example: "📄 Пример", literature: "📚 Литература",
  feedback: "💬 Замечания", current_version: "📝 Тек. версия", image: "🖼 Картинка",
  author_style_sample: "✍️ Стиль", other: "❓ Другое",
};

/* ---------------- job-док и поллинг ---------------- */

function trackJob(jobId, label) {
  if (state.activeJobs.has(jobId)) return;
  const dock = document.getElementById("jobdock");
  const card = h("div", "job-card");
  const head = h("div", "job-head");
  const title = h("span", "", "⏳ " + esc(label || jobId));
  const cancelBtn = h("button", "btn sm red", "✕");
  cancelBtn.onclick = async () => {
    try { await api("/api/jobs/" + jobId + "/cancel", { method: "POST" }); toast("Запрошена отмена задачи", "warn"); }
    catch (e) { toast(e.message, "err"); }
  };
  head.appendChild(title);
  head.appendChild(cancelBtn);
  const body = h("div", "job-body", "В очереди…");
  card.appendChild(head);
  card.appendChild(body);
  dock.appendChild(card);
  state.activeJobs.set(jobId, { card, title, body, label: label || jobId });
  ensurePoller();
}

function ensurePoller() {
  if (ensurePoller.timer) return;
  ensurePoller.timer = setInterval(pollJobs, 2000);
}

async function pollJobs() {
  if (state.activeJobs.size === 0) {
    clearInterval(ensurePoller.timer);
    ensurePoller.timer = null;
    return;
  }
  for (const [jobId, job] of Array.from(state.activeJobs.entries())) {
    try {
      const data = await api("/api/jobs/" + jobId);
      const j = data.job;
      if (j.status === "running" || j.status === "queued") {
        job.body.textContent = j.progress || "…";
        job.card.className = "job-card";
      } else if (j.status === "completed") {
        finishJob(jobId, job, "done", "✅ " + job.label + " — готово", j);
      } else if (j.status === "failed") {
        finishJob(jobId, job, "failed", "❌ " + job.label + ": " + (j.error || "ошибка"), j);
      } else if (j.status === "cancelled") {
        finishJob(jobId, job, "", "⛔ " + job.label + " — отменено", j);
      }
    } catch (e) {
      /* сервер мог перезагрузиться — оставляем карточку, попробуем ещё */
    }
  }
}

function finishJob(jobId, job, cls, text, j) {
  state.activeJobs.delete(jobId);
  job.card.className = "job-card " + cls;
  const head = job.card.querySelector(".job-head");
  if (head) {
    clear(head);
    head.appendChild(h("span", "", text));
    const closeBtn = h("button", "btn sm dark", "✕");
    closeBtn.onclick = () => job.card.remove();
    head.appendChild(closeBtn);
  }
  const body = job.card.querySelector(".job-body");
  if (body) {
    clear(body);
    body.appendChild(h("div", "", text));
    if (j && j.result && j.result.docx_path) {
      const dl = h("button", "btn sm green", "⬇ Скачать DOCX");
      dl.style.marginTop = "8px";
      dl.onclick = () => downloadFile(currentProjectName(), j.result.docx_path);
      body.appendChild(dl);
    }
  }
  setTimeout(() => { job.card.remove(); }, cls === "" ? 4000 : 12000);
  if (cls === "failed") toast(text, "err", 6000);
  else if (cls === "done") toast(text, "ok");
  refreshCurrentView();
}

async function downloadFile(project, path) {
  try {
    const res = await fetch(API_BASE + "/api/download?project=" + encodeURIComponent(project) + "&path=" + encodeURIComponent(path), { headers: authHeaders() });
    if (!res.ok) throw new Error("Скачивание не удалось (" + res.status + ")");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = path.split("/").pop();
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ---------------- роутинг ---------------- */

function currentProjectName() {
  const m = location.hash.match(/^#\/p\/(.+?)(?:\/|$)/);
  return m ? decodeURIComponent(m[1]) : null;
}

function route() {
  const hash = location.hash || "#/";
  if (hash.startsWith("#/admin")) return renderAdmin();
  const name = currentProjectName();
  if (name) return renderProject(name);
  return renderHome();
}

window.addEventListener("hashchange", route);

async function refreshCurrentView() {
  const name = currentProjectName();
  if (name && state.project && state.project.name === name) return renderProject(name, true);
  if (!name) return renderHome(true);
}

/* ---------------- HOME ---------------- */

async function renderHome(soft) {
  const app = document.getElementById("app");
  if (!soft) { clear(app); app.appendChild(h("div", "splash")); }
  let data;
  try {
    const meData = await api("/api/me");
    state.me = meData;
    data = await api("/api/projects");
  } catch (e) {
    clear(app);
    const panel = h("div", "panel pink");
    panel.appendChild(h("div", "panel-title", "😵 Не получилось войти"));
    panel.appendChild(h("div", "", e.message));
    const btn = h("button", "btn yellow block", "Попробовать снова");
    btn.onclick = () => renderHome();
    panel.appendChild(h("div", "", ""));
    panel.appendChild(btn);
    app.appendChild(panel);
    return;
  }

  clear(app);
  app.appendChild(topbar());

  const hero = h("div", "");
  hero.appendChild(h("div", "hero-title", "Работы пишет бот.\nТы сдаёшь."));
  const quota = state.me.quota || {};
  const sub = quota.paid
    ? "💎 Безлимитный доступ активен"
    : "🆓 Бесплатных работ осталось: " + (quota.remaining == null ? "—" : quota.remaining);
  hero.appendChild(h("div", "hero-sub", sub));
  app.appendChild(hero);

  const projects = data.projects || [];
  const done = projects.reduce((a, p) => a + (p.sections_done || 0), 0);
  const total = projects.reduce((a, p) => a + (p.sections_total || 0), 0);
  const stats = h("div", "stat-row");
  stats.appendChild(statCard(projects.length, "работ"));
  stats.appendChild(statCard(done + "/" + total, "разделов"));
  stats.appendChild(statCard(projects.filter(p => p.pct === 100).length, "готово"));
  app.appendChild(stats);

  app.appendChild(h("div", "section-title", "Мои работы"));
  const grid = h("div", "cards-grid");
  const colors = ["yellow", "pink", "green", "purple", "blue", "red"];
  projects.forEach((p, i) => {
    const card = h("div", "folder-card " + colors[i % colors.length]);
    const top = h("div", "");
    const icon = h("div", "folder-icon", TYPE_ICONS[p.type] || "❓");
    top.appendChild(icon);
    top.appendChild(h("div", "folder-meta",
      (p.materials || 0) + " материалов\n" + (p.sections_done || 0) + "/" + (p.sections_total || 0) + " разделов"));
    card.appendChild(top);
    card.appendChild(h("div", "folder-name", p.name));
    if (p.pct === 100 && p.sections_total > 0) card.appendChild(h("span", "chip approved", "готово"));
    card.onclick = () => { location.hash = "#/p/" + encodeURIComponent(p.name); };
    grid.appendChild(card);
  });
  const newCard = h("div", "folder-card new-card");
  newCard.appendChild(h("div", "folder-icon", "＋"));
  newCard.appendChild(h("div", "folder-name", "Новая работа"));
  newCard.onclick = openNewProjectModal;
  grid.appendChild(newCard);
  app.appendChild(grid);

  if (!quota.paid && quota.remaining === 0) {
    const banner = h("div", "panel pink");
    banner.appendChild(h("div", "panel-title", "💳 Лимит бесплатных работ исчерпан"));
    banner.appendChild(h("div", "", "Создано " + quota.used + " из " + quota.limit + ". Оплати доступ — и пиши сколько угодно работ."));
    const btn = h("button", "btn yellow block", "Открыть доступ");
    btn.onclick = openPaywall;
    banner.appendChild(btn);
    app.appendChild(banner);
  }
}

function statCard(num, label) {
  const card = h("div", "stat-card");
  card.appendChild(h("div", "stat-num", String(num)));
  card.appendChild(h("div", "stat-label", label));
  return card;
}

function topbar() {
  const bar = h("div", "topbar");
  const logo = h("div", "logo-chip", "🎓 AI·КУРСОВАЯ");
  bar.appendChild(logo);
  const group = h("div", "topbar-group");
  const refresh = h("button", "icon-btn yellow", "↻");
  refresh.onclick = () => route();
  group.appendChild(refresh);
  if (state.me && state.me.admin) {
    const admin = h("button", "icon-btn pink", "⚙️");
    admin.onclick = () => { location.hash = "#/admin"; };
    group.appendChild(admin);
  }
  bar.appendChild(group);
  return bar;
}

/* ---------------- модалка: новая работа ---------------- */

function openNewProjectModal() {
  const root = document.getElementById("modalroot");
  clear(root);
  const backdrop = h("div", "modal-backdrop");
  const modal = h("div", "modal yellow");
  modal.appendChild(modalHead("🆕 Новая работа", () => clear(root)));
  const body = h("div", "modal-body");
  body.appendChild(h("label", "field-label", "Название (фамилия / тема)"));
  const input = h("input", "input");
  input.placeholder = "например: Бего — экономика";
  body.appendChild(input);
  body.appendChild(h("label", "field-label", "Тип работы"));
  const select = h("select", "select");
  Object.keys(TYPE_NAMES).forEach(code => {
    const opt = h("option", "", TYPE_NAMES[code]);
    opt.value = code;
    select.appendChild(opt);
  });
  select.value = "coursework";
  body.appendChild(select);
  modal.appendChild(body);
  const actions = h("div", "modal-actions");
  const createBtn = h("button", "btn dark block", "Создать ✨");
  createBtn.onclick = async () => {
    const name = input.value.trim();
    if (!name) { toast("Введи название работы", "warn"); return; }
    createBtn.disabled = true;
    try {
      const res = await postJSON("/api/projects", { name, type: select.value });
      clear(root);
      toast("Работа «" + res.name + "» создана", "ok");
      location.hash = "#/p/" + encodeURIComponent(res.name);
    } catch (e) {
      createBtn.disabled = false;
      if (e.code === "PAYWALL") { clear(root); openPaywall(); return; }
      toast(e.message, "err", 6000);
    }
  };
  actions.appendChild(createBtn);
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  backdrop.onclick = (ev) => { if (ev.target === backdrop) clear(root); };
  root.appendChild(backdrop);
}

/* ---------------- модалка: оплата ---------------- */

function openPaywall() {
  const root = document.getElementById("modalroot");
  clear(root);
  const quota = (state.me && state.me.quota) || {};
  const backdrop = h("div", "modal-backdrop");
  const modal = h("div", "modal pink");
  modal.appendChild(modalHead("💳 Оплата доступа", () => clear(root)));
  const body = h("div", "modal-body");
  body.appendChild(h("div", "paywall-big", "🎓💸"));
  body.appendChild(h("div", "panel-title", "Бесплатный лимит исчерпан"));
  body.appendChild(h("div", "", "Создано работ: " + (quota.used != null ? quota.used : "—") + " из " + (quota.limit != null ? quota.limit : "—") + ". Оплати доступ — и создавай сколько угодно работ без ограничений."));
  body.appendChild(h("div", "", "Напиши владельцу бота, он подтвердит оплату — доступ откроется автоматически."));
  modal.appendChild(body);
  const actions = h("div", "modal-actions");
  const ownerBtn = h("a", "btn dark block", "✉️ Написать владельцу");
  ownerBtn.href = (state.me && state.me.owner_link) || "https://t.me/";
  ownerBtn.target = "_blank";
  const askBtn = h("button", "btn yellow block", "✋ Я написал");
  askBtn.onclick = async () => {
    askBtn.disabled = true;
    try {
      await postJSON("/api/payment/request", {});
      toast("Владелец получил уведомление. Как подтвердит оплату — придёт сообщение.", "ok", 6000);
    } catch (e) { toast(e.message, "err"); }
    askBtn.disabled = false;
  };
  actions.appendChild(ownerBtn);
  actions.appendChild(askBtn);
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  backdrop.onclick = (ev) => { if (ev.target === backdrop) clear(root); };
  root.appendChild(backdrop);
}

function modalHead(title, onClose) {
  const head = h("div", "modal-head");
  head.appendChild(h("span", "", title));
  const close = h("button", "icon-btn", "✕");
  close.style.width = "36px";
  close.style.height = "36px";
  close.onclick = onClose;
  head.appendChild(close);
  return head;
}

/* ---------------- ПРОЕКТ ---------------- */

async function renderProject(name, soft) {
  const app = document.getElementById("app");
  let data;
  try {
    data = await api("/api/projects/" + encodeURIComponent(name));
  } catch (e) {
    clear(app);
    app.appendChild(topbar());
    const panel = h("div", "panel pink");
    panel.appendChild(h("div", "panel-title", "😵 Не удалось открыть работу"));
    panel.appendChild(h("div", "", e.message));
    app.appendChild(panel);
    return;
  }
  state.project = data.project;
  clear(app);
  app.appendChild(topbar());

  const backRow = h("div", "back-row");
  const back = h("button", "btn sm", "← Все работы");
  back.onclick = () => { location.hash = "#/"; };
  backRow.appendChild(back);
  backRow.appendChild(h("span", "chip " + (data.project.pct === 100 ? "approved" : "planned"), (data.project.pct || 0) + "%"));
  app.appendChild(backRow);

  app.appendChild(h("div", "hero-title", data.project.name));
  const sub = h("div", "hero-sub",
    (TYPE_NAMES[data.project.type] || data.project.type) + " · материалов: " + data.project.materials +
    (data.project.topic ? " · тема: " + data.project.topic : ""));
  app.appendChild(sub);

  const bar = h("div", "progressbar");
  const fill = h("div", "", (data.project.pct || 0) + "%");
  fill.style.width = Math.max(0, Math.min(100, data.project.pct || 0)) + "%";
  bar.appendChild(fill);
  const panel = h("div", "panel");
  panel.appendChild(h("div", "panel-title", "Готовность: " + data.project.sections_done + "/" + data.project.sections_total + " разделов"));
  panel.appendChild(bar);
  app.appendChild(panel);

  const tabs = h("div", "tabs");
  const tabsDef = [
    ["sections", "🧩 Разделы"],
    ["materials", "📎 Материалы"],
    ["plan", "🗺 План"],
    ["assemble", "📕 Сборка"],
  ];
  tabsDef.forEach(([code, label]) => {
    const t = h("button", "tab" + (state.tab === code ? " active" : ""), label);
    t.onclick = () => { state.tab = code; renderProject(name, true); };
    tabs.appendChild(t);
  });
  app.appendChild(tabs);

  const content = h("div", "");
  app.appendChild(content);
  if (state.tab === "sections") renderSectionsTab(content, data.project);
  else if (state.tab === "materials") renderMaterialsTab(content, data.project);
  else if (state.tab === "plan") renderPlanTab(content, data.project);
  else renderAssembleTab(content, data.project);
}

/* ---- вкладка «Разделы» ---- */

function renderSectionsTab(root, p) {
  if (p.chapters && p.chapters.length) {
    root.appendChild(h("div", "section-title", "Главы"));
    const rows = h("div", "rows");
    p.chapters.forEach(ch => {
      const row = h("div", "row-item");
      const main = h("div", "row-main");
      main.appendChild(h("div", "row-title", ch.id + " " + ch.title));
      main.appendChild(h("div", "row-sub", "подразделов: " + ch.children));
      row.appendChild(main);
      const actions = h("div", "row-actions");
      const btn = h("button", "btn sm purple", "📑 Собрать");
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const res = await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/chapter", { chapter_id: ch.id });
          trackJob(res.job_id, "Сборка главы " + ch.id);
        } catch (e) { toast(e.message, "err"); btn.disabled = false; }
      };
      actions.appendChild(btn);
      row.appendChild(actions);
      rows.appendChild(row);
    });
    root.appendChild(rows);
  }

  root.appendChild(h("div", "section-title", "Разделы"));
  if (!p.sections || !p.sections.length) {
    const empty = h("div", "empty-state", "Разделов пока нет. Сначала создай план во вкладке «🗺 План».");
    root.appendChild(empty);
    return;
  }
  const grid = h("div", "cards-grid");
  p.sections.forEach(s => {
    const card = h("div", "folder-card " + statusColor(s.status));
    const top = h("div", "");
    const iconRow = h("div", "");
    iconRow.style.display = "flex";
    iconRow.style.justifyContent = "space-between";
    iconRow.style.alignItems = "center";
    iconRow.appendChild(h("div", "folder-icon", "📄"));
    iconRow.appendChild(h("span", "chip " + esc(s.status), statusLabel(s.status)));
    top.appendChild(iconRow);
    const meta = (s.target_pages ? "~" + s.target_pages + " стр. · " : "") +
      (s.words ? s.words + " слов" : "не создан") +
      (s.version ? " · v" + s.version : "");
    top.appendChild(h("div", "folder-meta", meta));
    card.appendChild(top);
    card.appendChild(h("div", "folder-name", s.id + " " + s.title));

    const actions = h("div", "row-actions");
    actions.style.marginTop = "10px";
    if (s.has_text) {
      const dl = h("button", "btn sm green", "⬇");
      dl.title = "Скачать DOCX";
      dl.onclick = () => downloadFile(p.name, "sections/" + slugFor(s) + "/v" + s.version + ".docx");
      actions.appendChild(dl);
    }
    const gen = h("button", "btn sm yellow", s.has_text ? "🔄" : "▶");
    gen.title = s.has_text ? "Сгенерировать заново" : "Сгенерировать раздел";
    gen.onclick = () => openInstructionModal(p.name, s, "generate");
    actions.appendChild(gen);
    if (s.has_text) {
      const cont = h("button", "btn sm blue", "➕");
      cont.title = "Продолжить / доработать";
      cont.onclick = () => openInstructionModal(p.name, s, "continue");
      actions.appendChild(cont);
      const rev = h("button", "btn sm pink", "💬");
      rev.title = "Правка по замечанию преподавателя";
      rev.onclick = () => openInstructionModal(p.name, s, "revise");
      actions.appendChild(rev);
    }
    card.appendChild(actions);
    grid.appendChild(card);
  });
  root.appendChild(grid);
}

function slugFor(s) {
  // Должно совпадать с section_slug на бэкенде: кликабельный slug вычисляется сервером,
  // поэтому для скачивания используем путь из версии через API проекта — упрощённо по id.
  const sid = String(s.id).trim().toLowerCase().replace(/\s+/g, "");
  if (sid === "intro" || sid === "introduction" || sid === "введение") return "00_introduction";
  if (sid === "conclusion" || sid === "заключение") return "conclusion";
  if (sid === "references" || sid === "literature" || sid === "списоклитературы") return "references";
  const cleaned = sid.replace(/[^0-9a-zа-яё]+/gi, "_").replace(/^_+|_+$/g, "").replace(/\./g, "_") || "section";
  return cleaned.slice(0, 70);
}

function statusColor(status) {
  return {
    approved: "green", completed: "green", draft: "blue", writing: "yellow",
    review: "yellow", needs_revision: "red", failed: "red",
  }[status] || "yellow";
}

function statusLabel(status) {
  return {
    approved: "✅ approved", completed: "✅ готово", draft: "🟡 draft",
    writing: "🟣 пишу…", review: "🔎 проверка", needs_revision: "🟠 правка",
    planned: "🔵 план", pending: "⚪ ждёт", failed: "❌ ошибка",
  }[status] || status;
}

function openInstructionModal(projectName, section, action) {
  const titles = { generate: "▶ Генерация раздела", continue: "➕ Продолжить раздел", revise: "💬 Замечание преподавателя" };
  const root = document.getElementById("modalroot");
  clear(root);
  const backdrop = h("div", "modal-backdrop");
  const modal = h("div", "modal " + (action === "revise" ? "pink" : "yellow"));
  modal.appendChild(modalHead(titles[action] + " " + section.id, () => clear(root)));
  const body = h("div", "modal-body");
  body.appendChild(h("div", "", section.id + " " + section.title));
  body.appendChild(h("label", "field-label",
    action === "generate" ? "Доп. инструкция (или оставь пустым — строго по плану)" :
    action === "continue" ? "Что добавить / изменить? Например: «добавь страницу про теорию мотивации»" :
    "Текст замечания преподавателя"));
  const ta = h("textarea", "textarea");
  ta.placeholder = action === "revise" ? "«Нужно больше цифр и ссылок на отечественных авторов…»" : "—";
  body.appendChild(ta);
  modal.appendChild(body);
  const actions = h("div", "modal-actions");
  const go = h("button", "btn dark block", action === "generate" ? "Поехали 🚀" : "Создать новую версию");
  go.onclick = async () => {
    go.disabled = true;
    try {
      const res = await postJSON(
        "/api/projects/" + encodeURIComponent(projectName) + "/sections/" + encodeURIComponent(section.id) + "/" + action,
        { instruction: ta.value.trim() }
      );
      clear(root);
      const labels = { generate: "Генерация " + section.id, continue: "Продолжение " + section.id, revise: "Правка " + section.id };
      trackJob(res.job_id, labels[action]);
      toast("Задача поставлена в очередь", "info");
    } catch (e) {
      go.disabled = false;
      toast(e.message, "err", 6000);
    }
  };
  actions.appendChild(go);
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  backdrop.onclick = (ev) => { if (ev.target === backdrop) clear(root); };
  root.appendChild(backdrop);
}

/* ---- вкладка «Материалы» ---- */

function renderMaterialsTab(root, p) {
  const dz = h("div", "dropzone", "📥 Перетащи файлы сюда или нажми\nPDF · DOCX · XLSX · TXT · MD · JPG · PNG");
  dz.style.whiteSpace = "pre-line";
  const input = h("input", "hidden");
  input.type = "file";
  input.multiple = true;
  input.accept = ".pdf,.docx,.xlsx,.txt,.md,.jpg,.jpeg,.png,.webp";
  input.onchange = () => uploadFiles(p.name, input.files);
  dz.onclick = () => input.click();
  ["dragover", "dragenter"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", e => uploadFiles(p.name, e.dataTransfer.files));
  root.appendChild(dz);
  root.appendChild(input);

  root.appendChild(h("div", "section-title", "Загружено: " + (p.materials ? p.materials.length : 0)));
  if (!p.materials || !p.materials.length) {
    root.appendChild(h("div", "empty-state", "Материалов пока нет. Загрузи методичку, задание и литературу."));
    return;
  }
  const rows = h("div", "rows");
  p.materials.forEach(m => {
    const row = h("div", "row-item");
    const main = h("div", "row-main");
    main.appendChild(h("div", "row-title", iconForExt(m.extension) + " " + m.name));
    main.appendChild(h("div", "row-sub", Math.round((m.size || 0) / 1024) + " КБ · " + (ROLE_NAMES[m.role] || m.role) + (m.role_manual ? " (вручную)" : "")));
    row.appendChild(main);
    const actions = h("div", "row-actions");
    const select = h("select", "select");
    Object.keys(ROLE_NAMES).forEach(code => {
      const opt = h("option", "", ROLE_NAMES[code]);
      opt.value = code;
      select.appendChild(opt);
    });
    select.value = m.role || "other";
    select.onchange = async () => {
      try {
        await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/materials/role", { path: m.path, role: select.value });
        toast("Роль обновлена", "ok");
        renderProject(p.name, true);
      } catch (e) { toast(e.message, "err"); }
    };
    actions.appendChild(select);
    const del = h("button", "btn sm red", "🗑");
    del.onclick = async () => {
      if (!confirm("Удалить «" + m.name + "»?")) return;
      try {
        await api("/api/projects/" + encodeURIComponent(p.name) + "/materials?path=" + encodeURIComponent(m.path), { method: "DELETE" });
        toast("Удалено", "ok");
        renderProject(p.name, true);
      } catch (e) { toast(e.message, "err"); }
    };
    actions.appendChild(del);
    row.appendChild(actions);
    rows.appendChild(row);
  });
  root.appendChild(rows);
}

function iconForExt(ext) {
  return { ".pdf": "📕", ".docx": "📝", ".xlsx": "📊", ".txt": "📃", ".md": "📃" }[ext] || "📎";
}

async function uploadFiles(projectName, files) {
  if (!files || !files.length) return;
  for (const file of files) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    try {
      await api("/api/projects/" + encodeURIComponent(projectName) + "/upload", { method: "POST", body: fd });
      toast("Загружено: " + file.name, "ok");
    } catch (e) {
      toast(file.name + ": " + e.message, "err", 6000);
    }
  }
  renderProject(projectName, true);
}

/* ---- вкладка «План» ---- */

function renderPlanTab(root, p) {
  if (!p.plan) {
    const panel = h("div", "panel yellow");
    panel.appendChild(h("div", "panel-title", "🗺 Плана пока нет"));
    panel.appendChild(h("div", "", "План строится из темы, задания и методички. Загрузи материалы и нажми кнопку."));
    const btn = h("button", "btn dark block", "✨ Создать план");
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const res = await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/plan", { force: false });
        trackJob(res.job_id, "Создание плана");
      } catch (e) { toast(e.message, "err"); btn.disabled = false; }
    };
    panel.appendChild(btn);
    root.appendChild(panel);
    return;
  }
  const panel = h("div", "panel");
  panel.appendChild(h("div", "panel-title", p.plan.title || p.name));
  const tree = h("div", "plan-tree", p.plan_text || "");
  panel.appendChild(tree);
  root.appendChild(panel);

  const actions = h("div", "row-actions");
  const rebuild = h("button", "btn yellow", "🔄 Перестроить");
  rebuild.onclick = async () => {
    if (!confirm("Текущий план будет заменён. Версии разделов не удалятся. Продолжить?")) return;
    try {
      const res = await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/plan", { force: true });
      trackJob(res.job_id, "Перестроение плана");
    } catch (e) { toast(e.message, "err"); }
  };
  const edit = h("button", "btn blue", "✏️ Редактировать JSON");
  edit.onclick = () => openPlanEditor(p);
  actions.appendChild(rebuild);
  actions.appendChild(edit);
  root.appendChild(actions);
}

function openPlanEditor(p) {
  const root = document.getElementById("modalroot");
  clear(root);
  const backdrop = h("div", "modal-backdrop");
  const modal = h("div", "modal purple");
  modal.appendChild(modalHead("✏️ work_plan.json", () => clear(root)));
  const body = h("div", "modal-body");
  const ta = h("textarea", "textarea");
  ta.style.minHeight = "300px";
  ta.value = JSON.stringify(p.plan, null, 2);
  body.appendChild(ta);
  modal.appendChild(body);
  const actions = h("div", "modal-actions");
  const save = h("button", "btn dark block", "💾 Сохранить");
  save.onclick = async () => {
    try {
      const plan = JSON.parse(ta.value);
      await putJSON("/api/projects/" + encodeURIComponent(p.name) + "/plan", plan);
      clear(root);
      toast("План обновлён", "ok");
      renderProject(p.name, true);
    } catch (e) {
      toast(e instanceof SyntaxError ? "JSON невалиден: " + e.message : e.message, "err", 6000);
    }
  };
  actions.appendChild(save);
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  root.appendChild(backdrop);
}

/* ---- вкладка «Сборка» ---- */

function renderAssembleTab(root, p) {
  const panel = h("div", "panel green");
  panel.appendChild(h("div", "panel-title", "📕 Финальная сборка"));
  panel.appendChild(h("div", "", "Соберёт последние версии всех разделов в один DOCX и проведёт финальную проверку согласованности. Разделы со статусом «правка» остановят сборку — можно собрать принудительно."));
  const actions = h("div", "row-actions");
  actions.style.marginTop = "12px";
  const assemble = h("button", "btn dark", "📕 Собрать работу");
  assemble.onclick = async () => {
    assemble.disabled = true;
    try {
      const res = await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/assemble", { force: false });
      trackJob(res.job_id, "Сборка работы");
    } catch (e) { toast(e.message, "err", 7000); assemble.disabled = false; }
  };
  const force = h("button", "btn red", "⚠️ Собрать принудительно");
  force.onclick = async () => {
    if (!confirm("Включу разделы без статуса approved. Уверен?")) return;
    force.disabled = true;
    try {
      const res = await postJSON("/api/projects/" + encodeURIComponent(p.name) + "/assemble", { force: true });
      trackJob(res.job_id, "Сборка (force)");
    } catch (e) { toast(e.message, "err", 7000); force.disabled = false; }
  };
  actions.appendChild(assemble);
  actions.appendChild(force);
  panel.appendChild(actions);
  root.appendChild(panel);
}

/* ---------------- ADMIN ---------------- */

async function renderAdmin() {
  const app = document.getElementById("app");
  clear(app);
  app.appendChild(topbar());
  let data;
  try {
    data = await api("/api/admin/users");
  } catch (e) {
    const panel = h("div", "panel pink");
    panel.appendChild(h("div", "panel-title", "⛔ Нет доступа"));
    panel.appendChild(h("div", "", e.message));
    app.appendChild(panel);
    return;
  }
  const back = h("button", "btn sm", "← Назад");
  back.onclick = () => { location.hash = "#/"; };
  app.appendChild(back);
  app.appendChild(h("div", "hero-title", "Панель владельца"));

  const usersPanel = h("div", "panel");
  usersPanel.appendChild(h("div", "panel-title", "👥 Пользователи (" + data.users.length + ")"));
  const rows = h("div", "rows");
  if (!data.users.length) rows.appendChild(h("div", "empty-state", "Пользователей пока нет"));
  data.users.forEach(u => {
    const row = h("div", "row-item admin-user");
    const top = h("div", "admin-user-top");
    top.appendChild(h("span", "chip " + (u.paid ? "paid" : "free"), u.paid ? "💎 оплачено" : "🆓 free"));
    top.appendChild(h("span", "row-title", (u.first_name || u.username || "без имени")));
    top.appendChild(h("span", "row-sub mono", "id " + u.user_id + " · работ: " + u.projects + (u.paid_until && !u.paid_until.startsWith("9999") ? " · до " + u.paid_until.slice(0, 10) : "")));
    row.appendChild(top);
    const actions = h("div", "row-actions");
    const toggle = h("button", "btn sm " + (u.paid ? "red" : "green"), u.paid ? "❌ Снять доступ" : "✅ Оплачено");
    toggle.onclick = async () => {
      toggle.disabled = true;
      try {
        await postJSON("/api/admin/users/" + u.user_id + "/paid", { paid: !u.paid, days: 30 });
        toast(u.paid ? "Доступ отключён" : "Доступ открыт на 30 дней — пользователь получил уведомление", "ok");
        renderAdmin();
      } catch (e) { toast(e.message, "err"); toggle.disabled = false; }
    };
    actions.appendChild(toggle);
    row.appendChild(actions);
    rows.appendChild(row);
  });
  usersPanel.appendChild(rows);
  app.appendChild(usersPanel);

  let eventsData;
  try { eventsData = await api("/api/admin/events"); } catch (e) { eventsData = { events: [], usage: null }; }
  const usage = eventsData.usage;
  if (usage) {
    const up = h("div", "stat-row");
    up.appendChild(statCard(fmtTokens(usage.prompt_tokens), "токенов ввод"));
    up.appendChild(statCard(fmtTokens(usage.completion_tokens), "токенов вывод"));
    up.appendChild(statCard(usage.calls, "запросов к API"));
    app.appendChild(up);
  }
  const eventsPanel = h("div", "panel");
  eventsPanel.appendChild(h("div", "panel-title", "📟 Лента событий"));
  const erows = h("div", "rows");
  if (!eventsData.events || !eventsData.events.length) {
    erows.appendChild(h("div", "empty-state", "Событий пока нет"));
  } else {
    eventsData.events.forEach(ev => {
      const row = h("div", "row-item");
      const main = h("div", "row-main");
      const title = h("div", "event-row");
      title.appendChild(h("span", "event-kind", ev.kind || "event"));
      title.appendChild(h("span", "", (ev.ts || "").slice(5, 16) + (ev.project ? " · " + ev.project : "") + (ev.user_id ? " · id" + ev.user_id : "")));
      main.appendChild(title);
      main.appendChild(h("div", "row-sub", ev.message || ""));
      row.appendChild(main);
      erows.appendChild(row);
    });
  }
  eventsPanel.appendChild(erows);
  app.appendChild(eventsPanel);
}

function fmtTokens(n) {
  n = n || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(0) + "K" : String(n);
}

/* ---------------- старт ---------------- */

route();
