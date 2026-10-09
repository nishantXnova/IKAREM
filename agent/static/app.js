/* HERMES client: BYOK (key-only), markdown chat, WS streaming + HTTP fallback. */
const $ = (id) => document.getElementById(id);
const chat = $("chat"), input = $("input"), sendBtn = $("send"), stopBtn = $("stopBtn");
const sessionsEl = $("sessions"), statusDot = $("statusDot"), modelBadge = $("modelBadge");

let sessionId = localStorage.getItem("hermes.sid") || "default";
let providers = [];
let selProvider = localStorage.getItem("hermes.provider") || "openai";
let busy = false, stopFlag = false, ws = null, aborter = null;

/* ================= BYOK: endpoint+model auto, key only ================= */
const byok = () => ({
  provider: localStorage.getItem("hermes.provider") || "openai",
  model: localStorage.getItem("hermes.model") || "",
  baseUrl: localStorage.getItem("hermes.baseurl") || "",
  apiKey: localStorage.getItem("hermes.key") || "",
});
const meta = (id) => providers.find((p) => p.id === id) || {};
const effBase = () => byok().baseUrl || meta(byok().provider).default_base_url || "";
const effModel = () => byok().model || (meta(byok().provider).models || [])[0] || "";
const needsKey = () => { const p = byok().provider; return p !== "ollama" && p !== "lmstudio"; };

function headers() {
  const b = byok();
  return { "Content-Type": "application/json", "X-Provider": b.provider,
    "X-Model": b.model, "X-Base-Url": b.baseUrl, "X-Api-Key": b.apiKey };
}
function refreshBadge() {
  const b = byok();
  modelBadge.textContent = b.provider + " / " + (effModel() || "default") + (b.apiKey ? "  [key]" : needsKey() ? "  [no key]" : "  [local]");
  $("byokBtn").classList.toggle("key-on", !!b.apiKey || !needsKey());
  $("keyReq").textContent = needsKey() ? (b.apiKey ? "saved" : "required") : "not needed";
  $("keyReq").classList.toggle("ok", !!b.apiKey || !needsKey());
}
function toast(msg, kind) {
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : ""); t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

/* ================= markdown ================= */
function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^| )_([^_ ][^_]*)_/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const out = []; const lines = String(src).split("\n");
  let i = 0, inCode = false, codeBuf = [], listBuf = [], tableBuf = [];
  const flushList = () => { if (listBuf.length) { out.push("<ul>" + listBuf.map((l) => "<li>" + inline(l) + "</li>").join("") + "</ul>"); listBuf = []; } };
  const flushTable = () => {
    if (tableBuf.length > 1) {
      const rows = tableBuf.filter((r) => !/^\|?[\s:\-|]+\|?$/.test(r));
      out.push("<table>" + rows.map((r, ri) => "<tr>" + r.split("|").filter((c) => c.trim() !== "").map((c) => ri === 0 ? "<th>" + inline(c.trim()) + "</th>" : "<td>" + inline(c.trim()) + "</td>").join("") + "</tr>").join("") + "</table>");
    } else if (tableBuf.length) out.push("<p>" + inline(tableBuf[0]) + "</p>");
    tableBuf = [];
  };
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      if (!inCode) { flushList(); flushTable(); inCode = true; codeBuf = []; }
      else { inCode = false; out.push('<pre><button class="copy" data-copy>COPY</button><code>' + esc(codeBuf.join("\n")) + "</code></pre>"); }
      i++; continue;
    }
    if (inCode) { codeBuf.push(ln); i++; continue; }
    if (/^\|.*\|$/.test(ln.trim())) { flushList(); tableBuf.push(ln.trim()); i++; continue; }
    flushTable();
    if (/^#{1,3}\s/.test(ln)) { flushList(); const l = ln.replace(/^#+/, "").trim(); out.push("<h3>" + inline(l) + "</h3>"); }
    else if (/^&gt;|^>/.test(ln)) { flushList(); out.push("<blockquote>" + inline(ln.replace(/^>\s?/, "")) + "</blockquote>"); }
    else if (/^[-*]\s+/.test(ln)) listBuf.push(ln.replace(/^[-*]\s+/, ""));
    else if (/^\d+\.\s+/.test(ln)) { flushList(); out.push("<p>" + inline(ln.replace(/^\d+\.\s+/, "")) + "</p>"); }
    else if (!ln.trim()) { flushList(); }
    else { flushList(); out.push("<p>" + inline(ln) + "</p>"); }
    i++;
  }
  flushList(); flushTable();
  if (inCode) out.push("<pre><code>" + esc(codeBuf.join("\n")) + "</code></pre>");
  return out.join("");
}

