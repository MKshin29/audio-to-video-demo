"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { jobId: null, job: null, segments: [], file: null, stopAt: null };
const SETTINGS_FIELDS = ["robotName", "theme", "resolution", "reveal", "clock", "showTimes"];

// ---------------------------------------------------------------- общее
function show(name) {
  document.querySelectorAll(".screen").forEach((s) => (s.hidden = s.id !== "screen-" + name));
  const order = ["upload", "edit", "video"];
  const cur = { upload: 0, progress: 1, edit: 1, video: 2 }[name];
  document.querySelectorAll(".steps li").forEach((li) => {
    const i = order.indexOf(li.dataset.step);
    li.classList.toggle("active", i === cur);
    li.classList.toggle("done", i < cur);
  });
  window.scrollTo(0, 0);
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { msg = (await res.json()).error || msg; } catch (e) { /* не JSON */ }
    throw new Error(msg);
  }
  return res;
}

function fmtTime(sec) {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
}

function parseTime(str) {
  str = String(str).trim().replace(",", ".");
  if (!str) return NaN;
  const parts = str.split(":").map(Number);
  if (parts.some((p) => isNaN(p) || p < 0)) return NaN;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function fmtDuration(sec) {
  const s = Math.round(sec || 0);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function lsGet(key, def) { try { return localStorage.getItem(key) ?? def; } catch (e) { return def; } }
function lsSet(key, val) { try { localStorage.setItem(key, val); } catch (e) { /* недоступно */ } }

// ---------------------------------------------------------------- шаг 1: загрузка
async function initUpload() {
  try {
    const cfg = await (await api("/api/config")).json();
    const sel = $("#modelSelect");
    sel.innerHTML = "";
    for (const m of cfg.models) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.title + (m.downloaded ? "  ✓ скачана" : "");
      sel.appendChild(o);
    }
    sel.value = lsGet("model", "small");
    if (!sel.value) sel.value = "small";
  } catch (e) {
    $("#uploadError").textContent = "Не удалось получить настройки: " + e.message;
  }
  $("#langSelect").value = lsGet("lang", "ru");
  $("#maxCharsUpload").value = lsGet("maxChars", "200");
  $("#maxChars").value = lsGet("maxChars", "200") === "0" ? "200" : lsGet("maxChars", "200");
  loadRecent();
}

async function loadRecent() {
  try {
    const list = await (await api("/api/jobs")).json();
    const ul = $("#recentList");
    ul.innerHTML = "";
    const items = list.filter((j) => j.status === "ready");
    $("#recentCard").hidden = !items.length;
    for (const j of items) {
      const li = document.createElement("li");
      const date = new Date(j.created * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
      li.innerHTML = `<span class="r-name"></span><span class="r-meta">${fmtDuration(j.duration)} · ${date}${j.video ? " · есть видео" : ""}</span>
        <a class="open">Открыть</a><a class="del" title="Удалить">✕</a>`;
      li.querySelector(".r-name").textContent = j.filename;
      li.querySelector(".open").onclick = () => openJob(j.id);
      li.querySelector(".del").onclick = async () => {
        if (!confirm("Удалить запись «" + j.filename + "» и созданное видео?")) return;
        await api("/api/job/" + j.id, { method: "DELETE" });
        loadRecent();
      };
      ul.appendChild(li);
    }
  } catch (e) { /* список не критичен */ }
}

function setFile(file) {
  state.file = file;
  $("#dropzone").classList.toggle("has-file", !!file);
  $("#dzTitle").textContent = file ? file.name : "Перетащите файл сюда или нажмите, чтобы выбрать";
  $("#dzSub").textContent = file ? (file.size / 1048576).toFixed(1) + " МБ" : "";
  $("#uploadBtn").disabled = !file;
  $("#uploadError").textContent = "";
}

function upload() {
  if (!state.file) return;
  lsSet("model", $("#modelSelect").value);
  lsSet("lang", $("#langSelect").value);
  lsSet("maxChars", $("#maxCharsUpload").value);
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("model", $("#modelSelect").value);
  fd.append("language", $("#langSelect").value);
  fd.append("skip_asr", $("#skipAsr").checked ? "1" : "0");
  fd.append("max_chars", $("#maxCharsUpload").value);

  show("progress");
  $("#progressTitle").textContent = "Загрузка файла…";
  $("#progressMsg").textContent = "";
  $("#progressError").textContent = "";
  $("#progressBack").hidden = true;
  setBar("#progressFill", 0);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) setBar("#progressFill", e.loaded / e.total); };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (e) { /* пусто */ }
    if (xhr.status !== 200) return progressFail(data.error || "Ошибка загрузки: " + xhr.status);
    location.hash = "#/job/" + data.id;
    pollTranscription(data.id);
  };
  xhr.onerror = () => progressFail("Сервер недоступен. Проверьте, что окно start.bat не закрыто.");
  xhr.send(fd);
}

