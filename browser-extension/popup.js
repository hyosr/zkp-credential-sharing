const DEFAULT_BASE = "http://127.0.0.1:8001";

/* ---------- helpers ---------- */
function normalizeBaseUrl(input) {
  const v = (input || "").trim().replace(/\/+$/, "");
  return v || DEFAULT_BASE;
}

function normalizeToHandoffUrl(baseUrl, input) {
  const v = (input || "").trim();
  if (!v) return null;
  if (v.startsWith("http://") || v.startsWith("https://")) return v;
  return `${baseUrl}/sharing/handoff/${encodeURIComponent(v)}`;
}

function setStatus(el, msg, kind = "") {
  if (!el) return;
  el.textContent = msg || "";
  el.classList.remove("ok", "err");
  if (kind) el.classList.add(kind);
}

function setLoading(btn, spinner, textEl, loadingText = "Connecting...", idleText = "Connect", isLoading = false) {
  if (btn) btn.disabled = !!isLoading;
  if (spinner) spinner.classList.toggle("hidden", !isLoading);
  if (textEl) textEl.textContent = isLoading ? loadingText : idleText;
}

/* ---------- DOM refs ---------- */
let baseUrlEl, jwtEl, statusEl;
let tokenEl, goBtn, clearBtn, spinner, goText, delayBetweenCookiesEl, delayAfterInjectEl;

let ownerRequestIdEl, ownerHandoffUrlEl, generateOwnerHandoffBtn, copyOwnerHandoffBtn;
let finalTokenEl, openByFinalTokenBtn;

let sessionJsonEl, captureJsonBtn, copySessionJsonBtn, injectJsonBtn;

/* ---------- core ---------- */
async function runHandoff() {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl?.value);
  const opts = {
    delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
    delayAfterInject: Number(delayAfterInjectEl?.value || 0),
  };

  if (!handoffUrl) return setStatus(statusEl, "Paste token/session_id or full handoff URL.", "err");

  setLoading(goBtn, spinner, goText, "Connecting...", "Connect", true);

  await chrome.storage.local.set({
    handoffUrl,
    baseUrl,
    jwt: (jwtEl?.value || "").trim(),
    ...opts,
  });

  chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {
    setLoading(goBtn, spinner, goText, "Connecting...", "Connect", false);
    if (chrome.runtime.lastError) return setStatus(statusEl, chrome.runtime.lastError.message, "err");
    if (!resp?.ok) return setStatus(statusEl, `Failed: ${resp?.error || "unknown"}`, "err");
    setStatus(statusEl, "Success. Opening connected profile…", "ok");
    setTimeout(() => window.close(), 350);
  });
}

async function captureCurrentSession() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab?.url) throw new Error("No active tab");

  const cookies = await chrome.cookies.getAll({ url: tab.url });

  const exec = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => {
      const ls = {};
      const ss = {};
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        ls[k] = localStorage.getItem(k);
      }
      for (let i = 0; i < sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        ss[k] = sessionStorage.getItem(k);
      }
      return {
        localStorage: JSON.stringify(ls),
        sessionStorage: JSON.stringify(ss),
        current_url: window.location.href,
        service_url: window.location.origin,
      };
    },
  });

  return {
    cookies,
    ...(exec?.[0]?.result || {}),
  };
}

/* ---------- owner handoff ---------- */
async function generateOwnerHandoff() {
  try {
    const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
    const jwt = (jwtEl?.value || "").trim();
    const requestId = (ownerRequestIdEl?.value || "").trim();

    if (!jwt) throw new Error("JWT required");
    if (!requestId) throw new Error("Request ID required");

    const capture = await captureCurrentSession();

    chrome.runtime.sendMessage(
      {
        type: "CREATE_OWNER_HANDOFF_WITH_REQUEST",
        baseUrl,
        jwt,
        requestId,
        capture,
      },
      (resp) => {
        if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
        if (!resp?.ok) return alert(resp?.error || "Failed");
        ownerHandoffUrlEl.value = resp.handoffUrl || resp.handoff_url || "";
        setStatus(statusEl, "Owner handoff URL generated ✅", "ok");
      }
    );
  } catch (e) {
    alert(e.message || String(e));
  }
}

async function copyOwnerHandoff() {
  const txt = (ownerHandoffUrlEl?.value || "").trim();
  if (!txt) return alert("No handoff URL");
  await navigator.clipboard.writeText(txt);
  alert("Copied ✅");
}

/* ---------- JSON copy/paste flow ---------- */
async function captureSessionJson() {
  try {
    const data = await captureCurrentSession();
    const payload = {
      service_url: data.service_url || data.current_url,
      current_url: data.current_url,
      cookies: data.cookies || [],
      localStorage: data.localStorage || "{}",
      sessionStorage: data.sessionStorage || "{}",
    };
    sessionJsonEl.value = JSON.stringify(payload, null, 2);
    setStatus(statusEl, "Session JSON captured ✅", "ok");
  } catch (e) {
    setStatus(statusEl, `Capture JSON failed: ${e.message || e}`, "err");
  }
}

