// MocapSync 마스터 대시보드.
//
// ★ 폰 이름처럼 네트워크에서 온 글자는 전부 textContent 로 넣습니다 (innerHTML 금지).
//   서버의 Content-Security-Policy 가 두 번째 방어선입니다.
"use strict";

const $ = (id) => document.getElementById(id);

/** 요소 만들기. children 의 문자열은 글자 그대로 들어갑니다 (HTML 로 해석하지 않음). */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  // ★ 끝까지 펼칩니다. 한 단계만 펴면 [dt, dd] 쌍 목록이 "[object HTMLElement]" 글자로 찍힙니다.
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function replace(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

async function api(name, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-MocapSync": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch("/api/" + name, opt);
  let data = {};
  try { data = await r.json(); } catch { /* 빈 응답 */ }
  return { status: r.status, ok: r.ok && data.ok !== false, data };
}

// ── 표시 규칙 ────────────────────────────────────────────────────────────

const STATE = {
  remote_ready: ["대기 중", "green", "ready"],
  recording: ["녹화 중", "red", "rec"],
  stopping: ["정지 중", "yellow", "busy"],
  uploading: ["PC 로 보내는 중", "yellow", "busy"],
};
function stateLabel(c) {
  if (STATE[c.state]) return STATE[c.state];
  return ["원격 대기 아님", "gray", "off"];
}

const THERMAL = { nominal: "정상", fair: "조금 따뜻함", serious: "뜨거움", critical: "매우 뜨거움" };

/** 시계 오차 상한 색. sidecar.py 의 판정 기준과 같습니다 (2 / 3 / 8.333 ms). */
function uncClass(ms) {
  if (ms === null || ms === undefined) return "";
  if (ms <= 2) return "good";
  if (ms <= 3) return "warn";
  if (ms <= 8.333) return "bad";
  return "fatal";
}

function fmtDuration(s) {
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60), r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

/** S20260927-221904 → 9월 27일 22:19:04 */
function fmtSid(sid) {
  const m = /^S(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(sid || "");
  if (!m) return sid;
  return `${Number(m[2])}월 ${Number(m[3])}일 ${m[4]}:${m[5]}:${m[6]}`;
}

function mb(bytes) { return (bytes / 1048576).toFixed(1) + " MB"; }

// ── 조작 ─────────────────────────────────────────────────────────────────

let lastMessageAt = 0;
function say(text, kind) {
  const el = $("message");
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
  lastMessageAt = Date.now();
}

async function act(name, body, busyText) {
  if (busyText) say(busyText);
  try {
    const r = await api(name, body || {});
    if (!r.ok) say(r.data.message || "실패했습니다", "bad");
    else if (r.status !== 202) say(r.data.message || "", "good");
    return r;
  } catch (e) {
    say("마스터에 보내지 못했습니다: " + e, "bad");
    return { ok: false };
  }
}

$("btn-start").addEventListener("click", () => act("start", {}, "모든 폰에 1초 뒤 같은 순간부터 녹화하라고 보냅니다…"));
$("btn-stop").addEventListener("click", () => act("stop", {}, "정지를 보냈습니다. 영상이 PC 로 다 올라올 때까지 기다립니다…"));
$("quit").addEventListener("click", async () => {
  if (!confirm("마스터를 끌까요? 폰 연결이 모두 끊깁니다.")) return;
  const r = await act("quit", {});
  if (r.ok) { stopped = true; $("bye").hidden = false; }
});

// ── 그리기 ───────────────────────────────────────────────────────────────

function renderControl(s) {
  const dot = $("phase-dot");
  let text, sub = "", cls;
  if (s.phase === "recording") {
    text = "녹화 중 " + fmtDuration(s.recordingS || 0);
    sub = `${s.session} · 카메라 ${s.cameras.filter((c) => c.state === "recording").length}대`;
    cls = "rec";
  } else if (s.phase === "starting") {
    text = "시작하는 중"; sub = "시계를 다시 맞추고 예약 시각을 보내는 중입니다"; cls = "busy";
  } else if (s.phase === "stopping") {
    text = "정지 · 받는 중"; sub = "폰이 영상을 PC 로 보내고 있습니다"; cls = "busy";
  } else if (s.readyCount > 0) {
    text = `준비 완료 · ${s.readyCount}대`;
    sub = s.readyCount < 2 ? "3D 를 만들려면 2대 이상이 필요합니다" : "녹화 시작을 누르면 모든 폰이 같은 순간부터 찍습니다";
    cls = "ready";
  } else {
    text = "폰을 기다리는 중"; sub = "폰의 녹화 화면에서 'PC 원격 대기 켜기'를 누르세요"; cls = "idle";
  }
  $("phase-text").textContent = text;
  $("phase-sub").textContent = sub;
  dot.className = "dot " + cls;
  $("btn-start").disabled = !s.canStart;
  $("btn-stop").disabled = !s.canStop;

  // 서버가 알려 준 마지막 조작 결과 (내가 방금 보낸 문장보다 새것이면)
  const lm = s.lastMessage;
  if (lm && lm.at * 1000 > lastMessageAt) {
    say(lm.text.split("\n")[0], lm.ok ? "good" : "bad");
    lastMessageAt = lm.at * 1000;
  }
}

function camCard(c) {
  const [label, color, cls] = stateLabel(c);
  const unc = c.uncertaintyMs;
  const sync = c.syncPending ? "측정 중…" : (c.syncAgeS === null ? "아직 안 함" : `${Math.round(c.syncAgeS)}초 전`);
  const rows = [
    ["시계 오차 상한", unc === null ? "—" : `${unc.toFixed(3)} ms`, uncClass(unc)],
    ["마지막 시계 맞춤", sync, ""],
    ["배터리", c.battery === null ? "—" : `${Math.round(c.battery * 100)}%`,
      c.battery !== null && c.battery < 0.2 ? "bad" : ""],
    ["온도", THERMAL[c.thermal] || c.thermal || "—",
      c.thermal === "serious" || c.thermal === "critical" ? "bad" : ""],
  ];
  if (c.state === "recording" || c.frames) rows.push(["찍은 프레임", String(c.frames || 0), ""]);
  return h("article", { class: `panel cam ${cls}` },
    h("div", { class: "cam-head" },
      h("div", {}, h("div", { class: "cam-name" }, c.name || "이름 없음"),
        h("div", { class: "cam-id" }, `${(c.deviceId || "").slice(0, 12)} · ${c.model} · iOS ${c.os}`)),
      h("span", { class: `badge ${color}` }, label)),
    h("dl", { class: "kv" }, rows.map(([k, v, vc]) => [h("dt", {}, k), h("dd", { class: vc }, v)])),
    c.remote ? null : h("div", { class: "hint" }, "이 폰은 PC 에 붙어 있지만 원격 대기가 아닙니다. 녹화 화면에서 'PC 원격 대기 켜기'를 누르세요."),
    h("div", { class: "hint" }, `앱 ${c.app}`));
}

function renderCams(s) {
  $("cams-count").textContent = s.cameras.length ? `${s.cameras.length}대 연결 · 원격 대기 ${s.remoteCount}대` : "";
  if (!s.cameras.length) {
    replace($("cams"), h("div", { class: "panel empty" },
      h("div", {}, "아직 연결된 폰이 없습니다"),
      h("ol", {},
        h("li", {}, "폰과 이 PC 를 같은 WiFi 에 연결합니다"),
        h("li", {}, "MocapSync 앱 → 녹화 화면 → 상태가 초록색 '준비 완료'인지 봅니다"),
        h("li", {}, `'PC 원격 촬영' 칸에 주소 ${s.server.addr || "(위 주소)"} 를 넣습니다`),
        h("li", {}, "'PC 원격 대기 켜기'를 누릅니다"))));
    return;
  }
  replace($("cams"), s.cameras.map(camCard));
}

function renderUploads(s) {
  const ups = s.uploads || [];
  $("uploads-wrap").hidden = !ups.length;
  if (!ups.length) return;
  replace($("uploads"), ups.map((u) => {
    const pct = u.size ? Math.min(100, (100 * u.got) / u.size) : 100;
    const bar = h("div", { class: "bar" }, h("span", {}));
    bar.firstChild.style.width = pct.toFixed(1) + "%";
    return h("div", { class: "up-row" },
      h("div", { class: "row-between" },
        h("span", {}, `${u.camera} · ${u.file}`),
        h("span", { class: "muted" }, `${mb(u.got)} / ${mb(u.size)}  (${pct.toFixed(0)}%)`)),
      bar);
  }));
}

function issueList(issues, max) {
  const bad = issues.filter((i) => i.severity !== "info");
  const info = issues.filter((i) => i.severity === "info");
  const list = h("ul", { class: "issues" },
    bad.slice(0, max).map((i) => h("li", { class: i.severity },
      `${i.severity === "fatal" ? "사용 불가" : "주의"} · ${i.where}: ${i.message}`)));
  const more = info.length ? h("details", { class: "more" },
    h("summary", {}, `참고 ${info.length}건`),
    h("ul", { class: "issues" }, info.map((i) => h("li", { class: "info" }, `${i.where}: ${i.message}`)))) : null;
  return [bad.length ? list : null, more];
}

function renderResult(s) {
  const r = s.lastResult;
  $("result-wrap").hidden = !r;
  if (!r) return;
  const spread = r.spreadMs;
  const spreadEl = h("div", {},
    h("div", { class: "stat-label" }, "두 폰의 첫 프레임 시각 차이"),
    h("div", { class: "big-number " + (spread === null ? "" : r.sameFrame ? "good" : "bad") },
      spread === null ? "—" : `${spread.toFixed(1)} ms`),
    h("div", { class: "hint" }, spread === null ? "폰이 2대 이상일 때 보입니다"
      : r.sameFrame ? "한 프레임(16.7 ms) 안 — 같은 순간을 잡았습니다" : "한 프레임보다 큽니다 — 예약 시작을 확인하세요"));
  const got = r.cameras.length;
  const verdict = h("div", {},
    h("div", { class: "stat-label" }, "판정"),
    h("div", { class: "big-number " + (r.usable && !(r.missing || []).length ? "good" : "bad") },
      r.usable && !(r.missing || []).length ? "사용 가능" : "확인 필요"),
    h("div", { class: "hint" }, `${got}/${r.expected || got}대 받음` +
      ((r.missing || []).length ? ` · 못 받음: ${r.missing.join(", ")}` : "")));
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["폰", "프레임", "길이", "시계 오차", "ISO", "렌즈", "상태"].map((t) => h("th", {}, t)))),
    h("tbody", {}, r.cameras.map((c) => h("tr", {},
      h("td", {}, (c.deviceId || c.file || "").slice(0, 8)),
      h("td", { class: "num" }, c.frames ?? "—"),
      h("td", { class: "num" }, c.durationS !== undefined ? `${c.durationS}초` : "—"),
      h("td", { class: "num " + uncClass(c.uncertaintyMs) }, c.uncertaintyMs !== undefined ? `${c.uncertaintyMs} ms` : "—"),
      h("td", { class: "num " + (c.iso >= 2000 ? "bad" : "") }, c.iso ?? "—"),
      h("td", {}, c.lens || "—"),
      h("td", { class: c.usable ? "good" : "fatal" }, c.error || (c.usable ? "사용 가능" : "사용 불가"))))));
  const issues = r.cameras.flatMap((c) => c.issues || []).concat(r.issues || []);
  replace($("result"),
    h("div", { class: "row-between" }, h("h3", {}, fmtSid(r.sid)),
      h("span", {}, h("button", { class: "btn small", type: "button", onclick: () => act("open", { sid: r.sid, what: "uploads" }) }, "영상 폴더"),
        " ",
        h("button", { class: "btn small primary", type: "button", onclick: () => make3d(r.sid) }, "3D 만들기"))),
    h("div", { class: "result-top" }, spreadEl, verdict),
    table, issueList(issues, 8));
}