function setBar(sel, p) { $(sel).style.width = Math.round(Math.max(0, Math.min(1, p)) * 100) + "%"; }

function progressFail(msg) {
  $("#progressTitle").textContent = "Ошибка";
  $("#progressError").textContent = msg;
  $("#progressBack").hidden = false;
}

async function pollTranscription(id) {
  show("progress");
  $("#progressTitle").textContent = "Распознавание…";
  while (true) {
    let job;
    try { job = await (await api("/api/job/" + id)).json(); } catch (e) { return progressFail(e.message); }
    if (job.status === "error") return progressFail(job.error);
    if (job.status === "ready") return openEditor(job);
    $("#progressMsg").textContent = job.message || "";
    const p = job.progress || 0;
    $("#progressFill").parentElement.classList.toggle("indeterminate", p <= 0);
    setBar("#progressFill", p);
    $("#progressTitle").textContent = "Распознавание… " + (p > 0 ? Math.round(p * 100) + "%" : "");
    await new Promise((r) => setTimeout(r, 800));
  }
}

async function openJob(id) {
  location.hash = "#/job/" + id;
  try {
    const job = await (await api("/api/job/" + id)).json();
    if (job.status === "ready") openEditor(job);
    else if (job.status === "error") { show("progress"); progressFail(job.error); }
    else pollTranscription(id);
  } catch (e) {
    location.hash = "";
    show("upload");
  }
}