/* ================= chat render ================= */
function bubble(role, html) {
  const d = document.createElement("div");
  d.className = "msg " + (role === "user" ? "user" : role === "assistant" ? "ai" : "sys");
  if (role === "user") d.textContent = html;
  else d.innerHTML = html + (role === "assistant" ? '<button class="copy" data-copy>COPY</button>' : "");
  chat.appendChild(d); chat.scrollTop = chat.scrollHeight;
  return d;
}
function traceBox(name, args) {
  const d = document.createElement("div");
  d.className = "trace";
  const det = document.createElement("details"); det.open = true;
  const sum = document.createElement("summary"); sum.textContent = "[tool] " + name;
  det.appendChild(sum);
  const pre = document.createElement("pre"); pre.textContent = JSON.stringify(args).slice(0, 800);
  det.appendChild(pre);
  d.appendChild(det); chat.appendChild(d);
  d.dataset.tool = name; d.dataset.args = JSON.stringify(args || {});
  chat.scrollTop = chat.scrollHeight;
  return d;
}
function traceResult(box, result) {
  const det = box.querySelector("details");
  if (result && result.needs_confirm) {
    const div = document.createElement("div"); div.textContent = "Approval needed: " + result.message;
    box.appendChild(div);
    const bar = document.createElement("div"); bar.className = "confirm-bar";
    const yes = document.createElement("button"); yes.className = "yes"; yes.textContent = "Approve + run";
    yes.onclick = () => approveRetry(box);
    const no = document.createElement("button"); no.textContent = "Deny";
    no.onclick = () => { const s = document.createElement("div"); s.textContent = "Denied."; box.appendChild(s); bar.remove(); };
    bar.append(yes, no); box.appendChild(bar);
  } else {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(result).slice(0, 2000);
    det.appendChild(pre);
  }
  chat.scrollTop = chat.scrollHeight;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b) return;
  const pre = b.closest("pre"), msg = b.closest(".msg");
  const text = pre ? pre.querySelector("code").innerText : msg ? msg.innerText.replace(/COPY$/, "") : "";
  navigator.clipboard.writeText(text).then(() => { b.textContent = "OK"; setTimeout(() => b.textContent = "COPY", 1200); });
});
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, signal: aborter ? aborter.signal : undefined,
    headers: { ...headers(), ...((opts && opts.headers) || {}) } });
  const data = await r.json().catch(() => ({}));
  if (!r.ok && (data.need_key || r.status === 428)) { openByok(); throw new Error(data.error || "API key needed"); }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

/* ================= sessions ================= */
async function loadSessions() {
  try {
    const data = await fetch("/api/sessions", { headers: headers() }).then((r) => r.json());
    sessionsEl.innerHTML = "";
    (data.sessions || []).forEach((s) => {
      const d = document.createElement("div");
      d.className = "sess" + (s.id === sessionId ? " on" : "");
      const sp = document.createElement("span"); sp.textContent = s.title || s.id; sp.title = "Double-click to rename";
      sp.ondblclick = async (ev) => {
        ev.stopPropagation();
        const t = prompt("Rename chat:", s.title || "");
        if (t === null) return;
        await api("/api/sessions", { method: "POST", body: JSON.stringify({ title: t || "New chat" }) });
        loadSessions();
      };
      const x = document.createElement("button");
      x.className = "del"; x.textContent = "\u00d7"; x.title = "Delete";
      x.onclick = async (e) => { e.stopPropagation();
        await fetch("/api/sessions/" + s.id, { method: "DELETE", headers: headers() }); loadSessions(); };
      d.onclick = () => { sessionId = s.id; localStorage.setItem("hermes.sid", s.id); loadSessions(); loadLog(); };
      d.append(sp, x); sessionsEl.appendChild(d);
    });
  } catch (e) { /* server down */ }
}
async function loadLog() {
  chat.innerHTML = "";
  try {
    const data = await fetch("/api/sessions/" + sessionId, { headers: headers() }).then((r) => r.json());
    (data.messages || []).forEach((m) => {
      if (m.role === "user") bubble("user", m.content);
      else if (m.role === "assistant") {
        bubble("assistant", md(m.content));
        (m.tool_trace || []).forEach((t) => traceResult(traceBox(t.tool, t.args), t.result));
      }
    });
  } catch (e) { bubble("sys", "Cannot reach server: " + esc(e.message)); }
}
$("newChat").onclick = async () => {
  try {
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify({ title: "New chat" }) });
    sessionId = s.id; localStorage.setItem("hermes.sid", s.id);
    loadSessions(); loadLog(); input.focus();
  } catch (e) { toast(e.message, "err"); }
};

