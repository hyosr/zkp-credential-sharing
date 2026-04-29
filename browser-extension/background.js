/* =====================================================================
 * ZKP Credential Sharing — background.js
 * Fix: cookies injected BEFORE navigation → site sees session on first load
 * ===================================================================== */

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function originFromUrl(u) {
  try { const x = new URL(u); return `${x.protocol}//${x.host}/`; } catch { return u; }
}
function hostFromUrl(u) {
  try { return new URL(u).hostname.toLowerCase(); } catch { return ""; }
}
function normalizeDomain(d) { return (d || "").toLowerCase().replace(/^\./, ""); }
function domainMatches(cd, host) {
  cd = normalizeDomain(cd); host = normalizeDomain(host);
  return cd === host || host.endsWith("." + cd) || cd.endsWith("." + host);
}
function normalizeSameSite(v) {
  const s = String(v || "").toLowerCase();
  if (s === "strict") return "strict";
  if (s === "none")   return "no_restriction";
  return "lax";
}
function safeParseJson(s) {
  if (!s) return null;
  if (typeof s === "object") return s;
  try { const o = JSON.parse(s); return typeof o === "object" ? o : null; } catch { return null; }
}
function waitForTabComplete(tabId, ms = 15000) {
  return new Promise((res) => {
    function done() { chrome.tabs.onUpdated.removeListener(cb); res(); }
    function cb(id, info) { if (id === tabId && info.status === "complete") done(); }
    chrome.tabs.onUpdated.addListener(cb);
    setTimeout(done, ms);
  });
}

/* ─── Cookie helpers ─────────────────────────────────────────────────── */
async function setCookie(originUrl, c, forceDomain) {
  const domain = forceDomain ?? c.domain;
  const d = {
    url:      originUrl,
    name:     c.name,
    value:    String(c.value ?? ""),
    path:     c.path || "/",
    httpOnly: !!c.httpOnly,
    secure:   !!c.secure,
    sameSite: normalizeSameSite(c.sameSite),
  };
  if (domain) d.domain = domain;
  if (typeof c.expires === "number" && c.expires > 0) d.expirationDate = c.expires;
  try { await chrome.cookies.set(d); }
  catch (e) { console.warn("[cookie]", c?.name, c?.domain, e?.message); }
}

async function injectCookies(cookies, serviceUrl) {
  if (!cookies?.length) return;
  const origin = originFromUrl(serviceUrl);
  const host   = hostFromUrl(serviceUrl);
  for (const c of cookies) {
    // Set with original domain
    if (!c.domain || domainMatches(c.domain, host)) {
      await setCookie(origin, c);
    }
    // Also set with leading dot for subdomain coverage (e.g. .facebook.com)
    if (c.domain && !c.domain.startsWith(".")) {
      await setCookie(origin, c, "." + c.domain);
    }
    await sleep(50);
  }
  console.log(`[cookies] injected ${cookies.length} for ${host}`);
}

/* ─── Storage injection (runs inside the page) ───────────────────────── */
async function injectStorage(tabId, ls, ss) {
  if (!ls && !ss) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (ls, ss) => {
        if (ls) for (const [k, v] of Object.entries(ls)) { try { localStorage.setItem(k, v); } catch {} }
        if (ss) for (const [k, v] of Object.entries(ss)) { try { sessionStorage.setItem(k, v); } catch {} }
        console.log("[zkp] storage injected ✓");
      },
      args: [ls, ss],
    });
  } catch (e) { console.warn("[storage]", e?.message); }
}

/* ─── Resolve the best URL to navigate to ───────────────────────────── */
function resolveFinalUrl(serviceUrl, currentUrl) {
  // If Playwright stopped on a login / 2FA / checkpoint page → go to root
  const loginRe = /\/(login|signin|sign-in|auth|accounts\/login|flow\/login|verify|checkpoint|challenge)/i;
  const googleRe = /accounts\.google\.com/i;
  let url = currentUrl || serviceUrl;
  try {
    const u = new URL(url);
    if (loginRe.test(u.pathname) || googleRe.test(u.hostname)) {
      // Go to site root instead
      url = `${new URL(serviceUrl).origin}/`;
      console.log("[url] login page detected → using root:", url);
    } else {
      // Strip query & hash noise
      u.search = "";
      u.hash = "";
      url = u.toString();
    }
  } catch { url = serviceUrl; }
  return url;
}

/* ═══════════════════════════════════════════════════════════════════════
 * CORE: doHandoff
 *
 * CORRECT ORDER:
 *   1. Fetch session data from backend
 *   2. Inject ALL cookies (before any tab opens — chrome.cookies is global)
 *   3. Open about:blank tab
 *   4. Navigate to final URL  ← site loads WITH cookies already set
 *   5. Wait for page load
 *   6. Inject localStorage / sessionStorage
 *   7. Reload once so the app reads storage
 *   8. Re-inject cookies (catches any that were overwritten by the site)
 * ═══════════════════════════════════════════════════════════════════════ */
async function doHandoff(handoffUrl, opts = {}) {
  console.log("[doHandoff] →", handoffUrl);

  // 1. Fetch
  let data;
  const tryFetch = async (url) => {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text().catch(() => "")}`);
    return r.json();
  };
  try {
    data = await tryFetch(handoffUrl);
  } catch (e) {
    const alt = handoffUrl.startsWith("http://")
      ? handoffUrl.replace("http://", "https://")
      : handoffUrl.replace("https://", "http://");
    console.warn("[doHandoff] retry with", alt);
    data = await tryFetch(alt);
  }

  const serviceUrl = data.service_url;
  if (!serviceUrl) throw new Error("handoff: missing service_url");

  const cookies  = data.cookies || [];
  const lsObj    = safeParseJson(data.localStorage);
  const ssObj    = safeParseJson(data.sessionStorage);
  const finalUrl = resolveFinalUrl(serviceUrl, data.current_url);

  console.log("[doHandoff] service:", serviceUrl);
  console.log("[doHandoff] final url:", finalUrl);
  console.log("[doHandoff] cookies:", cookies.length, "| ls:", lsObj ? Object.keys(lsObj).length : 0);

  // 2. Inject cookies BEFORE opening any tab
  await injectCookies(cookies, serviceUrl);
  await sleep(500); // give chrome time to commit cookies

  // 3. Open blank tab
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  await sleep(150);

  // 4. Navigate to final URL — cookies are already set, site loads authenticated
  await chrome.tabs.update(tab.id, { url: finalUrl });
  await waitForTabComplete(tab.id, 15000);

  // 5+6. Inject storage
  await injectStorage(tab.id, lsObj, ssObj);
  await sleep(300);

  // 7. Reload so SPA picks up localStorage (e.g. JWT token stored there)
  if (lsObj && Object.keys(lsObj).length > 0) {
    await chrome.tabs.reload(tab.id);
    await waitForTabComplete(tab.id, 10000);
  }

  // 8. Re-inject cookies post-reload (some sites clear/replace cookies on load)
  await injectCookies(cookies, serviceUrl);

  console.log("[doHandoff] done ✅");
}

/* ─── doHandoffFromData (paste-JSON flow) ────────────────────────────── */
async function doHandoffFromData(data, opts = {}) {
  const serviceUrl = data.service_url || data.current_url;
  if (!serviceUrl) throw new Error("missing service_url");
  const finalUrl = resolveFinalUrl(serviceUrl, data.current_url);
  const cookies  = data.cookies || [];
  const lsObj    = safeParseJson(data.localStorage);
  const ssObj    = safeParseJson(data.sessionStorage);

  await injectCookies(cookies, serviceUrl);
  await sleep(500);
  const tab = await chrome.tabs.create({ url: "about:blank", active: true });
  await sleep(150);
  await chrome.tabs.update(tab.id, { url: finalUrl });
  await waitForTabComplete(tab.id, 15000);
  await injectStorage(tab.id, lsObj, ssObj);
  await sleep(300);
  if (lsObj && Object.keys(lsObj).length > 0) {
    await chrome.tabs.reload(tab.id);
    await waitForTabComplete(tab.id, 10000);
  }
  await injectCookies(cookies, serviceUrl);
}

/* ─── API helpers ────────────────────────────────────────────────────── */
async function apiGet(base, path, jwt) {
  const r = await fetch(`${base}${path}`, { headers: { Authorization: `Bearer ${jwt}` } });
  if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
  return r.json();
}
async function apiPost(base, path, jwt, body) {
  const r = await fetch(`${base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`POST ${path} → ${r.status}: ${await r.text()}`);
  return r.json();
}