// ---------------------------------------------------------------- шаг 2: редактор
function openEditor(job) {
  state.jobId = job.id;
  state.job = job;
  state.segments = (job.segments || []).map((s) => ({ ...s }));
  $("#fileName").textContent = job.filename + " · " + fmtDuration(job.duration);
  const audio = $("#audio");
  const src = "/api/job/" + job.id + "/audio";
  if (!audio.src.endsWith(src)) audio.src = src;
  const st = job.settings || {};
  const saved = JSON.parse(lsGet("settings", "{}") || "{}");
  for (const f of SETTINGS_FIELDS) {
    const el = $("#" + f);
    const v = st[keyOf(f)] ?? saved[keyOf(f)];
    if (v === undefined || v === null) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
  show("edit");
  renderList();
  updatePreview();
}

function keyOf(field) {
  return { robotName: "robot_name", showTimes: "show_times" }[field] || field;
}

function getSettings() {
  const s = {};
  for (const f of SETTINGS_FIELDS) {
    const el = $("#" + f);
    s[keyOf(f)] = el.type === "checkbox" ? el.checked : el.value;
  }
  return s;
}

function renderList() {
  const list = $("#segList");
  list.innerHTML = "";
  state.segments.forEach((seg, i) => list.appendChild(makeRow(seg, i)));
  // высоту полей можно посчитать только после вставки в страницу
  list.querySelectorAll("textarea").forEach(autoGrow);
  if (!state.segments.length) {
    list.innerHTML = '<div class="card muted center">Баблов нет. Нажмите «＋ Добавить бабл».</div>';
  }
  updateGrouping();
}

// баблы одного говорящего подряд показываем ближе друг к другу, как в чате
function updateGrouping() {
  document.querySelectorAll("#segList .seg").forEach((row, i) => {
    row.classList.toggle("cont", i > 0 && state.segments[i - 1].speaker === state.segments[i].speaker);
  });
}

function makeRow(seg, i) {
  const node = $("#segTpl").content.firstElementChild.cloneNode(true);
  node.dataset.index = i;
  const setSpeakerUI = () => {
    node.classList.toggle("robot", seg.speaker === "robot");
    node.classList.toggle("client", seg.speaker === "client");
    node.querySelector(".speaker").textContent = seg.speaker === "robot" ? "🤖 Робот" : "👤 Клиент";
  };
  setSpeakerUI();
  const ta = node.querySelector(".text");
  ta.value = seg.text;
  node.classList.toggle("empty", !seg.text.trim());
  autoGrow(ta);
  const tStart = node.querySelector(".t-start");
  const tEnd = node.querySelector(".t-end");
  tStart.value = fmtTime(seg.start);
  tEnd.value = fmtTime(seg.end);

  node.querySelector(".speaker").onclick = () => {
    seg.speaker = seg.speaker === "robot" ? "client" : "robot";
    setSpeakerUI();
    updateGrouping();
    changed();
  };
  ta.oninput = () => {
    seg.text = ta.value;
    node.classList.toggle("empty", !seg.text.trim());
    autoGrow(ta);
    changed(false);
  };
  ta.onkeydown = (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); splitSeg(i, ta.selectionStart); }
  };
  const bindTime = (input, key) => {
    input.onchange = () => {
      const v = parseTime(input.value);
      const bad = isNaN(v);
      input.classList.toggle("bad", bad);
      if (bad) return;
      seg[key] = Math.min(v, state.job.duration);
      if (key === "start" && seg.end < seg.start) seg.end = seg.start;
      if (key === "end" && seg.end < seg.start) seg.start = seg.end;
      sortAndRender();
      changed();
    };
  };
  bindTime(tStart, "start");
  bindTime(tEnd, "end");
  node.querySelector(".now-start").onclick = () => { tStart.value = fmtTime($("#audio").currentTime); tStart.onchange(); };
  node.querySelector(".now-end").onclick = () => { tEnd.value = fmtTime($("#audio").currentTime); tEnd.onchange(); };
  node.querySelector(".play").onclick = () => playSeg(seg);
  node.querySelector(".split").onclick = () => splitSeg(i, ta.selectionStart || Math.floor(ta.value.length / 2));
  node.querySelector(".merge").onclick = () => mergeSeg(i);
  node.querySelector(".insert").onclick = () => insertAfter(i);
  node.querySelector(".del").onclick = () => {
    if (seg.text.trim() && !confirm("Удалить этот бабл?")) return;
    state.segments.splice(i, 1);
    renderList();
    changed();
  };
  node.querySelector(".merge").disabled = i >= state.segments.length - 1;
  return node;
}

function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.max(62, ta.scrollHeight + 2) + "px";
}

function sortAndRender() {
  const before = state.segments.slice();
  state.segments.sort((a, b) => a.start - b.start);
  if (before.some((s, i) => s !== state.segments[i])) renderList();
  else document.querySelectorAll("#segList .seg").forEach((row, i) => {
    row.querySelector(".t-start").value = fmtTime(state.segments[i].start);
    row.querySelector(".t-end").value = fmtTime(state.segments[i].end);
  });
}

function splitSeg(i, pos) {
  const seg = state.segments[i];
  const text = seg.text;
  // разрезаем по ближайшему пробелу, чтобы не делить слова
  let cut = Math.max(0, Math.min(text.length, pos));
  const left = text.slice(0, cut).trim();
  const right = text.slice(cut).trim();
  if (!left || !right) { alert("Поставьте курсор в тексте в то место, где нужно разделить бабл."); return; }
  const frac = cut / text.length;
  const mid = +(seg.start + (seg.end - seg.start) * frac).toFixed(2);
  state.segments.splice(i, 1, { ...seg, text: left, end: mid }, { ...seg, text: right, start: mid });
  renderList();
  focusRow(i + 1);
  changed();
}

function mergeSeg(i) {
  const a = state.segments[i], b = state.segments[i + 1];
  if (!b) return;
  state.segments.splice(i, 2, { ...a, text: (a.text.trim() + " " + b.text.trim()).trim(), end: Math.max(a.end, b.end) });
  renderList();
  changed();
}