/* ================= send: WS streaming, HTTP fallback ================= */
function setBusy(on) {
  busy = on; sendBtn.disabled = on;
  stopBtn.style.display = on ? "inline" : "none";
  statusDot.className = on ? "busy" : "live";
}
async function send() {
  const text = input.value.trim();
  if (!text || busy) return;
  const b = byok();
  if (needsKey() && !b.apiKey && !b.baseUrl) { openByok(); toast("Paste your key first - only the key is needed.", "err"); return; }
  bubble("user", text); input.value = ""; autogrow();
  setBusy(true); stopFlag = false; aborter = new AbortController();
  const ai = bubble("assistant", "<i>thinking...</i>"); ai.classList.add("thinking");
  const payload = { message: text, session_id: sessionId, provider: b.provider,
    model: b.model, base_url: b.baseUrl, api_key: b.apiKey };
  let streamed = false;
  try { streamed = await sendWS(payload, ai); } catch (e) { streamed = false; }
  if (!streamed && !stopFlag) await sendHTTP(payload, ai);
  if (!stopFlag) { setBusy(false); loadSessions(); }
}
function sendWS(payload, ai) {
  return new Promise((resolve) => {
    let gotFinal = false, settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    try { ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/chat"); }
    catch (e) { done(false); return; }
    const timer = setTimeout(() => { try { ws.close(); } catch (e) {} done(false); }, 8000);
    ws.onopen = () => ws.send(JSON.stringify(payload));
    ws.onmessage = (ev) => {
      if (stopFlag) return;
      clearTimeout(timer);
      const m = JSON.parse(ev.data);
      if (m.type === "thought") { ai.classList.remove("thinking"); ai.innerHTML = md(m.text) + '<button class="copy" data-copy>COPY</button>'; }
      else if (m.type === "tool_call") { ai.classList.remove("thinking"); traceBox(m.name, m.arguments); }
      else if (m.type === "tool_result") {
        const boxes = chat.querySelectorAll(".trace");
        traceResult(boxes[boxes.length - 1], m.result);
      }
      else if (m.type === "final") { gotFinal = true; ai.classList.remove("thinking"); ai.innerHTML = md(m.answer) + '<button class="copy" data-copy>COPY</button>'; done(true); }
      else if (m.type === "error") {
        ai.classList.remove("thinking"); ai.innerHTML = "<b>Warning:</b> " + esc(m.message);
        if (m.need_key) openByok();
        gotFinal = true; done(true);
      }
    };
    ws.onerror = () => { clearTimeout(timer); done(false); };
    ws.onclose = () => { clearTimeout(timer); done(gotFinal); };
  });
}
async function sendHTTP(payload, ai) {
  try {
    const r = await api("/api/chat", { method: "POST",
      body: JSON.stringify({ message: payload.message, session_id: payload.session_id }) });
    ai.classList.remove("thinking");
    ai.innerHTML = md(r.answer) + '<button class="copy" data-copy>COPY</button>';
    (r.trace || []).forEach((t) => traceResult(traceBox(t.tool, t.args), t.result));
  } catch (e) {
    if (e.name === "AbortError") return;
    ai.classList.remove("thinking"); ai.innerHTML = "<b>Warning:</b> " + esc(e.message);
  }
}
async function approveRetry(box) {
  const name = box.dataset.tool;
  let args = {}; try { args = JSON.parse(box.dataset.args || "{}"); } catch (e) {}
  args.confirm = true;
  const bar = box.querySelector(".confirm-bar"); if (bar) bar.remove();
  try {
    const r = await api("/api/tools/call", { method: "POST", body: JSON.stringify({ name, arguments: args }) });
    const det = box.querySelector("details"); const pre = document.createElement("pre");
    pre.textContent = "approved -> " + JSON.stringify(r.result).slice(0, 2000);
    det.appendChild(pre);
    toast("Approved tool executed. Ask HERMES to continue with the result.", "ok");
  } catch (e) { toast(e.message, "err"); }
}
stopBtn.onclick = () => { stopFlag = true; try { if (ws) ws.close(); } catch (e) {} if (aborter) aborter.abort(); setBusy(false); toast("Stopped."); };
sendBtn.onclick = send;
function autogrow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 160) + "px"; }
input.addEventListener("input", autogrow);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
document.querySelectorAll(".chips button").forEach((c) => c.onclick = () => { input.value = c.dataset.q; autogrow(); input.focus(); });
$("menuBtn").onclick = () => $("app").classList.toggle("nav-closed");

