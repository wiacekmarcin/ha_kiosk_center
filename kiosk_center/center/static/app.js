// Okno Kiosk Center w Home Assistant (ingress): wiadomości, lista tabletów, konfiguracja.
// Adresy API względne - strona działa pod adresem ingress HA.
"use strict";
const $ = (s) => document.querySelector(s);
const MAX_SECONDS = 60;
let state = null;
let editing = null;            // id edytowanego tabletu, "" = nowy
let recorder = null, chunks = [], recStart = 0, recTimer = null, cancelled = false;

function esc(t) {
  return String(t ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = {};
  try { body = await r.json(); } catch (e) { /* pusta odpowiedź */ }
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// --- zakładki -----------------------------------------------------------------------------
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-body").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
}

// --- odświeżanie stanu ------------------------------------------------------------------------
async function refresh() {
  try {
    state = await api("api/state");
  } catch (e) {
    $("#st-ha").className = "pill off";
    return;
  }
  $("#st-ha").className = "pill " + (state.ha ? "on" : "off");
  $("#st-mqtt").className = "pill " + (state.mqtt ? "on" : "off");
  renderTargets();
  renderQuick();
  renderHistory();
  renderTablets();
  renderConfig();
  const s = state.settings;
  $("#cond").textContent = s.voice_condition_entity
    ? `(${s.voice_condition_entity} = ${state.condition ?? "?"}, wymagane ${s.voice_condition_state || "on"})` : "";
}

function renderTargets() {
  const sel = $("#target");
  const cur = sel.value;
  const opts = ['<option value="">Wszystkie tablety z odczytywaniem</option>'].concat(
    state.tablets.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}${t.place ? " - " + esc(t.place) : ""}${t.online ? "" : " (niedostępny)"}</option>`));
  sel.innerHTML = opts.join("");
  sel.value = state.tablets.some((t) => t.id === cur) ? cur : "";
}

function renderQuick() {
  const box = $("#quick");
  const key = JSON.stringify(state.settings.quick_messages);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.innerHTML = "";
  state.settings.quick_messages.forEach((m) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = m;
    b.addEventListener("click", () => sendText(m));
    box.appendChild(b);
  });
}

function renderHistory() {
  $("#history").innerHTML = state.history.length ? state.history.map((h) => {
    const what = h.kind === "voice" ? "🎤 " + esc(h.text) : "💬 „" + esc(h.text) + "”";
    const to = h.sent.length ? "do: " + h.sent.map(esc).join(", ") : "nikt nie odebrał";
    const sk = h.skipped.length ? ` <span class="muted">(pominięte: ${h.skipped.map((s) => esc(s[0]) + " - " + esc(s[1])).join("; ")})</span>` : "";
    return `<li><b>${esc(h.time)}</b> ${esc(h.from)}: ${what}<br><span class="muted">${to}</span>${sk}</li>`;
  }).join("") : '<li class="muted">Brak wiadomości od uruchomienia aplikacji</li>';
}

function fmtNum(v, unit, digits = 0) {
  return v === null || v === undefined ? "-" : `${Number(v).toFixed(digits)} ${unit}`;
}

function renderTablets() {
  $("#tablets").innerHTML = state.tablets.length ? state.tablets.map((t) => {
    const low = typeof t.battery === "number" && t.battery <= 20;
    return `<div class="tablet">
      <div class="head"><span class="dot ${t.online ? "on" : ""}"></span>${esc(t.name)}</div>
      <div class="place">${esc(t.place || t.ip)}${t.online ? "" : " - niedostępny"}</div>
      <div class="facts">
        <span>Bateria</span><span class="battery ${low ? "low" : ""}">${fmtNum(t.battery, "%")}${t.charging ? " ⚡" : ""}</span>
        ${t.power_entity ? `<span>Moc ładowania</span><span>${fmtNum(t.power, "W", 1)}</span>` : ""}
        ${t.power_entity || t.energy_entity ? `<span>Energia ładowania</span><span>${fmtNum(t.energy, "kWh", 3)}</span>` : ""}
        ${t.switch_entity ? `<span>Zasilanie</span><span>${esc(t.switch === "on" ? "włączone" : t.switch === "off" ? "wyłączone" : t.switch ?? "-")}</span>` : ""}
        <span>Odczyt głosu</span><span>${t.voice ? "tak" : "nie"}</span>
        <span>Aplikacja</span><span>${esc(t.version || "-")}</span>
      </div>
      <button type="button" class="primary" data-only="${esc(t.id)}">Wyślij wiadomość tylko do niego</button>
    </div>`;
  }).join("") : '<p class="muted">Brak tabletów. Dodaj je w zakładce Konfiguracja albo uruchom aplikację na tablecie - sam się zgłosi.</p>';
  document.querySelectorAll("[data-only]").forEach((b) => b.addEventListener("click", () => {
    $("#target").value = b.dataset.only;
    showTab("msg");
  }));
}

function renderConfig() {
  $("#cfg-tablets").innerHTML = state.tablets.map((t) => `<tr>
    <td>${esc(t.name)}</td><td>${esc(t.ip)}</td><td>${esc(t.place)}</td><td>${esc(t.model)}</td>
    <td>${t.voice ? "tak" : "nie"}</td><td>${esc(t.audio_format)}</td>
    <td><button type="button" data-edit="${esc(t.id)}">Edytuj</button>
        <button type="button" class="danger" data-del="${esc(t.id)}">Usuń</button></td></tr>`).join("");
  document.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => openForm(b.dataset.edit)));
  document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => delTablet(b.dataset.del)));
  if (!$("#formats").options.length)       // raz - nie kasuje wyboru w otwartym formularzu
    $("#formats").innerHTML = Object.entries(state.formats).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  const sf = $("#settings-form");
  if (!sf.dataset.loaded) {                 // nie nadpisujemy pól w trakcie edycji
    sf.dataset.loaded = "1";
    const s = state.settings;
    sf.voice_condition_entity.value = s.voice_condition_entity || "";
    sf.voice_condition_state.value = s.voice_condition_state || "";
    sf.tts_engine.value = s.tts_engine || "";
    sf.tts_language.value = s.tts_language || "";
    sf.quick_messages.value = (s.quick_messages || []).join("\n");
    sf.screens.value = JSON.stringify(s.screens || {}, null, 1);
  }
}

// --- encje do podpowiedzi ---------------------------------------------------------------------
async function loadEntities() {
  try {
    const all = await api("api/entities");
    const fill = (id, list) => { $(id).innerHTML = list.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join(""); };
    fill("#dl-power", all.filter((e) => e.id.startsWith("sensor.") && e.unit === "W"));
    fill("#dl-energy", all.filter((e) => e.id.startsWith("sensor.") && (e.unit === "kWh" || e.unit === "Wh")));
    fill("#dl-switch", all.filter((e) => e.id.startsWith("switch.")));
    fill("#dl-tts", all.filter((e) => e.id.startsWith("tts.")));
    fill("#dl-all", all.filter((e) => /^(switch|binary_sensor|input_boolean|person|group|zone|sensor)\./.test(e.id)));
  } catch (e) { /* bez podpowiedzi */ }
}

// --- formularz tabletu --------------------------------------------------------------------------
function openForm(id) {
  const f = $("#tablet-form");
  const t = id ? state.tablets.find((x) => x.id === id) : {voice: true, audio_format: "m4a", charge_on_below: 30, charge_off_above: 90};
  editing = id || "";
  $("#form-title").textContent = id ? `Tablet: ${t.name}` : "Nowy tablet";
  ["name", "ip", "place", "model", "power_entity", "energy_entity", "switch_entity", "charge_on_below", "charge_off_above"]
    .forEach((k) => { f[k].value = t[k] ?? ""; });
  f.audio_format.value = t.audio_format || "m4a";
  f.voice.checked = !!t.voice;
  $("#form-error").textContent = "";
  f.classList.remove("hidden");
  f.scrollIntoView({behavior: "smooth"});
}
$("#add").addEventListener("click", () => openForm(null));
$("#form-cancel").addEventListener("click", () => { $("#tablet-form").classList.add("hidden"); editing = null; });
$("#tablet-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const data = {};
  ["name", "ip", "place", "model", "audio_format", "power_entity", "energy_entity", "switch_entity"].forEach((k) => { data[k] = f[k].value.trim(); });
  data.voice = f.voice.checked;
  data.charge_on_below = Number(f.charge_on_below.value || 0);
  data.charge_off_above = Number(f.charge_off_above.value || 0);
  try {
    await api(editing ? `api/tablets/${encodeURIComponent(editing)}` : "api/tablets", {
      method: editing ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
    f.classList.add("hidden");
    editing = null;
    refresh();
  } catch (e) {
    $("#form-error").textContent = e.message;
  }
});
async function delTablet(id) {
  const t = state.tablets.find((x) => x.id === id);
  if (!confirm(`Usunąć tablet ${t.name}? Jego encje znikną z Home Assistant.`)) return;
  try { await api(`api/tablets/${encodeURIComponent(id)}`, {method: "DELETE"}); } catch (e) { alert(e.message); }
  refresh();
}

$("#settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  let screens;
  try { screens = JSON.parse(f.screens.value || "{}"); } catch (e) { $("#settings-msg").textContent = "Ekrany: błędny JSON"; return; }
  try {
    await api("api/settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      voice_condition_entity: f.voice_condition_entity.value, voice_condition_state: f.voice_condition_state.value,
      tts_engine: f.tts_engine.value, tts_language: f.tts_language.value,
      quick_messages: f.quick_messages.value, screens})});
    $("#settings-msg").textContent = "Zapisano";
    refresh();
  } catch (e) {
    $("#settings-msg").textContent = e.message;
  }
});

// --- wysyłanie -------------------------------------------------------------------------------------
function showResult(r) {
  const el = $("#result");
  if (r.error) { el.className = "result bad"; el.textContent = r.error; return; }
  const sk = (r.skipped || []).map((s) => `${s[0]} (${s[1]})`).join(", ");
  el.className = "result " + (r.sent && r.sent.length ? "ok" : "bad");
  el.textContent = (r.sent && r.sent.length ? "Wysłano do: " + r.sent.join(", ") : "Nikt nie odebrał") + (sk ? ". Pominięte: " + sk : "");
  refresh();
}
async function sendText(text) {
  text = (text || "").trim();
  if (!text) return;
  $("#result").textContent = "Wysyłanie…";
  try {
    showResult(await api("api/text", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text, to: $("#target").value || null, force: $("#force").checked})}));
    $("#text").value = "";
  } catch (e) { showResult({error: e.message}); }
}
$("#send-text").addEventListener("click", () => sendText($("#text").value));
$("#text").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText($("#text").value); });

// --- nagrywanie (format przeglądarki / telefonu, konwersję robi centrum) ----------------------------
async function startRec() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    $("#rec-info").textContent = "Nagrywanie wymaga połączenia HTTPS (np. przez Nabu Casa albo aplikację HA na telefonie).";
    return;
  }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({audio: true}); }
  catch (e) { $("#rec-info").textContent = "Brak dostępu do mikrofonu: " + e.message; return; }
  chunks = []; cancelled = false;
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(recTimer);
    $("#rec").classList.remove("recording");
    $("#rec-label").textContent = "Nagraj wiadomość";
    $("#rec-cancel").classList.add("hidden");
    const seconds = (Date.now() - recStart) / 1000;
    if (cancelled || seconds < 0.7) { $("#rec-info").textContent = cancelled ? "Anulowano" : "Za krótkie nagranie"; return; }
    const blob = new Blob(chunks, {type: recorder.mimeType || "audio/webm"});
    $("#rec-info").textContent = `Wysyłanie ${seconds.toFixed(1)} s…`;
    $("#rec").disabled = true;
    try {
      const to = $("#target").value;
      showResult(await api(`api/voice?to=${encodeURIComponent(to)}&force=${$("#force").checked ? 1 : 0}`,
        {method: "POST", headers: {"Content-Type": blob.type}, body: blob}));
      $("#rec-info").textContent = "";
    } catch (e) { showResult({error: e.message}); $("#rec-info").textContent = ""; }
    $("#rec").disabled = false;
  };
  recorder.start();
  recStart = Date.now();
  $("#rec").classList.add("recording");
  $("#rec-cancel").classList.remove("hidden");
  $("#rec-info").textContent = "";
  recTimer = setInterval(() => {
    const s = Math.floor((Date.now() - recStart) / 1000);
    $("#rec-label").textContent = `Zakończ i wyślij (${s} s)`;
    if (s >= MAX_SECONDS) recorder.stop();
  }, 250);
}
$("#rec").addEventListener("click", () => {
  if (recorder && recorder.state === "recording") recorder.stop(); else startRec();
});
$("#rec-cancel").addEventListener("click", () => { cancelled = true; if (recorder) recorder.stop(); });

refresh();
loadEntities();
setInterval(refresh, 5000);