function renderJob(s) {
  const j = s.job;
  $("job-wrap").hidden = !j;
  if (!j) return;
  const steps = h("div", { class: "steps" });
  for (let i = 1; i <= (j.steps || 7); i++) {
    steps.append(h("span", { class: i < j.step || (j.state !== "running" && i <= j.step) ? "done" : i === j.step && j.state === "running" ? "now" : "" }));
  }
  const head = j.state === "running"
    ? `${fmtSid(j.sid)} · ${j.step}/${j.steps} ${j.stepText} · ${fmtDuration(j.elapsedS)}`
    : `${fmtSid(j.sid)} · ${j.verdict || ""} · ${fmtDuration(j.elapsedS)} 걸림`;
  const cls = j.state === "running" ? "" : j.state === "done" ? "good" : "bad";
  const pre = h("pre", {}, (j.log || []).join("\n"));
  replace($("job"), h("div", { class: "row-between" }, h("strong", { class: cls }, head),
    j.state !== "running" ? h("button", { class: "btn small", type: "button", onclick: () => act("open", { sid: j.sid, what: "project" }) }, "결과 폴더") : null),
    steps, pre);
  pre.scrollTop = pre.scrollHeight;
}

let jobWasRunning = false;
async function make3d(sid) {
  const r = await act("make3d", { sid }, `${fmtSid(sid)} 3D 만들기를 시작합니다…`);
  if (r.ok) say("3D 만들기를 시작했습니다. 아래 '3D 만들기' 칸에서 진행 상황을 볼 수 있습니다.", "good");
}