async function copySessionJson() {
  const txt = (sessionJsonEl?.value || "").trim();
  if (!txt) return alert("No JSON to copy");
  await navigator.clipboard.writeText(txt);
  alert("Session JSON copied ✅");
}

async function injectFromJson() {
  try {
    const txt = (sessionJsonEl?.value || "").trim();
    if (!txt) return alert("Paste session JSON first");
    const session = JSON.parse(txt);

    const baseUrl = normalizeBaseUrl(baseUrlEl?.value);

    chrome.runtime.sendMessage(
      {
        type: "INJECT_FROM_JSON",
        baseUrl,
        session,
        opts: {
          delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
          delayAfterInject: Number(delayAfterInjectEl?.value || 0),
        },
      },
      (resp) => {
        if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
        if (!resp?.ok) return alert(resp?.error || "Inject failed");
        setStatus(statusEl, "Injected from JSON ✅", "ok");
      }
    );
  } catch (e) {
    alert("Invalid JSON: " + (e.message || e));
  }
}

/* ---------- final token ---------- */
async function openConnectedByFinalToken() {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const jwt = (jwtEl?.value || "").trim();
  const finalToken = (finalTokenEl?.value || "").trim();

  if (!jwt || !finalToken) return alert("JWT and final token are required");

  chrome.runtime.sendMessage(
    { type: "OPEN_CONNECTED_PROFILE_BY_TOKEN", baseUrl, jwt, finalToken, opts: {} },
    (resp) => {
      if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
      if (!resp?.ok) return alert(resp?.error || "Failed");
      alert("Connected profile opened ✅");
    }
  );
}