function insertAfter(i) {
  const prev = state.segments[i];
  const next = state.segments[i + 1];
  const dur = state.job.duration;
  const start = Math.min(prev ? prev.end : 0, dur);
  // если до следующего бабла нет паузы, новый бабл длится 1.5 с (время можно поправить)
  let end = Math.min(next ? next.start : dur, start + 2);
  if (end - start < 0.5) end = Math.min(dur, start + 1.5);
  const seg = { speaker: prev && prev.speaker === "robot" ? "client" : "robot", start, end, text: "" };
  state.segments.splice(i + 1, 0, seg);
  renderList();
  focusRow(i + 1);
  changed();
}

function addAtCurrentTime() {
  const t = +$("#audio").currentTime.toFixed(2);
  const seg = { speaker: "client", start: t, end: Math.min(state.job.duration, t + 2), text: "" };
  state.segments.push(seg);
  state.segments.sort((a, b) => a.start - b.start);
  renderList();
  focusRow(state.segments.indexOf(seg));
  changed();
}

async function splitLong() {
  const before = state.segments.length;
  try {
    const res = await api("/api/job/" + state.jobId + "/split", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: state.segments, max_chars: +$("#maxChars").value }),
    });
    state.segments = (await res.json()).segments;
  } catch (e) { $("#editError").textContent = e.message; return; }
  renderList();
  changed();
  const added = state.segments.length - before;
  flash(added > 0 ? "Добавлено баблов: " + added : "Длинных баблов не найдено");
}

function flash(msg) {
  const el = $("#flash");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => (el.hidden = true), 2500);
}

function focusRow(i) {
  const row = document.querySelectorAll("#segList .seg")[i];
  if (row) { row.scrollIntoView({ block: "center" }); row.querySelector(".text").focus(); }
}

function playSeg(seg) {
  const audio = $("#audio");
  audio.currentTime = seg.start;
  state.stopAt = seg.end;
  audio.play();
}

function onTimeUpdate() {
  const audio = $("#audio");
  const t = audio.currentTime;
  if (state.stopAt !== null && t >= state.stopAt) { audio.pause(); state.stopAt = null; }
  document.querySelectorAll("#segList .seg").forEach((row, i) => {
    const s = state.segments[i];
    row.classList.toggle("playing", !!s && !audio.paused && t >= s.start && t <= s.end);
  });
}

// автосохранение и предпросмотр
let saveTimer = null, previewTimer = null;
function changed(previewNow = true) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 700);
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, previewNow ? 300 : 1200);
}

async function save() {
  if (!state.jobId) return;
  lsSet("settings", JSON.stringify(getSettings()));
  try {
    await api("/api/job/" + state.jobId + "/segments", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: state.segments, settings: getSettings() }),
    });
  } catch (e) { $("#editError").textContent = "Не удалось сохранить правки: " + e.message; }
}

let previewUrl = null, previewBusy = false;
async function updatePreview() {
  if (!state.jobId || previewBusy) return;
  previewBusy = true;
  try {
    let t = $("#audio").currentTime || 0;
    // в самом начале записи чат пустой — показываем кадр с первыми репликами
    if (t < 0.1 && state.segments.length) {
      const last = state.segments[Math.min(3, state.segments.length - 1)];
      t = last.end;
    }
    const res = await api("/api/job/" + state.jobId + "/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: state.segments, settings: getSettings(), t }),
    });
    const blob = await res.blob();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(blob);
    $("#previewImg").src = previewUrl;
  } catch (e) { /* предпросмотр не критичен */ }
  previewBusy = false;
}

// ---------------------------------------------------------------- шаг 3: видео
async function startRender() {
  $("#editError").textContent = "";
  const empty = state.segments.filter((s) => !s.text.trim()).length;
  if (empty && !confirm("Есть пустые баблы (" + empty + "). Они не попадут в видео. Продолжить?")) return;
  $("#audio").pause();
  clearTimeout(saveTimer);
  try {
    await api("/api/job/" + state.jobId + "/render", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: state.segments, settings: getSettings() }),
    });
  } catch (e) {
    if (!/уже создаётся/.test(e.message)) { $("#editError").textContent = e.message; return; }
  }
  location.hash = "#/job/" + state.jobId + "/video";
  pollRender();
}