function resultBadge(res) {
  if (!res) return h("span", { class: "badge gray" }, "아직 안 만듦");
  if (res.exitCode === 0) return h("span", { class: "badge green" }, res.reprojPx ? `3D 완료 · ${res.reprojPx} px` : "3D 완료");
  if (res.exitCode === 10) return h("span", { class: "badge yellow" }, "3D 전까지 (캘리브레이션 필요)");
  return h("span", { class: "badge orange", title: res.verdict || "" }, res.verdict || "실패");
}

function renderSessions(list, jobRunning) {
  if (!list.length) {
    replace($("sessions"), h("div", { class: "empty" }, "아직 받은 촬영이 없습니다"));
    return;
  }
  replace($("sessions"), h("table", {},
    h("thead", {}, h("tr", {}, ["촬영", "폰", "길이", "첫 프레임 차이", "받은 파일", "3D", ""].map((t) => h("th", {}, t)))),
    h("tbody", {}, list.map((x) => {
      const dur = Math.min(...x.cameras.map((c) => c.durationS || 0));
      return h("tr", {},
        h("td", {}, h("div", {}, fmtSid(x.sid)), h("div", { class: "cam-id" }, x.sid)),
        h("td", { class: "num" }, `${x.cameras.length}대`),
        h("td", { class: "num" }, isFinite(dur) ? `${dur}초` : "—"),
        h("td", { class: "num " + (x.spreadMs === null ? "" : x.sameFrame ? "good" : "bad") },
          x.spreadMs === null ? "—" : `${x.spreadMs.toFixed(1)} ms`),
        h("td", { class: "num " + (x.partial ? "bad" : "") }, x.partial ? "받는 중" : `${x.sizeMB} MB`),
        h("td", {}, resultBadge(x.result)),
        h("td", { class: "actions" },
          h("button", { class: "btn small", type: "button", onclick: () => act("open", { sid: x.sid, what: "uploads" }) }, "영상"),
          x.result ? h("button", { class: "btn small", type: "button", onclick: () => act("open", { sid: x.sid, what: "project" }) }, "결과") : null,
          h("button", { class: "btn small primary", type: "button", disabled: jobRunning || x.partial || null, onclick: () => make3d(x.sid) },
            x.result ? "다시 만들기" : "3D 만들기")));
    }))));
}

// ── 주기적 갱신 ──────────────────────────────────────────────────────────

let stopped = false;
let sessionsAt = 0;
let lastState = null;

async function tick() {
  if (stopped) return;
  try {
    const r = await api("state");
    if (!r.ok) throw new Error(String(r.status));
    const s = r.data;
    lastState = s;
    $("offline").hidden = true;
    $("addr").textContent = s.server.addr || "WiFi 에 연결되지 않음";
    $("port").textContent = s.server.port;
    renderControl(s);
    renderCams(s);
    renderUploads(s);
    renderResult(s);
    renderJob(s);
    const logEl = $("log");
    const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
    logEl.textContent = (s.log || []).join("\n");
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;

    const running = !!(s.job && s.job.state === "running");
    const finished = jobWasRunning && !running;
    jobWasRunning = running;
    if (finished || Date.now() - sessionsAt > 5000 || s.phase === "stopping") {
      sessionsAt = Date.now();
      const ls = await api("sessions");
      if (ls.ok) renderSessions(ls.data.sessions || [], running);
    }
  } catch (e) {
    $("offline").hidden = false;
    $("btn-start").disabled = true;
    $("btn-stop").disabled = true;
  }
}

tick();
setInterval(tick, 1000);