/* ---------- boot ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  baseUrlEl = document.getElementById("baseUrl");
  jwtEl = document.getElementById("jwt");
  statusEl = document.getElementById("status");

  tokenEl = document.getElementById("token");
  goBtn = document.getElementById("go");
  clearBtn = document.getElementById("clear");
  spinner = document.getElementById("spinner");
  goText = document.getElementById("goText");
  delayBetweenCookiesEl = document.getElementById("delayBetweenCookies");
  delayAfterInjectEl = document.getElementById("delayAfterInject");

  ownerRequestIdEl = document.getElementById("ownerRequestId");
  ownerHandoffUrlEl = document.getElementById("ownerHandoffUrl");
  generateOwnerHandoffBtn = document.getElementById("generateOwnerHandoff");
  copyOwnerHandoffBtn = document.getElementById("copyOwnerHandoff");

  sessionJsonEl = document.getElementById("sessionJson");
  captureJsonBtn = document.getElementById("captureJsonBtn");
  copySessionJsonBtn = document.getElementById("copySessionJsonBtn");
  injectJsonBtn = document.getElementById("injectJsonBtn");

  finalTokenEl = document.getElementById("finalToken");
  openByFinalTokenBtn = document.getElementById("openByFinalToken");

  const saved = await chrome.storage.local.get(["baseUrl", "jwt", "handoffUrl"]);
  baseUrlEl.value = saved.baseUrl || DEFAULT_BASE;
  if (saved.jwt) jwtEl.value = saved.jwt;

  goBtn?.addEventListener("click", runHandoff);
  clearBtn?.addEventListener("click", async () => {
    if (tokenEl) tokenEl.value = "";
    await chrome.storage.local.remove(["handoffUrl"]);
    setStatus(statusEl, "Cleared.", "ok");
  });

  generateOwnerHandoffBtn?.addEventListener("click", generateOwnerHandoff);
  copyOwnerHandoffBtn?.addEventListener("click", copyOwnerHandoff);

  captureJsonBtn?.addEventListener("click", captureSessionJson);
  copySessionJsonBtn?.addEventListener("click", copySessionJson);
  injectJsonBtn?.addEventListener("click", injectFromJson);

  openByFinalTokenBtn?.addEventListener("click", openConnectedByFinalToken);

  const autosave = async () => {
    await chrome.storage.local.set({
      baseUrl: normalizeBaseUrl(baseUrlEl?.value),
      jwt: (jwtEl?.value || "").trim(),
    });
  };
  baseUrlEl?.addEventListener("input", autosave);
  jwtEl?.addEventListener("input", autosave);
});








































// const DEFAULT_BASE = "http://127.0.0.1:8001";

// // ---------- helpers ----------
// function normalizeBaseUrl(input) {
//   const v = (input || "").trim().replace(/\/+$/, "");
//   return v || DEFAULT_BASE;
// }

// function normalizeToHandoffUrl(baseUrl, input) {
//   const v = (input || "").trim();
//   if (!v) return null;
//   if (v.startsWith("http://") || v.startsWith("https://")) return v;
//   return `${baseUrl}/sharing/handoff/${encodeURIComponent(v)}`;
// }

// function setStatus(el, msg, kind = "") {
//   if (!el) return;
//   el.textContent = msg || "";
//   el.classList.remove("ok", "err");
//   if (kind) el.classList.add(kind);
// }

// function setLoading(btn, spinner, goText, isLoading) {
//   if (btn) btn.disabled = !!isLoading;
//   if (spinner) spinner.classList.toggle("hidden", !isLoading);
//   if (goText) goText.textContent = isLoading ? "Connecting..." : "Connect";
// }

// // ---------- DOM refs ----------
// let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
// let delayBetweenCookiesEl, delayAfterInjectEl, jwtEl;
// let assistedTokenEl, assistedStartBtn;
// let pendingListEl, refreshPendingBtn, finishBtn;
// let ownerRequestIdEl, ownerHandoffUrlEl, generateOwnerHandoffBtn, copyOwnerHandoffBtn;
// let finalTokenEl, openByFinalTokenBtn;

// // ---------- existing handoff run ----------
// async function runHandoff() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl?.value);
//   const opts = {
//     delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
//     delayAfterInject: Number(delayAfterInjectEl?.value || 0),
//   };

//   if (!handoffUrl) return setStatus(statusEl, "Paste token/session_id or full handoff URL.", "err");

//   setLoading(goBtn, spinner, goText, true);
//   await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts, jwt: (jwtEl?.value || "").trim() });

//   chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {
//     setLoading(goBtn, spinner, goText, false);
//     if (chrome.runtime.lastError) return setStatus(statusEl, chrome.runtime.lastError.message, "err");
//     if (!resp?.ok) return setStatus(statusEl, `Failed: ${resp?.error || "unknown"}`, "err");
//     setStatus(statusEl, "Success. Opening connected profile…", "ok");
//     setTimeout(() => window.close(), 400);
//   });
// }

// // ---------- owner capture helpers ----------
// async function captureCurrentSession() {
//   const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
//   if (!tab?.id || !tab?.url) throw new Error("No active tab");

//   const cookies = await chrome.cookies.getAll({ url: tab.url });

//   const exec = await chrome.scripting.executeScript({
//     target: { tabId: tab.id },
//     func: () => {
//       const ls = {};
//       const ss = {};
//       for (let i = 0; i < localStorage.length; i++) {
//         const k = localStorage.key(i); ls[k] = localStorage.getItem(k);
//       }
//       for (let i = 0; i < sessionStorage.length; i++) {
//         const k = sessionStorage.key(i); ss[k] = sessionStorage.getItem(k);
//       }
//       return {
//         localStorage: JSON.stringify(ls),
//         sessionStorage: JSON.stringify(ss),
//         current_url: window.location.href,
//         service_url: window.location.origin,
//       };
//     },
//   });

//   return { cookies, ...(exec?.[0]?.result || {}) };
// }

// async function generateOwnerHandoff() {
//   try {
//     const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//     const jwt = (jwtEl?.value || "").trim();
//     const requestId = (ownerRequestIdEl?.value || "").trim();

//     if (!jwt) throw new Error("JWT required");
//     if (!requestId) throw new Error("Request ID required");

//     const capture = await captureCurrentSession();

//     chrome.runtime.sendMessage(
//       { type: "CREATE_OWNER_HANDOFF_WITH_REQUEST", baseUrl, jwt, requestId, capture },
//       // (resp) => {
//       //   if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//       //   if (!resp?.ok) return alert(resp?.error || "Failed");
//       //   ownerHandoffUrlEl.value = resp.handoffUrl || "";
//       //   setStatus(statusEl, "Owner handoff URL generated ✅", "ok");
//       // }



//       // Dans generateOwnerHandoff(), remplace seulement le callback (resp) => { ... } par:

//       (resp) => {
//         if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//         if (!resp?.ok) return alert(resp?.error || "Failed");

//         // ✅ accepte camelCase et snake_case
//         const url = (resp.handoffUrl || resp.handoff_url || "").trim();

//         ownerHandoffUrlEl.value = url;
//         if (!url) {
//         // aide debug si le backend répond mais sans champ attendu
//           console.warn("No handoff url in response:", resp);
//           setStatus(statusEl, "Generated but no handoff URL returned (check console).", "err");
//           return;
//       }

//         setStatus(statusEl, "Owner handoff URL generated ✅", "ok");
// }




//     );
//   } catch (e) {
//     alert(e.message || String(e));
//   }
// }

// async function copyOwnerHandoff() {
//   const txt = (ownerHandoffUrlEl?.value || "").trim();
//   if (!txt) return alert("No handoff URL");
//   await navigator.clipboard.writeText(txt);
//   alert("Copied ✅");
// }

// // ---------- final token open ----------
// async function openConnectedByFinalToken() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const jwt = (jwtEl?.value || "").trim();
//   const finalToken = (finalTokenEl?.value || "").trim();
//   if (!jwt || !finalToken) return alert("JWT and final token are required");

//   chrome.runtime.sendMessage(
//     { type: "OPEN_CONNECTED_PROFILE_BY_TOKEN", baseUrl, jwt, finalToken, opts: {} },
//     (resp) => {
//       if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//       if (!resp?.ok) return alert(resp?.error || "Failed");
//       alert("Connected profile opened ✅");
//     }
//   );
// }

// // ---------- boot ----------
// document.addEventListener("DOMContentLoaded", async () => {
//   baseUrlEl = document.getElementById("baseUrl");
//   tokenEl = document.getElementById("token");
//   goBtn = document.getElementById("go");
//   clearBtn = document.getElementById("clear");
//   statusEl = document.getElementById("status");
//   spinner = document.getElementById("spinner");
//   goText = document.getElementById("goText");
//   delayBetweenCookiesEl = document.getElementById("delayBetweenCookies");
//   delayAfterInjectEl = document.getElementById("delayAfterInject");
//   jwtEl = document.getElementById("jwt");

//   pendingListEl = document.getElementById("pendingList");
//   refreshPendingBtn = document.getElementById("refreshPending");
//   finishBtn = document.getElementById("finishLogin");

//   ownerRequestIdEl = document.getElementById("ownerRequestId");
//   ownerHandoffUrlEl = document.getElementById("ownerHandoffUrl");
//   generateOwnerHandoffBtn = document.getElementById("generateOwnerHandoff");
//   copyOwnerHandoffBtn = document.getElementById("copyOwnerHandoff");

//   finalTokenEl = document.getElementById("finalToken");
//   openByFinalTokenBtn = document.getElementById("openByFinalToken");

//   const saved = await chrome.storage.local.get(["baseUrl", "jwt", "handoffUrl"]);
//   baseUrlEl.value = saved.baseUrl || DEFAULT_BASE;
//   if (saved.jwt) jwtEl.value = saved.jwt;

//   goBtn?.addEventListener("click", runHandoff);
//   clearBtn?.addEventListener("click", async () => {
//     tokenEl.value = "";
//     await chrome.storage.local.remove(["handoffUrl"]);
//     setStatus(statusEl, "Cleared.", "ok");
//   });

//   generateOwnerHandoffBtn?.addEventListener("click", generateOwnerHandoff);
//   copyOwnerHandoffBtn?.addEventListener("click", copyOwnerHandoff);
//   openByFinalTokenBtn?.addEventListener("click", openConnectedByFinalToken);

//   const autosave = async () => {
//     await chrome.storage.local.set({
//       baseUrl: normalizeBaseUrl(baseUrlEl.value),
//       jwt: (jwtEl.value || "").trim(),
//     });
//   };
//   baseUrlEl?.addEventListener("input", autosave);
//   jwtEl?.addEventListener("input", autosave);


//   document.getElementById("captureJsonBtn")?.addEventListener("click", captureSessionJson);
//   document.getElementById("copySessionJsonBtn")?.addEventListener("click", copySessionJson);
//   document.getElementById("injectJsonBtn")?.addEventListener("click", injectFromJson);
// });








// async function captureSessionJson() {
//   const data = await captureCurrentSession(); // ta fonction existante
//   const payload = {
//     service_url: data.service_url || data.current_url,
//     current_url: data.current_url,
//     cookies: data.cookies || [],
//     localStorage: data.localStorage || "{}",
//     sessionStorage: data.sessionStorage || "{}",
//   };
//   document.getElementById("sessionJson").value = JSON.stringify(payload, null, 2);
// }

// async function copySessionJson() {
//   const txt = (document.getElementById("sessionJson").value || "").trim();
//   if (!txt) return alert("No JSON to copy");
//   await navigator.clipboard.writeText(txt);
//   alert("JSON copied ✅");
// }

// async function injectFromJson() {
//   try {
//     const txt = (document.getElementById("sessionJson").value || "").trim();
//     if (!txt) return alert("Paste JSON first");
//     const session = JSON.parse(txt);

//     const baseUrl = (document.getElementById("baseUrl").value || "http://127.0.0.1:8001").trim();

//     chrome.runtime.sendMessage(
//       { type: "INJECT_FROM_JSON", baseUrl, session, opts: {} },
//       (resp) => {
//         if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//         if (!resp?.ok) return alert(resp?.error || "Inject failed");
//         alert("Injected from JSON ✅");
//       }
//     );
//   } catch (e) {
//     alert("Invalid JSON: " + (e.message || e));
//   }
// }