/* ─── Assisted flow ──────────────────────────────────────────────────── */
let assistedPollTimer = null;
async function assistedStartFlow(baseUrl, jwt, shareToken) {
  const { request_id } = await apiPost(baseUrl, "/sharing/assisted/request", jwt, { share_token: shareToken });
  await chrome.storage.local.set({ assistedRequestId: request_id });
  if (assistedPollTimer) clearInterval(assistedPollTimer);
  assistedPollTimer = setInterval(async () => {
    try {
      const st = await apiGet(baseUrl, `/sharing/assisted/${encodeURIComponent(request_id)}/status`, jwt);
      if (st.status === "completed" && st.handoff_url) {
        clearInterval(assistedPollTimer); assistedPollTimer = null;
        await doHandoff(`${baseUrl}${st.handoff_url}`);
        try { chrome.notifications.create({ type:"basic", iconUrl:"icon32.png", title:"Session ready", message:"Connected profile opened." }); } catch {}
      }
    } catch (e) { console.error("[assisted poll]", e?.message); }
  }, 2000);
  return { requestId: request_id };
}

/* ─── Owner polling ──────────────────────────────────────────────────── */
let ownerPollTimer = null;
const seenPending = new Set();
async function ownerStartPolling(baseUrl, jwt) {
  if (ownerPollTimer) return;
  ownerPollTimer = setInterval(async () => {
    try {
      const list = await apiGet(baseUrl, "/sharing/assisted/pending", jwt);
      for (const item of list) {
        if (seenPending.has(item.request_id)) continue;
        seenPending.add(item.request_id);
        try {
          chrome.notifications.create(`assist:${item.request_id}`, {
            type:"basic", iconUrl:"icon32.png",
            title:"Assisted login request",
            message:`Access request for: ${item.service_url}`, priority:2,
          });
        } catch {}
      }
    } catch (e) {
      if (String(e?.message).includes("401")) { clearInterval(ownerPollTimer); ownerPollTimer = null; }
    }
  }, 5000);
}

if (chrome.notifications?.onClicked) {
  chrome.notifications.onClicked.addListener(async (notifId) => {
    if (!notifId.startsWith("assist:")) return;
    const reqId = notifId.slice("assist:".length);
    const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl","jwt"]);
    if (!baseUrl || !jwt) return;
    try {
      const resp = await apiPost(baseUrl, `/sharing/assisted/${encodeURIComponent(reqId)}/approve`, jwt, {});
      if (!resp.assist_login_url) throw new Error("missing assist_login_url");
      await chrome.tabs.create({ url: resp.assist_login_url, active: true });
      await chrome.storage.local.set({ pendingCaptureRequestId: reqId });
      chrome.action.setBadgeText({ text: "!" });
    } catch (e) { console.error("[approve]", e); }
  });
}

/* ─── Session capture (owner side) ──────────────────────────────────── */
async function captureCurrentSession(tabId, serviceUrl, requestId) {
  const rawCookies = await chrome.cookies.getAll({ url: serviceUrl });
  const cookies = rawCookies.map((c) => ({
    name: c.name, value: c.value,
    domain: c.domain || new URL(serviceUrl).hostname,
    path: c.path || "/", expires: c.expirationDate || -1,
    httpOnly: !!c.httpOnly, secure: !!c.secure, sameSite: c.sameSite || "Lax",
  }));
  const [exec] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const ls = {}, ss = {};
      for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i);ls[k]=localStorage.getItem(k);}
      for (let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);ss[k]=sessionStorage.getItem(k);}
      return { ls, ss, url: location.href };
    }
  });
  const { ls, ss, url } = exec.result;
  const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl","jwt"]);
  if (!jwt) throw new Error("No JWT");
  const r = await fetch(`${baseUrl}/sharing/assisted/${requestId}/session`, {
    method: "POST",
    headers: { "Content-Type":"application/json", Authorization:`Bearer ${jwt}` },
    body: JSON.stringify({ cookies, localStorage:JSON.stringify(ls), sessionStorage:JSON.stringify(ss), current_url:url }),
  });
  if (!r.ok) throw new Error(`Submit session HTTP ${r.status}`);
  return r.json();
}

/* ─── Bridge URL intercept ───────────────────────────────────────────── */
function isBridgeUrl(url) {
  try { const u = new URL(url); return u.pathname === "/extension/connect" && u.searchParams.has("handoff"); }
  catch { return false; }
}
chrome.webNavigation.onCommitted.addListener(async (details) => {
  if (details.frameId !== 0 || !isBridgeUrl(details.url)) return;
  const handoffUrl = new URL(details.url).searchParams.get("handoff");
  if (!handoffUrl) return;
  chrome.tabs.remove(details.tabId, () => {});
  try { await doHandoff(handoffUrl); console.log("[bridge] ✓"); }
  catch (e) { console.error("[bridge]", e); }
});