async function pollRender() {
  show("video");
  $("#renderProgress").hidden = false;
  $("#renderDone").hidden = true;
  $("#renderError").textContent = "";
  setBar("#renderFill", 0);
  const started = Date.now();
  while (true) {
    let job;
    try { job = await (await api("/api/job/" + state.jobId)).json(); } catch (e) {
      $("#renderError").textContent = e.message; return;
    }
    if (job.render_status === "error") { $("#renderError").textContent = job.render_error; $("#renderProgress").hidden = true; return; }
    if (job.render_status === "done" || (job.render_status !== "rendering" && job.video)) return showVideo(job);
    const p = job.render_progress || 0;
    setBar("#renderFill", p);
    let msg = "Готово " + Math.round(p * 100) + "%";
    const el = (Date.now() - started) / 1000;
    if (p > 0.03 && el > 3) msg += " · осталось примерно " + fmtDuration(el / p - el);
    $("#renderMsg").textContent = msg;
    await new Promise((r) => setTimeout(r, 700));
  }
}

function showVideo(job) {
  show("video");
  $("#renderProgress").hidden = true;
  $("#renderDone").hidden = false;
  const v = "/api/job/" + job.id + "/video?v=" + Date.now();
  $("#video").src = v;
  $("#downloadLink").href = "/api/job/" + job.id + "/video?download=1";
}

// ---------------------------------------------------------------- маршрутизация и события
async function route() {
  const m = location.hash.match(/^#\/job\/([0-9a-f]+)(\/video)?/);
  if (!m) { show("upload"); return; }
  try {
    const job = await (await api("/api/job/" + m[1])).json();
    if (job.status !== "ready") return openJob(m[1]);
    openEditor(job);
    if (m[2]) {
      if (job.render_status === "rendering") pollRender();
      else if (job.video) showVideo(job);
    }
  } catch (e) { location.hash = ""; show("upload"); }
}

function bind() {
  const dz = $("#dropzone");
  $("#fileInput").onchange = (e) => setFile(e.target.files[0] || null);
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });
  $("#skipAsr").onchange = () => { $("#uploadBtn").textContent = $("#skipAsr").checked ? "Далее" : "Распознать"; };
  $("#uploadBtn").onclick = upload;
  $("#progressBack").onclick = () => { location.hash = ""; show("upload"); loadRecent(); };
  $("#homeLink").onclick = () => { $("#audio").pause(); location.hash = ""; setFile(null); show("upload"); loadRecent(); };

  $("#audio").addEventListener("timeupdate", onTimeUpdate);
  $("#audio").addEventListener("seeked", () => { if ($("#audio").paused) updatePreview(); });
  $("#audio").addEventListener("pause", () => { updatePreview(); });
  // ручная перемотка за пределы реплики отменяет автоостановку кнопки ▶
  $("#audio").addEventListener("seeking", () => {
    const a = $("#audio");
    if (state.stopAt !== null && (a.currentTime > state.stopAt + 0.5)) state.stopAt = null;
  });
  $("#swapBtn").onclick = () => {
    state.segments.forEach((s) => (s.speaker = s.speaker === "robot" ? "client" : "robot"));
    renderList();
    changed();
  };
  $("#addBtn").onclick = addAtCurrentTime;
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => document.querySelectorAll("#segList textarea").forEach(autoGrow), 150);
  });
  $("#splitLongBtn").onclick = splitLong;
  $("#maxChars").onchange = () => lsSet("maxChars", $("#maxChars").value);
  $("#previewBtn").onclick = updatePreview;
  SETTINGS_FIELDS.forEach((f) => $("#" + f).addEventListener("change", () => changed()));
  $("#robotName").addEventListener("input", () => changed(false));
  $("#renderBtn").onclick = startRender;
  $("#backToEdit").onclick = () => { $("#video").pause(); location.hash = "#/job/" + state.jobId; show("edit"); };
  $("#newFile").onclick = () => { $("#video").pause(); location.hash = ""; setFile(null); show("upload"); loadRecent(); };
}

bind();
initUpload();
route();