/* ================= drawer: files + memory ================= */
let cwd = ".", crumbs = ["."];
function openDrawer(tab) {
  $("drawer").classList.add("open");
  (tab === "mem" ? $("tabMem") : $("tabFiles")).click();
}
$("filesBtn").onclick = () => openDrawer("files");
$("drawerClose").onclick = () => $("drawer").classList.remove("open");
$("tabFiles").onclick = () => { $("tabFiles").classList.add("on"); $("tabMem").classList.remove("on");
  $("fileList").style.display = ""; $("crumbs").style.display = ""; $("memPane").style.display = "none";
  if (!$("fileView").dataset.file) $("fileView").style.display = "none"; listFiles(cwd); };
$("tabMem").onclick = () => { $("tabMem").classList.add("on"); $("tabFiles").classList.remove("on");
  $("fileList").style.display = "none"; $("crumbs").style.display = "none"; $("fileView").style.display = "none";
  $("memPane").style.display = "flex"; loadMem(); };
async function listFiles(path) {
  cwd = path;
  $("crumbs").innerHTML = "";
  crumbs.forEach((c, i) => {
    const btn = document.createElement("button"); btn.textContent = (i ? "/ " : "") + c;
    btn.onclick = () => { crumbs = crumbs.slice(0, i + 1); listFiles(crumbs.join("/").replace(/^\.\//, "") || "."); };
    $("crumbs").appendChild(btn);
  });
  try {
    const r = await api("/api/files?path=" + encodeURIComponent(path));
    const box = $("fileList"); box.innerHTML = "";
    ((r.items) || []).forEach((it) => {
      const b = document.createElement("button"); b.className = "frow";
      const name = document.createElement("span"); name.textContent = (it.type === "dir" ? "[ ] " : "") + it.name;
      const sz = document.createElement("span"); sz.className = "sz"; sz.textContent = it.type === "file" ? it.size + " B" : "";
      b.append(name, sz);
      b.onclick = () => {
        const rel = (path === "." ? "" : path + "/") + it.name;
        if (it.type === "dir") { crumbs.push(it.name); listFiles(rel); }
        else viewFile(rel);
      };
      box.appendChild(b);
    });
    if (!(r.items || []).length) box.innerHTML = "<div class='mem'>Empty directory.</div>";
  } catch (e) { toast(e.message, "err"); }
}
async function viewFile(rel) {
  try {
    const r = await api("/api/file?path=" + encodeURIComponent(rel));
    $("fileList").style.display = "none"; $("fileView").style.display = "flex";
    $("fileView").dataset.file = "1";
    $("fileContent").textContent = r.content || r.error || "(empty)";
    if (r.truncated) $("fileContent").textContent += "\n...[truncated]";
    $("fileBack").onclick = () => { delete $("fileView").dataset.file; $("fileView").style.display = "none"; $("fileList").style.display = ""; };
  } catch (e) { toast(e.message, "err"); }
}
async function loadMem() {
  try {
    const r = await api("/api/memories");
    const box = $("memList"); box.innerHTML = "";
    (r.memories || []).forEach((m) => {
      const d = document.createElement("div"); d.className = "mem";
      const b = document.createElement("b"); b.textContent = m.key + ": ";
      const s = document.createElement("span"); s.textContent = m.value;
      d.append(b, s); box.appendChild(d);
    });
    if (!(r.memories || []).length) box.innerHTML = "<div class='mem'>No memories yet.</div>";
  } catch (e) { toast(e.message, "err"); }
}
$("memForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/memories", { method: "POST",
      body: JSON.stringify({ key: $("memKey").value, value: $("memVal").value, confirm: true }) });
    $("memKey").value = ""; $("memVal").value = ""; loadMem(); toast("Remembered.", "ok");
  } catch (err) { toast(err.message, "err"); }
};

/* ================= BYOK modal: provider grid, auto endpoint/model ================= */
function renderProvGrid() {
  const g = $("provGrid"); g.innerHTML = "";
  providers.forEach((p) => {
    const b = document.createElement("button");
    b.textContent = p.label + (p.env_has_key ? " [server key]" : "");
    b.className = p.id === selProvider ? "on" : "";
    b.onclick = () => {
      selProvider = p.id;
      localStorage.setItem("hermes.provider", p.id);
      if (p.id === "custom") { $("advBox").style.display = "block"; }
      syncAuto(); renderProvGrid(); refreshBadge();
    };
    g.appendChild(b);
  });
}
function syncAuto() {
  const m = meta(selProvider);
  $("roBase").textContent = byok().baseUrl || m.default_base_url || "(none)";
  $("roModel").textContent = byok().model || ((m.models || [])[0] || "(none)");
  const dl = $("modelList"); dl.innerHTML = "";
  (m.models || []).forEach((x) => { const o = document.createElement("option"); o.value = x; dl.appendChild(o); });
  refreshBadge();
}
async function loadProviders() {
  try {
    const r = await fetch("/api/providers").then((x) => x.json());
    providers = r.providers || [];
    renderProvGrid(); syncAuto();
  } catch (e) { /* offline */ }
}
function openByok() {
  $("pModel").value = byok().model; $("pBase").value = byok().baseUrl; $("pKey").value = byok().apiKey;
  $("advBox").style.display = (byok().model || byok().baseUrl || selProvider === "custom") ? "block" : "none";
  syncAuto(); renderProvGrid();
  $("byokModal").classList.add("open");
}
$("byokBtn").onclick = openByok;
modelBadge.onclick = openByok;
$("advToggle").onclick = () => { $("advBox").style.display = $("advBox").style.display === "block" ? "none" : "block"; };
$("keyShow").onclick = () => { $("pKey").type = $("pKey").type === "password" ? "text" : "password"; };
$("byokForget").onclick = () => {
  localStorage.removeItem("hermes.key"); $("pKey").value = ""; refreshBadge(); toast("Key forgotten in this browser.", "ok");
};
$("byokSave").onclick = () => {
  localStorage.setItem("hermes.provider", selProvider);
  localStorage.setItem("hermes.model", $("pModel").value.trim());
  localStorage.setItem("hermes.baseurl", $("pBase").value.trim());
  localStorage.setItem("hermes.key", $("pKey").value.trim());
  $("byokModal").classList.remove("open");
  refreshBadge(); syncAuto();
  toast("Saved: " + selProvider + " / " + effModel() + ". Only the key was yours to fill.", "ok");
};
$("byokModal").addEventListener("click", (e) => { if (e.target.id === "byokModal") $("byokModal").classList.remove("open"); });

/* ================= boot ================= */
refreshBadge();
Promise.all([loadProviders(), loadSessions()]).then(loadLog);
fetch("/api/providers").then(() => { if (!busy) statusDot.className = "live"; }).catch(() => {});