/* ─── Message router ─────────────────────────────────────────────────── */
console.log("[BG] loaded");
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  console.log("[BG]", msg?.type);

  if (msg?.type === "RUN_HANDOFF") {
    (async () => {
      try {
        const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
        if (!url) throw new Error("Missing handoffUrl");
        await doHandoff(url, msg.opts || {});
        sendResponse({ ok: true });
      } catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }

  if (msg?.type === "INJECT_FROM_JSON") {
    (async () => {
      try { await doHandoffFromData(msg.session, msg.opts||{}); sendResponse({ ok:true }); }
      catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }

  if (msg?.type === "CAPTURE_SESSION") {
    (async () => {
      try {
        const { tabId, serviceUrl, requestId } = msg;
        if (!tabId||!serviceUrl||!requestId) throw new Error("Missing params");
        await captureCurrentSession(tabId, serviceUrl, requestId);
        await chrome.storage.local.remove(["pendingCaptureRequestId"]);
        chrome.action.setBadgeText({ text:"" });
        sendResponse({ ok:true });
      } catch (e) { sendResponse({ ok:false, error:e.message }); }
    })(); return true;
  }

  if (msg?.type === "START_OWNER_POLLING") {
    ownerStartPolling(msg.baseUrl, msg.jwt).catch(console.error);
    sendResponse({ ok:true }); return true;
  }

  if (msg?.type === "ASSISTED_START") {
    (async () => {
      try {
        const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
        const jwt     = msg.jwt     || (await chrome.storage.local.get(["jwt"])).jwt;
        if (!baseUrl||!jwt||!msg.shareToken) throw new Error("Missing params");
        await chrome.storage.local.set({ baseUrl, jwt });
        ownerStartPolling(baseUrl, jwt).catch(()=>{});
        const r = await assistedStartFlow(baseUrl, jwt, msg.shareToken);
        sendResponse({ ok:true, requestId:r?.requestId });
      } catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }

  if (msg?.type === "CREATE_OWNER_HANDOFF_WITH_REQUEST") {
    (async () => {
      try {
        const { baseUrl, jwt, requestId, capture } = msg;
        if (!baseUrl||!jwt||!requestId) throw new Error("Missing params");
        const res = await fetch(`${baseUrl}/sharing/owner-handoff/from-capture`, {
          method:"POST",
          headers:{"Content-Type":"application/json", Authorization:`Bearer ${jwt}`},
          body: JSON.stringify({ request_id:requestId, cookies:capture.cookies||[], localStorage:capture.localStorage||"{}", sessionStorage:capture.sessionStorage||"{}", current_url:capture.current_url||null, service_url:capture.service_url||null }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
        const out = await res.json();
        const handoffUrl = out.handoff_url ? `${baseUrl}${out.handoff_url}` : `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;
        sendResponse({ ok:true, handoffUrl, handoff_url:handoffUrl });
      } catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }

  if (msg?.type === "CREATE_HANDOFF_FROM_CAPTURE") {
    (async () => {
      try {
        const { baseUrl, jwt, capture } = msg;
        const r = await fetch(`${baseUrl}/sharing/create-handoff`, {
          method:"POST",
          headers:{"Content-Type":"application/json", Authorization:`Bearer ${jwt}`},
          body:JSON.stringify(capture),
        });
        if (!r.ok) throw new Error(await r.text());
        const out = await r.json();
        sendResponse({ ok:true, handoff_url: out.handoff_url||`${baseUrl}/sharing/handoff/${out.handoff_session_id}` });
      } catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }

  if (msg?.type === "OPEN_CONNECTED_PROFILE_BY_TOKEN") {
    (async () => {
      try {
        const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
        const jwt     = msg.jwt     || (await chrome.storage.local.get(["jwt"])).jwt;
        if (!baseUrl||!jwt||!msg.finalToken) throw new Error("Missing params");
        const r = await fetch(`${baseUrl}/sharing/final-capture/resolve/${encodeURIComponent(msg.finalToken)}`, { method:"POST", headers:{Authorization:`Bearer ${jwt}`} });
        if (!r.ok) throw new Error(`Resolve ${r.status}`);
        const resolved = await r.json();
        await doHandoff(`${baseUrl}${resolved.handoff_url}`, msg.opts||{});
        sendResponse({ ok:true, data:resolved });
      } catch (e) { sendResponse({ ok:false, error:String(e?.message||e) }); }
    })(); return true;
  }
});

/* ─── Auto-start ─────────────────────────────────────────────────────── */
(async () => {
  const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl","jwt"]);
  if (baseUrl && jwt) ownerStartPolling(baseUrl, jwt).catch(console.error);
})();









































//functional with root mee 
// /* =====================
//  * ZKP Credential Sharing - Extension Background (fixed)
//  * ========================= */

// function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
// function originFromUrl(u) { try { const x = new URL(u); return `${x.protocol}//${x.host}/`; } catch { return u; } }
// function hostFromUrl(u) { try { return new URL(u).hostname.toLowerCase(); } catch { return ""; } }
// function normalizeDomain(d) { return (d || "").toLowerCase().replace(/^\./, ""); }
// function domainMatches(cookieDomain, targetHost) {
//   const cd = normalizeDomain(cookieDomain), h = normalizeDomain(targetHost);
//   return cd === h || h.endsWith("." + cd) || cd.endsWith("." + h);
// }
// function waitForTabComplete(tabId, timeoutMs = 15000) {
//   return new Promise((resolve) => {
//     function done() { chrome.tabs.onUpdated.removeListener(onUpdated); resolve(); }
//     function onUpdated(id, info) { if (id === tabId && info.status === "complete") done(); }
//     chrome.tabs.onUpdated.addListener(onUpdated);
//     setTimeout(done, timeoutMs);
//   });
// }
// function safeParseJsonObject(s) {
//   if (!s) return null;
//   try { const o = JSON.parse(s); return o && typeof o === "object" ? o : null; } catch { return null; }
// }
// function normalizeSameSite(v) {
//   if (!v) return "lax";
//   const s = String(v).toLowerCase();
//   if (s === "strict") return "strict";
//   if (s === "none") return "no_restriction";
//   return "lax";
// }

// async function setOneCookie(serviceOriginUrl, c, forcedDomain = null) {
//   const domain = forcedDomain || c.domain;
//   const details = {
//     url: serviceOriginUrl, name: c.name, value: c.value,
//     path: c.path || "/", httpOnly: !!c.httpOnly, secure: !!c.secure,
//     sameSite: normalizeSameSite(c.sameSite),
//   };
//   if (domain) details.domain = domain;
//   if (typeof c.expires === "number" && c.expires > 0) details.expirationDate = c.expires;
//   try { return await chrome.cookies.set(details); } catch (e) { console.warn("cookie set failed:", c?.name, e?.message); }
// }

// async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
//   if (!localStorageObj && !sessionStorageObj) return;
//   try {
//     await chrome.scripting.executeScript({
//       target: { tabId },
//       func: (ls, ss) => {
//         try {
//           if (ls) for (const [k, v] of Object.entries(ls)) localStorage.setItem(k, v);
//           if (ss) for (const [k, v] of Object.entries(ss)) sessionStorage.setItem(k, v);
//         } catch (e) { console.error("Storage injection error:", e); }
//       },
//       args: [localStorageObj, sessionStorageObj],
//     });
//     await sleep(300);
//     await chrome.tabs.reload(tabId);
//   } catch (e) { console.warn("injectStorageAndReload failed:", e?.message); }
// }

// /* ─── CORE doHandoff ─────────────────────────────────────────────────────── */
// async function doHandoff(handoffUrl, opts = {}) {
//   console.log("[doHandoff] fetching:", handoffUrl);

//   let data;
//   // Try http first, fallback to https if it fails (handles localhost with SSL)
//   const tryFetch = async (url) => {
//     const res = await fetch(url, { method: "GET" });
//     if (!res.ok) { const b = await res.text().catch(() => ""); throw new Error(`HTTP ${res.status}: ${b}`); }
//     return res.json();
//   };

//   try {
//     data = await tryFetch(handoffUrl);
//   } catch (e) {
//     const alt = handoffUrl.startsWith("http://")
//       ? handoffUrl.replace("http://", "https://")
//       : handoffUrl.replace("https://", "http://");
//     console.warn("[doHandoff] primary fetch failed, trying:", alt, "-", e?.message);
//     data = await tryFetch(alt);
//   }

//   const serviceUrl = data.service_url;
//   if (!serviceUrl) throw new Error("handoff response missing service_url");

//   // Determine final URL to open (clean login paths)
//   let finalUrl = data.current_url || serviceUrl;
//   try {
//     const u = new URL(finalUrl);
//     u.search = ""; u.hash = "";
//     if (/\/(login|signin|auth|verify_otp|accounts)/i.test(u.pathname)) {
//       finalUrl = `${u.protocol}//${u.host}/`;
//     } else {
//       finalUrl = u.toString();
//     }
//   } catch { finalUrl = serviceUrl; }
//   console.log("[doHandoff] opening:", finalUrl);

//   const cookies = data.cookies || [];
//   const lsObj = safeParseJsonObject(data.localStorage);
//   const ssObj = safeParseJsonObject(data.sessionStorage);
//   const canonicalOrigin = originFromUrl(serviceUrl);
//   const canonicalHost = hostFromUrl(serviceUrl);

//   // 1. Inject cookies before opening tab
//   for (const c of cookies) {
//     if (!c.domain || domainMatches(c.domain, canonicalHost)) {
//       await setOneCookie(canonicalOrigin, c);
//       await sleep(80);
//     }
//   }
//   await sleep(500);

//   // 2. Open tab
//   const tab = await chrome.tabs.create({ url: finalUrl, active: true });
//   await waitForTabComplete(tab.id, 15000);

//   // 3. Inject storage + reload
//   await injectStorageAndReload(tab.id, lsObj, ssObj);
//   await waitForTabComplete(tab.id, 10000);

//   // 4. Re-inject cookies after reload (catches httpOnly)
//   for (const c of cookies) {
//     await setOneCookie(originFromUrl(finalUrl), c);
//     await sleep(60);
//   }
//   console.log("[doHandoff] done");
// }

// async function doHandoffFromData(data, opts = {}) {
//   const serviceUrl = data.service_url || data.current_url;
//   const currentUrl = data.current_url || serviceUrl;
//   if (!serviceUrl) throw new Error("session JSON missing service_url");
//   const cookies = data.cookies || [];
//   const lsObj = typeof data.localStorage === "string" ? safeParseJsonObject(data.localStorage) : (data.localStorage || null);
//   const ssObj = typeof data.sessionStorage === "string" ? safeParseJsonObject(data.sessionStorage) : (data.sessionStorage || null);
//   const canonicalOrigin = originFromUrl(serviceUrl);
//   for (const c of cookies) { try { await setOneCookie(canonicalOrigin, c); } catch {} await sleep(80); }
//   await sleep(500);
//   const tab = await chrome.tabs.create({ url: currentUrl, active: true });
//   await waitForTabComplete(tab.id, 15000);
//   await injectStorageAndReload(tab.id, lsObj, ssObj);
//   await waitForTabComplete(tab.id, 10000);
// }

// /* ─── API helpers ────────────────────────────────────────────────────────── */
// async function apiGetJson(baseUrl, path, jwt) {
//   const r = await fetch(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${jwt}` } });
//   if (!r.ok) throw new Error(`GET ${path} failed: ${r.status} ${await r.text()}`);
//   return r.json();
// }
// async function apiPostJson(baseUrl, path, jwt, body) {
//   const r = await fetch(`${baseUrl}${path}`, {
//     method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
//     body: JSON.stringify(body || {}),
//   });
//   if (!r.ok) throw new Error(`POST ${path} failed: ${r.status} ${await r.text()}`);
//   return r.json();
// }

// /* ─── Assisted flow ──────────────────────────────────────────────────────── */
// let assistedPollTimer = null;
// async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
//   const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { share_token: shareToken });
//   const requestId = created.request_id;
//   await chrome.storage.local.set({ assistedRequestId: requestId });
//   if (assistedPollTimer) clearInterval(assistedPollTimer);
//   assistedPollTimer = setInterval(async () => {
//     try {
//       const st = await apiGetJson(baseUrl, `/sharing/assisted/${encodeURIComponent(requestId)}/status`, jwtToken);
//       if (st.status === "completed" && st.handoff_url) {
//         clearInterval(assistedPollTimer); assistedPollTimer = null;
//         await doHandoff(`${baseUrl}${st.handoff_url}`, {});
//         if (chrome.notifications?.create)
//           chrome.notifications.create({ type: "basic", iconUrl: "icon32.png", title: "Assisted Share Complete", message: "Connected profile opened." });
//       }
//     } catch (err) { console.error("Assisted poll error:", err); }
//   }, 2000);
// }

// /* ─── Owner polling ──────────────────────────────────────────────────────── */
// let ownerPollTimer = null;
// const seenPending = new Set();
// async function ownerStartPolling(baseUrl, jwtToken) {
//   if (ownerPollTimer) return;
//   ownerPollTimer = setInterval(async () => {
//     try {
//       const pending = await apiGetJson(baseUrl, "/sharing/assisted/pending", jwtToken);
//       for (const item of pending) {
//         if (seenPending.has(item.request_id)) continue;
//         seenPending.add(item.request_id);
//         if (chrome.notifications?.create)
//           chrome.notifications.create(`assist:${item.request_id}`, { type: "basic", iconUrl: "icon32.png", title: "Assisted login request", message: `Access request for: ${item.service_url}`, priority: 2 });
//       }
//     } catch (e) {
//       if (String(e?.message || e).includes("401")) { clearInterval(ownerPollTimer); ownerPollTimer = null; }
//     }
//   }, 5000);
// }

// if (chrome.notifications?.onClicked) {
//   chrome.notifications.onClicked.addListener(async (notifId) => {
//     if (!notifId.startsWith("assist:")) return;
//     const requestId = notifId.slice("assist:".length);
//     const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//     if (!baseUrl || !jwt) return;
//     try {
//       const resp = await apiPostJson(baseUrl, `/sharing/assisted/${encodeURIComponent(requestId)}/approve`, jwt, {});
//       if (!resp.assist_login_url) throw new Error("missing assist_login_url");
//       await chrome.tabs.create({ url: resp.assist_login_url, active: true });
//       await chrome.storage.local.set({ pendingCaptureRequestId: requestId });
//       chrome.action.setBadgeText({ text: "!" });
//     } catch (e) { console.error("approve failed:", e); }
//   });
// }

// /* ─── Session capture ────────────────────────────────────────────────────── */
// async function captureCurrentSession(tabId, serviceUrl, requestId) {
//   const cookies = await chrome.cookies.getAll({ url: serviceUrl });
//   const validatedCookies = cookies.map((c) => ({
//     name: c.name, value: c.value, domain: c.domain || new URL(serviceUrl).hostname,
//     path: c.path || "/", expires: c.expirationDate || -1,
//     httpOnly: c.httpOnly || false, secure: c.secure || false, sameSite: c.sameSite || "Lax",
//   }));
//   const exec = await chrome.scripting.executeScript({
//     target: { tabId },
//     func: () => {
//       const ls = {}, ss = {};
//       for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); ls[k] = localStorage.getItem(k); }
//       for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); ss[k] = sessionStorage.getItem(k); }
//       return { localStorage: ls, sessionStorage: ss };
//     }
//   });
//   const { localStorage: ls, sessionStorage: ss } = exec[0].result;
//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//   if (!jwt) throw new Error("No JWT");
//   const r = await fetch(`${baseUrl}/sharing/assisted/${requestId}/session`, {
//     method: "POST",
//     headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
//     body: JSON.stringify({ cookies: validatedCookies, localStorage: JSON.stringify(ls), sessionStorage: JSON.stringify(ss), current_url: serviceUrl }),
//   });
//   if (!r.ok) throw new Error(`Submit session HTTP ${r.status}`);
//   return r.json();
// }

// /* ─── Bridge URL intercept ───────────────────────────────────────────────── */
// function isBridgeUrl(url) {
//   try { const u = new URL(url); return u.pathname === "/extension/connect" && u.searchParams.has("handoff"); }
//   catch { return false; }
// }
// chrome.webNavigation.onCommitted.addListener(async (details) => {
//   if (details.frameId !== 0 || !isBridgeUrl(details.url)) return;
//   const handoffUrl = new URL(details.url).searchParams.get("handoff");
//   if (!handoffUrl) return;
//   chrome.tabs.remove(details.tabId, () => { if (chrome.runtime.lastError) {} });
//   try { await doHandoff(handoffUrl, {}); console.log("[bridge] success"); }
//   catch (e) { console.error("[bridge] failed:", e); }
// });

// /* ─── Message router ─────────────────────────────────────────────────────── */
// console.log("[BG] loaded");
// chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
//   console.log("[BG] message:", msg?.type);

//   if (msg?.type === "RUN_HANDOFF") {
//     (async () => {
//       try {
//         const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
//         if (!url) throw new Error("Missing handoffUrl");
//         await doHandoff(url, msg.opts || {});
//         sendResponse({ ok: true });
//       } catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
//   if (msg?.type === "CAPTURE_SESSION") {
//     (async () => {
//       try {
//         const { tabId, serviceUrl, requestId } = msg;
//         if (!tabId || !serviceUrl || !requestId) throw new Error("Missing params");
//         await captureCurrentSession(tabId, serviceUrl, requestId);
//         await chrome.storage.local.remove(["pendingCaptureRequestId"]);
//         chrome.action.setBadgeText({ text: "" });
//         sendResponse({ ok: true });
//       } catch (e) { sendResponse({ ok: false, error: e.message }); }
//     })(); return true;
//   }
//   if (msg?.type === "START_OWNER_POLLING") {
//     ownerStartPolling(msg.baseUrl, msg.jwt).catch(console.error);
//     sendResponse({ ok: true }); return true;
//   }
//   if (msg?.type === "ASSISTED_START") {
//     (async () => {
//       try {
//         const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
//         const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
//         if (!baseUrl || !jwt || !msg.shareToken) throw new Error("Missing params");
//         await chrome.storage.local.set({ baseUrl, jwt });
//         ownerStartPolling(baseUrl, jwt).catch(() => {});
//         const r = await assistedStartFlow(baseUrl, jwt, msg.shareToken);
//         sendResponse({ ok: true, requestId: r?.requestId });
//       } catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
//   if (msg?.type === "INJECT_FROM_JSON") {
//     (async () => {
//       try { await doHandoffFromData(msg.session, msg.opts || {}); sendResponse({ ok: true }); }
//       catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
//   if (msg?.type === "CREATE_OWNER_HANDOFF_WITH_REQUEST") {
//     (async () => {
//       try {
//         const { baseUrl, jwt, requestId, capture } = msg;
//         if (!baseUrl || !jwt || !requestId) throw new Error("Missing params");
//         const res = await fetch(`${baseUrl}/sharing/owner-handoff/from-capture`, {
//           method: "POST",
//           headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
//           body: JSON.stringify({ request_id: requestId, cookies: capture.cookies || [], localStorage: capture.localStorage || "{}", sessionStorage: capture.sessionStorage || "{}", current_url: capture.current_url || null, service_url: capture.service_url || null }),
//         });
//         if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
//         const out = await res.json();
//         const handoffUrl = out.handoff_url ? `${baseUrl}${out.handoff_url}` : `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;
//         sendResponse({ ok: true, handoffUrl, handoff_url: handoffUrl });
//       } catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
//   if (msg?.type === "OPEN_CONNECTED_PROFILE_BY_TOKEN") {
//     (async () => {
//       try {
//         const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
//         const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
//         const finalToken = (msg.finalToken || "").trim();
//         if (!baseUrl || !jwt || !finalToken) throw new Error("Missing params");
//         const r = await fetch(`${baseUrl}/sharing/final-capture/resolve/${encodeURIComponent(finalToken)}`, { method: "POST", headers: { Authorization: `Bearer ${jwt}` } });
//         if (!r.ok) throw new Error(`Resolve failed: ${r.status}`);
//         const resolved = await r.json();
//         await doHandoff(`${baseUrl}${resolved.handoff_url}`, msg.opts || {});
//         sendResponse({ ok: true, data: resolved });
//       } catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
//   if (msg?.type === "CREATE_HANDOFF_FROM_CAPTURE") {
//     (async () => {
//       try {
//         const { baseUrl, jwt, capture } = msg;
//         const r = await fetch(`${baseUrl}/sharing/create-handoff`, {
//           method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` },
//           body: JSON.stringify(capture),
//         });
//         if (!r.ok) throw new Error(await r.text());
//         const out = await r.json();
//         sendResponse({ ok: true, handoff_url: out.handoff_url || `${baseUrl}/sharing/handoff/${out.handoff_session_id}` });
//       } catch (e) { sendResponse({ ok: false, error: String(e?.message || e) }); }
//     })(); return true;
//   }
// });

// (async () => {
//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//   if (baseUrl && jwt) ownerStartPolling(baseUrl, jwt).catch(console.error);
// })();

































// /* =====================//  * ZKP Credential Sharing - Extension Background
//  * Supports:
//  *  - RUN_HANDOFF: fetch /sharing/handoff/<id>, inject cookies + storages, open connected tab
//  *  - ASSISTED_START (recipient): POST /sharing/assisted/request, poll /status, open /handoff?handoff_token=...
//  *  - OWNER polling: GET /sharing/assisted/pending, show notification, click => POST /approve then open assist_login_url
//  * ========================= */

// /* ---------- helpers ---------- */
// function sleep(ms) {
//   return new Promise((resolve) => setTimeout(resolve, ms));
// }

// function originFromUrl(u) {
//   try {
//     const x = new URL(u);
//     return `${x.protocol}//${x.host}/`;
//   } catch {
//     return u;
//   }
// }

// function hostFromUrl(u) {
//   try {
//     return new URL(u).hostname.toLowerCase();
//   } catch {
//     return "";
//   }
// }

// function normalizeDomain(d) {
//   return (d || "").toLowerCase().replace(/^\./, "");
// }

// function domainMatches(cookieDomain, targetHost) {
//   const cd = normalizeDomain(cookieDomain);
//   const h = normalizeDomain(targetHost);
//   return cd === h || h.endsWith("." + cd) || cd.endsWith("." + h);
// }

// function waitForTabComplete(tabId, timeoutMs = 15000) {
//   return new Promise((resolve) => {
//     const t0 = Date.now();

//     function cleanup() {
//       chrome.tabs.onUpdated.removeListener(onUpdated);
//     }

//     function done() {
//       cleanup();
//       resolve();
//     }

//     function onUpdated(id, info) {
//       if (id === tabId && info.status === "complete") return done();
//       if (Date.now() - t0 > timeoutMs) return done();
//     }

//     chrome.tabs.onUpdated.addListener(onUpdated);
//     setTimeout(done, timeoutMs);
//   });
// }

// function safeParseJsonObject(s) {
//   if (!s) return null;
//   try {
//     const obj = JSON.parse(s);
//     return obj && typeof obj === "object" ? obj : null;
//   } catch {
//     return null;
//   }
// }

// function normalizeSameSite(v) {
//   if (!v) return "lax";
//   const s = String(v).toLowerCase();
//   if (s === "strict") return "strict";
//   if (s === "none") return "no_restriction";
//   return "lax";
// }

// function cookieSetUrlForDomain(serviceOriginUrl, cookieDomain) {
//   const u = new URL(serviceOriginUrl);
//   const d = (cookieDomain || u.hostname).replace(/^\./, "");
//   return `${u.protocol}//${d}/`;
// }

// async function setOneCookie(serviceOriginUrl, c, forcedDomain = null) {
//   const domain = forcedDomain || c.domain;

//   const details = {
//     url: cookieSetUrlForDomain(serviceOriginUrl, domain),
//     name: c.name,
//     value: c.value,
//     path: c.path || "/",
//     httpOnly: !!c.httpOnly,
//     secure: !!c.secure,
//     sameSite: normalizeSameSite(c.sameSite),
//   };

//   if (domain) details.domain = domain;

//   // Session cookie: expires = -1 or undefined => do not set expirationDate
//   if (typeof c.expires === "number" && c.expires > 0) {
//     details.expirationDate = c.expires;
//   }

//   return await chrome.cookies.set(details);
  
// }

// async function injectCookies(serviceOriginUrl, cookies, opts = {}) {
//   const delayBeforeMs = Number(opts.delayBeforeMs ?? 1000);
//   const delayBetweenMs = Number(opts.delayBetweenMs ?? 150);
//   const delayAfterMs = Number(opts.delayAfterMs ?? 600);

//   const list = Array.isArray(cookies) ? cookies : [];

//   await sleep(delayBeforeMs);

//   for (const c of list) {
//     try {
//       await setOneCookie(serviceOriginUrl, c);

//       // Optional: special case recolyse token cookie (if you still need it)
//       if (c.name === "token" && normalizeDomain(c.domain) === "recolyse.com") {
//         await setOneCookie(serviceOriginUrl, c, ".recolyse.com");
//       }
//     } catch (e) {
//       console.warn("Failed to set cookie:", c?.name, c?.domain, e);
//     }
//     await sleep(delayBetweenMs);
//   }

//   await sleep(delayAfterMs);
// }

// async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
//   await chrome.scripting.executeScript({
//     target: { tabId },
//     func: (ls, ss) => {
//       try {
//         if (ls && typeof ls === "object") {
//           for (const [k, v] of Object.entries(ls)) localStorage.setItem(k, v);
//         }
//         if (ss && typeof ss === "object") {
//           for (const [k, v] of Object.entries(ss)) sessionStorage.setItem(k, v);
//         }
//       } catch (e) {
//         console.error("Storage injection error:", e);
//       }
//     },
//     args: [localStorageObj, sessionStorageObj],
//   });

//   await sleep(500);
//   await chrome.tabs.reload(tabId);
// }

// async function fetchJson(url) {
//   const r = await fetch(url, { method: "GET" });
//   if (!r.ok) {
//     const t = await r.text();
//     throw new Error(`Fetch failed ${r.status}: ${t}`);
//   }
//   return await r.json();
// }

// async function apiGetJson(baseUrl, path, jwtToken) {
//   const r = await fetch(`${baseUrl}${path}`, {
//     method: "GET",
//     headers: { Authorization: `Bearer ${jwtToken}` },
//   });
//   if (!r.ok) throw new Error(`GET ${path} failed: ${r.status} ${await r.text()}`);
//   return await r.json();
// }

// async function apiPostJson(baseUrl, path, jwtToken, body) {
//   const r = await fetch(`${baseUrl}${path}`, {
//     method: "POST",
//     headers: {
//       "Content-Type": "application/json",
//       Authorization: `Bearer ${jwtToken}`,
//     },
//     body: JSON.stringify(body || {}),
//   });
//   if (!r.ok) throw new Error(`POST ${path} failed: ${r.status} ${await r.text()}`);
//   return await r.json();
// }








// // async function doHandoff(handoffUrl, opts = {}) {
// //   const data = await fetchJson(handoffUrl);
// //   const serviceUrl = data.service_url;
// //   let currentUrl = data.current_url || serviceUrl;

// //   // Nettoyer l'URL : enlever les paramètres de requête et le fragment
// //   try {
// //     const urlObj = new URL(currentUrl);
// //     // Supprimer les paramètres de requête (ex: ?action1=verify_otp)
// //     urlObj.search = '';
// //     // Supprimer le fragment (ex: #)
// //     urlObj.hash = '';
// //     // Si le chemin se termine par un mot‑clé de login/OTP, on remonte d'un niveau
// //     if (urlObj.pathname.endsWith('/verify_otp') || urlObj.pathname.includes('/auth')) {
// //       const parts = urlObj.pathname.split('/');
// //       parts.pop(); // enlève le dernier segment
// //       urlObj.pathname = parts.join('/') + '/';
// //     }
// //     currentUrl = urlObj.toString();
// //     console.log('URL nettoyée pour handoff:', currentUrl);
// //   } catch (e) {
// //     console.warn('Impossible de parser l’URL, utilisation brute', currentUrl);
// //   }

// //   const cookies = data.cookies || [];
// //   const localStorageObj = safeParseJsonObject(data.localStorage);
// //   const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

// //   const targetHost = hostFromUrl(currentUrl);
// //   const serviceOrigin = originFromUrl(currentUrl);

// //   for (const c of cookies) {
// //     if (domainMatches(c.domain, targetHost)) {
// //       await setOneCookie(serviceOrigin, c);
// //       await sleep(100);
// //     }
// //   }

// //   await sleep(1000); // délai pour laisser les cookies s’appliquer

// //   const tab = await chrome.tabs.create({ url: currentUrl, active: true });
// //   await waitForTabComplete(tab.id, 15000);

// //   await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
// //   await waitForTabComplete(tab.id, 15000);
// // }









// async function doHandoff(handoffUrl, opts = {}) {
//   const data = await fetchJson(handoffUrl);
//   const serviceUrl = data.service_url;
//   let currentUrl = data.current_url || serviceUrl;

//   if (!serviceUrl) throw new Error("handoff response missing service_url");

//   // Nettoyage léger URL finale (sans casser le domaine)
//   try {
//     const urlObj = new URL(currentUrl);
//     urlObj.search = "";
//     urlObj.hash = "";
//     if (urlObj.pathname.endsWith("/verify_otp") || urlObj.pathname.includes("/auth")) {
//       const parts = urlObj.pathname.split("/");
//       parts.pop();
//       urlObj.pathname = parts.join("/") + "/";
//     }
//     currentUrl = urlObj.toString();
//     console.log("URL nettoyée pour handoff:", currentUrl);
//   } catch (e) {
//     console.warn("Impossible de parser l’URL, utilisation brute", currentUrl);
//   }

//   const cookies = data.cookies || [];
//   const localStorageObj = safeParseJsonObject(data.localStorage);
//   const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

//   // ✅ FIX IMPORTANT:
//   // Injection des cookies selon le domaine canonique serviceUrl, pas currentUrl
//   const canonicalHost = hostFromUrl(serviceUrl);
//   const canonicalOrigin = originFromUrl(serviceUrl);

//   // 1) injecter cookies d'abord
//   for (const c of cookies) {
//     if (!c.domain || domainMatches(c.domain, canonicalHost)) {
//       try {
//         await setOneCookie(canonicalOrigin, c);
//       } catch (e) {
//         console.warn("Cookie inject failed:", c?.name, c?.domain, e);
//       }
//       await sleep(Number(opts.delayBetweenCookies ?? 100));
//     }
//   }

//   // petit délai pour stabiliser cookies
//   await sleep(800);

//   // 2) ouvrir d'abord serviceUrl (même origine)
//   // const tab = await chrome.tabs.create({ url: serviceUrl, active: true });
//   // await waitForTabComplete(tab.id, 15000);

//   // // 3) injecter storage puis reload
//   // await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
//   // await waitForTabComplete(tab.id, 15000);

//   // // 4) ensuite aller à l'URL finale capturée
//   // if (currentUrl && currentUrl !== serviceUrl) {
//   //   await chrome.tabs.update(tab.id, { url: currentUrl });
//   //   await waitForTabComplete(tab.id, 15000);
//   // }

//   // await sleep(Number(opts.delayAfterInject ?? 300));


//   // 1) ouvrir d'abord l'URL FINALE
//   const finalUrl = currentUrl || serviceUrl;
//   const tab = await chrome.tabs.create({ url: finalUrl, active: true });
//   await waitForTabComplete(tab.id, 15000);

//   // 2) injecter storage + cookies directement sur l'URL finale
//   await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
//   await waitForTabComplete(tab.id, 15000);

//   // 3) injecter à nouveau les cookies au cas où certains sont HTTPOnly
//   for (const c of cookies) {
//     try {
//       await setOneCookie(originFromUrl(finalUrl), c);
//       await sleep(Number(opts.delayBetweenCookies ?? 100));
//     } catch (e) {
//       console.warn("Cookie re-inject failed:", c?.name, c?.domain, e);
//   }
  
// }

//   await sleep(Number(opts.delayAfterInject ?? 300));
// }











// async function doHandoffFromData(data, opts = {}) {
//   const serviceUrl = data.service_url;
//   const currentUrl = data.current_url || serviceUrl;
//   if (!serviceUrl) throw new Error("session JSON missing service_url");

//   const cookies = data.cookies || [];
//   const localStorageObj = typeof data.localStorage === "string" ? JSON.parse(data.localStorage || "{}") : (data.localStorage || {});
//   const sessionStorageObj = typeof data.sessionStorage === "string" ? JSON.parse(data.sessionStorage || "{}") : (data.sessionStorage || {});

//   const tab = await chrome.tabs.create({ url: currentUrl, active: true });
//   await waitForTabComplete(tab.id, 15000);

//   // set cookies
//   for (const c of cookies) {
//     try { await setOneCookie(originFromUrl(serviceUrl), c); } catch {}
//     await sleep(Number(opts.delayBetweenCookies ?? 100));
//   }

//   await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
//   await waitForTabComplete(tab.id, 15000);
// }
























// /* ---------- Mode 2: Assisted (safe) ---------- */
// let assistedPollTimer = null;

// // async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
// //   // Create assisted request
// //   const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { share_token: shareToken });
// //   // const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { token: shareToken });
// //   const requestId = created.request_id;

// //   await chrome.storage.local.set({ assistedRequestId: requestId });

// //   // Poll status until completed
// //   if (assistedPollTimer) clearInterval(assistedPollTimer);

// //   assistedPollTimer = setInterval(async () => {
// //     try {
// //       const st = await apiGetJson(
// //         baseUrl,
// //         `/sharing/assisted/${encodeURIComponent(requestId)}/status`,
// //         jwtToken
// //       );

// //       if (st.status === "completed" && st.handoff_token) {
// //         clearInterval(assistedPollTimer);
// //         assistedPollTimer = null;

// //         const url = `${baseUrl}/handoff?handoff_token=${encodeURIComponent(st.handoff_token)}&redirect_to=/app`;
// //         await chrome.tabs.create({ url, active: true });

// //         if (chrome.notifications?.create) {
// //           chrome.notifications.create(`assisted:done:${requestId}`, {
// //             type: "basic",
// //             // iconUrl: "icon128.png",
// //             title: "Assisted access ready",
// //             message: "Opening delegated session…",
// //             priority: 1,
// //           });
// //         }
// //       } else if (st.status === "expired" || st.status === "rejected") {
// //         clearInterval(assistedPollTimer);
// //         assistedPollTimer = null;

// //         if (chrome.notifications?.create) {
// //           chrome.notifications.create(`assisted:fail:${requestId}`, {
// //             type: "basic",
// //             // iconUrl: "icon128.png",
// //             title: "Assisted access failed",
// //             message: `Status: ${st.status}`,
// //             priority: 2,
// //           });
// //         }
// //       }
// //     } catch (e) {
// //       console.warn("assisted poll error:", e);
// //     }
// //   }, 2000);

// //   return { requestId };
// // }



// async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
//   const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { share_token: shareToken });
//   const requestId = created.request_id;

//   await chrome.storage.local.set({ assistedRequestId: requestId });

//   if (assistedPollTimer) clearInterval(assistedPollTimer);

//   assistedPollTimer = setInterval(async () => {
//     try {
//       const st = await apiGetJson(
//         baseUrl,
//         `/sharing/assisted/${encodeURIComponent(requestId)}/status`,
//         jwtToken
//       );

//       if (st.status === "completed" && st.handoff_url) {  // ← CHANGÉ
//         clearInterval(assistedPollTimer);
//         assistedPollTimer = null;

//         // Construire l'URL complète du handoff
//         const fullHandoffUrl = `${baseUrl}${st.handoff_url}`;  // ← CHANGÉ
        
//         await doHandoff(fullHandoffUrl, {});  // Utiliser directement doHandoff

//         if (chrome.notifications?.create) {
//           chrome.notifications.create({
//             type: "basic",
//             iconUrl: "icon32.png",
//             title: "Assisted Share Complete",
//             message: "Connected profile opened.",
//           });
//         }
//       }
//     } catch (err) {
//       console.error("Assisted status poll error:", err);
//     }
//   }, 2000);
// }








// /* ---------- Owner: pending polling + notification ---------- */
// let ownerPollTimer = null;
// let seenPending = new Set();








// async function ownerStartPolling(baseUrl, jwtToken) {
//   if (ownerPollTimer) return;
//   ownerPollTimer = setInterval(async () => {
//     try {
//       const pending = await apiGetJson(baseUrl, "/sharing/assisted/pending", jwtToken);
//       for (const item of pending) {
//         const key = item.request_id;
//         if (seenPending.has(key)) continue;
//         seenPending.add(key);
//         if (chrome.notifications?.create) {
//           try {
//             await new Promise((resolve, reject) => {
//               chrome.notifications.create(`assist:${key}`, {
//                 type: "basic",
//                 title: "Assisted login request",
//                 message: `Recipient requests access to: ${item.service_url}`,
//                 priority: 2,
//               }, (notificationId) => {
//                 if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
//                 else resolve(notificationId);
//               });
//             });
//           } catch (notifErr) {
//             console.error("Notification creation failed:", notifErr);
//           }
//         }
//       }
//     } catch (e) {



//       const msg = String(e?.message || e);

//       // ✅ si JWT expiré/invalide: stop polling pour éviter un spam infini
//       if (msg.includes("failed: 401") || msg.includes(" 401 ") || msg.includes("Token invalide")) {
//         console.warn("[ownerStartPolling] JWT invalid/expired -> stopping polling. Please re-login / paste a new JWT.");
//         clearInterval(ownerPollTimer);
//         ownerPollTimer = null;

//         // optionnel: notifier l'utilisateur
//         if (chrome.notifications?.create) {
//           chrome.notifications.create("owner:jwt:expired", {
//           type: "basic",
//           title: "JWT expiré",
//           message: "Ton JWT backend est invalide/expiré. Ouvre l’extension et colle un nouveau JWT.",
//           priority: 2,
//       });
//     }
//       return;
//   }

//     console.warn("owner pending poll error:", e);




//       // console.warn("owner pending poll error:", e);




//     }
//   }, 5000);
// }








// // Notification click handler (owner)
// if (chrome.notifications?.onClicked) {
//   chrome.notifications.onClicked.addListener(async (notifId) => {
//     if (!notifId.startsWith("assist:")) return;
//     const requestId = notifId.slice("assist:".length);
//     const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//     if (!baseUrl || !jwt) return;
//     try {
//       const resp = await apiPostJson(baseUrl, `/sharing/assisted/${encodeURIComponent(requestId)}/approve`, jwt, {});
//       if (!resp.assist_login_url) throw new Error("approve response missing assist_login_url");
//       const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });
//       // Store pending capture info with service URL (the target site URL)
//       // await chrome.storage.local.set({
//       //   pendingCaptureRequestId: requestId,
//       //   pendingCaptureTabId: tab.id,
//       //   pendingServiceUrl: resp.assist_login_url  // URL du site cible
//       // });

//       await chrome.storage.local.set({
//         pendingCaptureRequestId: requestId
//       });



//       chrome.action.setBadgeText({ text: "!" });
//     } catch (e) {
//       console.error("approve failed:", e);
//     }
//   });
// }


















// async function captureCurrentSession(tabId, serviceUrl, requestId) {
//   console.log("[CAPTURE] Starting session capture...");
//   console.log("[CAPTURE] Tab ID:", tabId);
//   console.log("[CAPTURE] Service URL:", serviceUrl);
//   console.log("[CAPTURE] Request ID:", requestId);

//   let targetUrl = serviceUrl;
//   if (targetUrl.includes('/login') || targetUrl.includes('/auth')) {
//     const origin = new URL(targetUrl).origin;
//     targetUrl = origin + '/';
//     console.log("[CAPTURE] URL corrected to origin:", targetUrl);
//   }

//   // Get cookies for the domain
//   const cookies = await chrome.cookies.getAll({ url: serviceUrl });
//   console.log(`[CAPTURE] Found ${cookies.length} cookies for domain`);
  
//   for (const c of cookies) {
//     console.log(`[CAPTURE] Cookie: ${c.name} | Domain: ${c.domain} | Secure: ${c.secure} | SameSite: ${c.sameSite}`);
//   }

//   // NEW: Validate cookie structure before sending
//   const validatedCookies = cookies.map(c => ({
//     name: c.name,
//     value: c.value,
//     domain: c.domain || new URL(serviceUrl).hostname,
//     path: c.path || "/",
//     expires: c.expirationDate || -1,
//     httpOnly: c.httpOnly || false,
//     secure: c.secure || false,
//     sameSite: c.sameSite || "Lax",
//   }));

//   // Execute in page to get localStorage and sessionStorage
//   const injectionResult = await chrome.scripting.executeScript({
//     target: { tabId },
//     func: () => {
//       const ls = {};
//       for (let i = 0; i < localStorage.length; i++) {
//         const key = localStorage.key(i);
//         ls[key] = localStorage.getItem(key);
//       }
//       const ss = {};
//       for (let i = 0; i < sessionStorage.length; i++) {
//         const key = sessionStorage.key(i);
//         ss[key] = sessionStorage.getItem(key);
//       }
//       return { localStorage: ls, sessionStorage: ss };
//     }
//   });
  
//   const { localStorage, sessionStorage } = injectionResult[0].result;
//   console.log("[CAPTURE] localStorage keys:", Object.keys(localStorage).length);
//   console.log("[CAPTURE] sessionStorage keys:", Object.keys(sessionStorage).length);

//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
  
//   if (!jwt) {
//     throw new Error("No JWT token found in storage");
//   }

//   console.log("[CAPTURE] Submitting session to backend...");
//   const response = await fetch(`${baseUrl}/sharing/assisted/${requestId}/session`, {
//     method: "POST",
//     headers: {
//       "Content-Type": "application/json",
//       Authorization: `Bearer ${jwt}`,
//     },
//     body: JSON.stringify({
//       cookies: validatedCookies,
//       localStorage: JSON.stringify(localStorage),
//       sessionStorage: JSON.stringify(sessionStorage),
//       current_url: targetUrl
//     }),
//   });
  
//   if (!response.ok) {
//     const errorText = await response.text();
//     console.error("[CAPTURE] Submit failed:", response.status, errorText);
//     throw new Error(`Failed to submit session (HTTP ${response.status})`);
//   }
  
//   const result = await response.json();
//   console.log("[CAPTURE] ✅ Session submitted successfully");
//   console.log("[CAPTURE] Handoff session ID:", result.handoff_session_id);
  
//   return result;
// }










// async function resolveFinalCaptureToken(baseUrl, jwt, finalToken) {
//   const r = await fetch(`${baseUrl}/sharing/final-capture/resolve/${encodeURIComponent(finalToken)}`, {
//     method: "POST",
//     headers: {
//       "Content-Type": "application/json",
//       Authorization: `Bearer ${jwt}`,
//     },
//   });
//   if (!r.ok) throw new Error(`Resolve failed: ${r.status} ${await r.text()}`);
//   return await r.json();
// }



















// console.log("[BG] loaded");




// // ---------- Messages from popup ----------
// chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {


//   console.log("[BG] message:", msg?.type);

//   if (msg?.type === "CREATE_OWNER_HANDOFF_WITH_REQUEST") {
//     (async () => {
//       try {
//         const baseUrl = (msg.baseUrl || "").trim();
//         const jwt = (msg.jwt || "").trim();
//         const requestId = (msg.requestId || "").trim();
//         const capture = msg.capture || {};

//         if (!baseUrl) throw new Error("Missing baseUrl");
//         if (!jwt) throw new Error("Missing jwt");
//         if (!requestId) throw new Error("Missing requestId");

//         const url = `${baseUrl}/sharing/owner-handoff/from-capture`;
//         console.log("[BG] POST", url);

//         const res = await fetch(url, {
//           method: "POST",
//           headers: {
//             "Content-Type": "application/json",
//             "Authorization": `Bearer ${jwt}`,
//           },
//           body: JSON.stringify({
//             request_id: requestId,
//             cookies: capture.cookies || [],
//             localStorage: capture.localStorage || "{}",
//             sessionStorage: capture.sessionStorage || "{}",
//             current_url: capture.current_url || null,
//             service_url: capture.service_url || null,
//           }),
//         });

//         const text = await res.text();
//         console.log("[BG] status:", res.status, text);

//         if (!res.ok) throw new Error(`HTTP ${res.status}: ${text}`);
//         let out = {};
//         try { out = JSON.parse(text); } catch {}

//         const handoffUrl = out.handoff_url
//           ? `${baseUrl}${out.handoff_url}`
//           : `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;

//         // sendResponse({ ok: true, handoffUrl });

//         sendResponse({ ok: true, handoffUrl, handoff_url: handoffUrl });
//       } catch (e) {
//         console.error("[BG] handoff create failed:", e);
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//   return true;
//   }








//   if (msg?.type === "RUN_HANDOFF") {
//     (async () => {
//       try {
//         const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
//         if (!url) throw new Error("Missing handoffUrl");
//         await doHandoff(url, msg.opts || {});
//         sendResponse({ ok: true });
//       } catch (e) {
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//     return true;
//   }

//   if (msg.type === "CAPTURE_SESSION") {
//     (async () => {
//       try {
//         const { tabId, serviceUrl, requestId } = msg;
//         if (!tabId || !serviceUrl || !requestId) {
//           throw new Error("Missing capture parameters");
//         }
//         await captureCurrentSession(tabId, serviceUrl, requestId);
//         await chrome.storage.local.remove(["pendingCaptureRequestId", "pendingCaptureTabId", "pendingServiceUrl"]);
//         chrome.action.setBadgeText({ text: "" });
//         sendResponse({ ok: true });
//       } catch (e) {
//         console.error("Capture failed:", e);
//         sendResponse({ ok: false, error: e.message });
//       }
//     })();
//     return true;
//   }




//   if (msg.type === "START_OWNER_POLLING") {
//   ownerStartPolling(msg.baseUrl, msg.jwt).catch(console.error);
//   sendResponse({ ok: true });
//   return true;
// }










//   if (msg?.type === "ASSISTED_START") {
//     (async () => {
//       try {
//         const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
//         const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
//         if (!baseUrl || !jwt || !msg.shareToken) throw new Error("Missing parameters");
//         await chrome.storage.local.set({ baseUrl, jwt });
//         ownerStartPolling(baseUrl, jwt).catch(() => {});
//         const r = await assistedStartFlow(baseUrl, jwt, msg.shareToken);
//         sendResponse({ ok: true, requestId: r.requestId });
//       } catch (e) {
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//     return true;
//   }







//   if (msg?.type === "OPEN_CONNECTED_PROFILE_BY_TOKEN") {
//     (async () => {
//       try {
//         const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
//         const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
//         const finalToken = (msg.finalToken || "").trim();
//         if (!baseUrl || !jwt || !finalToken) throw new Error("Missing baseUrl/jwt/finalToken");

//         const resolved = await resolveFinalCaptureToken(baseUrl, jwt, finalToken);
//         const handoffUrl = `${baseUrl}${resolved.handoff_url}`;
//         await doHandoff(handoffUrl, msg.opts || {});
//         sendResponse({ ok: true, data: resolved });
//       } catch (e) {
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//     return true;
//   }






//   if (msg?.type === "CREATE_HANDOFF_FROM_CAPTURE") {
//   (async () => {
//     try {
//       const baseUrl = msg.baseUrl;
//       const jwt = msg.jwt;
//       const capture = msg.capture; // {cookies, localStorage, sessionStorage, current_url, service_url}

//       const r = await fetch(`${baseUrl}/sharing/create-handoff`, {
//         method: "POST",
//         headers: {
//           "Content-Type": "application/json",
//           "Authorization": `Bearer ${jwt}`,
//         },
//         body: JSON.stringify(capture),
//       });

//       if (!r.ok) throw new Error(await r.text());
//       const out = await r.json(); // expects {handoff_url: "..."} or {handoff_session_id: "..."}
//       const handoffUrl = out.handoff_url || `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;

//       sendResponse({ ok: true, handoff_url: handoffUrl });
//     } catch (e) {
//       sendResponse({ ok: false, error: String(e?.message || e) });
//     }
//   })();
//   return true;
// }






//   if (msg?.type === "INJECT_FROM_JSON") {
//     (async () => {
//       try {
//         await doHandoffFromData(msg.session, msg.opts || {});
//         sendResponse({ ok: true });
//       } catch (e) {
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//     return true;
// }



// });








// // Auto-start owner polling when extension loads
// (async () => {
//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//   if (baseUrl && jwt) {
//     ownerStartPolling(baseUrl, jwt).catch(console.error);
//   }
// })();













// /* ---------- Optional: Bridge URL support (handoff injection) ---------- */
// // function isBridgeUrl(url) {
// //   try {
// //     const u = new URL(url);
// //     return (
// //       u.origin === "https://localhost:8001" &&
// //       u.pathname === "/extension/connect" &&
// //       u.searchParams.get("handoff")
// //     );
// //   } catch {
// //     return false;
// //   }
// // }



// function isBridgeUrl(url) {
//   try {
//     const u = new URL(url);
//     // Match any URL that has pathname exactly "/extension/connect" and has a "handoff" parameter
//     return u.pathname === "/extension/connect" && u.searchParams.has("handoff");
//   } catch {
//     return false;
//   }
// }






// // chrome.webNavigation.onCommitted.addListener(async (details) => {
// //   if (details.frameId !== 0) return;
// //   if (!isBridgeUrl(details.url)) return;

// //   const u = new URL(details.url);
// //   const handoffUrl = u.searchParams.get("handoff");

// //   try {
// //     await chrome.storage.local.set({ handoffUrl });
// //     await doHandoff(handoffUrl, {});
// //     chrome.tabs.remove(details.tabId);
// //   } catch (e) {
// //     console.error("Bridge handoff failed:", e);
// //   }
// // });





// chrome.webNavigation.onCommitted.addListener(async (details) => {
//   if (details.frameId !== 0) return; // only main frame
//   if (!isBridgeUrl(details.url)) return;

//   const url = new URL(details.url);
//   const handoffUrl = url.searchParams.get("handoff");
//   if (!handoffUrl) return;

//   // Remove the bridge tab immediately (optional, but prevents showing the "Not Found" page)
//   chrome.tabs.remove(details.tabId, () => {
//     if (chrome.runtime.lastError) console.warn("Could not remove tab:", chrome.runtime.lastError);
//   });

//   // Execute handoff injection
//   try {
//     await doHandoff(handoffUrl, {});
//     console.log("Handoff successful via bridge URL");
//   } catch (e) {
//     console.error("Handoff failed:", e);
//   }
// });









// // Auto-start owner polling when extension loads
// (async () => {
//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//   if (baseUrl && jwt) {
//     console.log("Auto-starting owner polling");

//     ownerStartPolling(baseUrl, jwt).catch(console.error);
//   }else {
//     console.log("Missing baseUrl or jwt, polling not started");
//   }
  
// })();







































































