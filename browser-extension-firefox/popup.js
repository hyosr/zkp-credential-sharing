const ext = typeof browser !== "undefined" ? browser : chrome;

const DEFAULT_BASE = "http://127.0.0.1:8001";

// ---------- helpers ----------
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

function setLoading(btn, spinner, goText, isLoading) {
  if (btn) btn.disabled = !!isLoading;
  if (spinner) spinner.classList.toggle("hidden", !isLoading);
  if (goText) goText.textContent = isLoading ? "Connecting..." : "Connect";
}

// ---------- DOM refs ----------
let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
let delayBetweenCookiesEl, delayAfterInjectEl, jwtEl;
let ownerRequestIdEl, ownerHandoffUrlEl, generateOwnerHandoffBtn, copyOwnerHandoffBtn;
let finalTokenEl, openByFinalTokenBtn;
let sessionJsonEl, injectJsonBtn, captureJsonBtn, copySessionJsonBtn;

// ---------- existing handoff run ----------
async function runHandoff() {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl?.value);
  const opts = {
    delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
    delayAfterInject: Number(delayAfterInjectEl?.value || 0),
  };

  if (!handoffUrl) return setStatus(statusEl, "Paste token/session_id or full handoff URL.", "err");

  setLoading(goBtn, spinner, goText, true);
  await ext.storage.local.set({ handoffUrl, baseUrl, ...opts, jwt: (jwtEl?.value || "").trim() });

  ext.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {
    setLoading(goBtn, spinner, goText, false);
    if (ext.runtime.lastError) return setStatus(statusEl, ext.runtime.lastError.message, "err");
    if (!resp?.ok) return setStatus(statusEl, `Failed: ${resp?.error || "unknown"}`, "err");
    setStatus(statusEl, "Success. Opening connected profile…", "ok");
    setTimeout(() => window.close(), 400);
  });
}

// ---------- owner capture helpers ----------
async function captureCurrentSession() {
  const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !tab?.url) throw new Error("No active tab");

  const cookies = await ext.cookies.getAll({ url: tab.url });

  const exec = await ext.scripting.executeScript({
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

  return { cookies, ...(exec?.[0]?.result || {}) };
}

async function generateOwnerHandoff() {
  try {
    const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
    const jwt = (jwtEl?.value || "").trim();
    const requestId = (ownerRequestIdEl?.value || "").trim();

    if (!jwt) throw new Error("JWT required");
    if (!requestId) throw new Error("Request ID required");

    const capture = await captureCurrentSession();

    ext.runtime.sendMessage(
      { type: "CREATE_OWNER_HANDOFF_WITH_REQUEST", baseUrl, jwt, requestId, capture },
      (resp) => {
        if (ext.runtime.lastError) return alert(ext.runtime.lastError.message);
        if (!resp?.ok) return alert(resp?.error || "Failed");

        const url = (resp.handoffUrl || resp.handoff_url || "").trim();
        ownerHandoffUrlEl.value = url;

        if (!url) {
          console.warn("No handoff url in response:", resp);
          setStatus(statusEl, "Generated but no handoff URL returned (check console).", "err");
          return;
        }

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

// ---------- final token open ----------
async function openConnectedByFinalToken() {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const jwt = (jwtEl?.value || "").trim();
  const finalToken = (finalTokenEl?.value || "").trim();
  if (!jwt || !finalToken) return alert("JWT and final token are required");

  ext.runtime.sendMessage(
    { type: "OPEN_CONNECTED_PROFILE_BY_TOKEN", baseUrl, jwt, finalToken, opts: {} },
    (resp) => {
      if (ext.runtime.lastError) return alert(ext.runtime.lastError.message);
      if (!resp?.ok) return alert(resp?.error || "Failed");
      alert("Connected profile opened ✅");
    }
  );
}

// ---------- JSON capture/inject ----------
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
    alert(e.message || String(e));
  }
}

async function copySessionJson() {
  const txt = (sessionJsonEl?.value || "").trim();
  if (!txt) return alert("No JSON to copy");
  await navigator.clipboard.writeText(txt);
  alert("JSON copied ✅");
}

async function injectFromJson() {
  try {
    const txt = (sessionJsonEl?.value || "").trim();
    if (!txt) return alert("Paste session JSON first");
    const session = JSON.parse(txt);

    ext.runtime.sendMessage(
      {
        type: "FORCE_INJECT_JSON_CURRENT_TAB",
        session,
        opts: { delayBetweenCookies: 60 },
      },
      (resp) => {
        if (ext.runtime.lastError) return alert(ext.runtime.lastError.message);
        if (!resp?.ok) return alert(resp?.error || "Force inject failed");
        alert("Force injection done ✅\nNow manually type exact URL and click reload.");
      }
    );
  } catch (e) {
    alert("Invalid JSON: " + (e.message || e));
  }
}

// ---------- boot ----------
document.addEventListener("DOMContentLoaded", async () => {
  baseUrlEl = document.getElementById("baseUrl");
  tokenEl = document.getElementById("token");
  goBtn = document.getElementById("go");
  clearBtn = document.getElementById("clear");
  statusEl = document.getElementById("status");
  spinner = document.getElementById("spinner");
  goText = document.getElementById("goText");
  delayBetweenCookiesEl = document.getElementById("delayBetweenCookies");
  delayAfterInjectEl = document.getElementById("delayAfterInject");
  jwtEl = document.getElementById("jwt");

  ownerRequestIdEl = document.getElementById("ownerRequestId");
  ownerHandoffUrlEl = document.getElementById("ownerHandoffUrl");
  generateOwnerHandoffBtn = document.getElementById("generateOwnerHandoff");
  copyOwnerHandoffBtn = document.getElementById("copyOwnerHandoff");

  finalTokenEl = document.getElementById("finalToken");
  openByFinalTokenBtn = document.getElementById("openByFinalToken");

  sessionJsonEl = document.getElementById("sessionJson");
  injectJsonBtn = document.getElementById("injectJsonBtn");
  captureJsonBtn = document.getElementById("captureJsonBtn");
  copySessionJsonBtn = document.getElementById("copySessionJsonBtn");

  const saved = await ext.storage.local.get(["baseUrl", "jwt", "handoffUrl"]);
  if (baseUrlEl) baseUrlEl.value = saved.baseUrl || DEFAULT_BASE;
  if (saved.jwt && jwtEl) jwtEl.value = saved.jwt;

  goBtn?.addEventListener("click", runHandoff);
  clearBtn?.addEventListener("click", async () => {
    if (tokenEl) tokenEl.value = "";
    await ext.storage.local.remove(["handoffUrl"]);
    setStatus(statusEl, "Cleared.", "ok");
  });

  generateOwnerHandoffBtn?.addEventListener("click", generateOwnerHandoff);
  copyOwnerHandoffBtn?.addEventListener("click", copyOwnerHandoff);
  openByFinalTokenBtn?.addEventListener("click", openConnectedByFinalToken);

  captureJsonBtn?.addEventListener("click", captureSessionJson);
  copySessionJsonBtn?.addEventListener("click", copySessionJson);
  injectJsonBtn?.addEventListener("click", injectFromJson);

  const autosave = async () => {
    await ext.storage.local.set({
      baseUrl: normalizeBaseUrl(baseUrlEl?.value),
      jwt: (jwtEl?.value || "").trim(),
    });
  };

  baseUrlEl?.addEventListener("input", autosave);
  jwtEl?.addEventListener("input", autosave);
});






























// // const chrome = typeof browser !== "undefined" ? browser : chrome;

// const ext = typeof browser !== "undefined" ? browser : chrome;




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

// let sessionJsonEl, injectJsonBtn, captureJsonBtn, copySessionJsonBtn;


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






// async function captureSessionJson() {
//   try {
//     const data = await captureCurrentSession();
//     const payload = {
//       service_url: data.service_url || data.current_url,
//       current_url: data.current_url,
//       cookies: data.cookies || [],
//       localStorage: data.localStorage || "{}",
//       sessionStorage: data.sessionStorage || "{}",
//     };
//     sessionJsonEl.value = JSON.stringify(payload, null, 2);
//     setStatus(statusEl, "Session JSON captured ✅", "ok");
//   } catch (e) {
//     alert(e.message || String(e));
//   }
// }

// async function copySessionJson() {
//   const txt = (sessionJsonEl?.value || "").trim();
//   if (!txt) return alert("No JSON to copy");
//   await navigator.clipboard.writeText(txt);
//   alert("JSON copied ✅");
// }

// async function injectFromJson() {
//   try {
//     const txt = (sessionJsonEl?.value || "").trim();
//     if (!txt) return alert("Paste session JSON first");
//     const session = JSON.parse(txt);

//     ext.runtime.sendMessage(
//       {
//         type: "FORCE_INJECT_JSON_CURRENT_TAB",
//         session,
//         opts: { delayBetweenCookies: 60 }
//       },
//       (resp) => {
//         if (ext.runtime.lastError) return alert(ext.runtime.lastError.message);
//         if (!resp?.ok) return alert(resp?.error || "Force inject failed");
//         alert("Force injection done ✅\nNow manually type exact URL and click reload.");
//       }
//     );
//   } catch (e) {
//     alert("Invalid JSON: " + (e.message || e));
//   }
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


//   sessionJsonEl = document.getElementById("sessionJson");
//   injectJsonBtn = document.getElementById("injectJsonBtn");
//   captureJsonBtn = document.getElementById("captureJsonBtn");
//   copySessionJsonBtn = document.getElementById("copySessionJsonBtn");

//   captureJsonBtn?.addEventListener("click", captureSessionJson);
//   copySessionJsonBtn?.addEventListener("click", copySessionJson);
//   injectJsonBtn?.addEventListener("click", injectFromJson);
// });





























// // popup.js - ZKP Credential Sharing Extension
// // Improvements: single DOMContentLoaded, no duplicate listeners, less inline styles reliance,
// // better autosave, safer polling, clearer status messages.
// // NOTE: No buttons/IDs changed.

// const DEFAULT_BASE = "http://localhost:8001";

// // ---------- Helper functions ----------
// function normalizeBaseUrl(input) {
//   const v = (input || "").trim().replace(/\/+$/, "");
//   if (!v) return DEFAULT_BASE;
//   return v;
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

// function setLoading2(btn, spinner, textEl, isLoading, loadingText, idleText) {
//   if (btn) btn.disabled = !!isLoading;
//   if (spinner) spinner.classList.toggle("hidden", !isLoading);
//   if (textEl) textEl.textContent = isLoading ? loadingText : idleText;
// }

// function safeText(s, max = 5000) {
//   const v = String(s ?? "");
//   return v.length > max ? v.slice(0, max) + "…" : v;
// }

// async function tryJson(resp) {
//   try {
//     return await resp.json();
//   } catch {
//     return null;
//   }
// }

// function epochToLocal(epochSeconds) {
//   if (!epochSeconds) return "—";
//   try {
//     return new Date(Number(epochSeconds) * 1000).toLocaleString();
//   } catch {
//     return String(epochSeconds);
//   }
// }

// // ---------- DOM elements ----------
// let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
// let delayBetweenCookiesEl, delayAfterInjectEl;
// let jwtEl, modeHandoffEl, modeAssistedEl, assistedBox, assistedTokenEl, assistedStartBtn, assistedSpinner, assistedStartText;
// let finishBtn;

// // owner list
// let pendingListEl, refreshPendingBtn;

// // timers
// let pendingInterval = null;

// // ---------- Run handoff injection ----------
// async function runHandoff() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl?.value);

//   const opts = {
//     delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
//     delayAfterInject: Number(delayAfterInjectEl?.value || 0),
//   };

//   if (!handoffUrl) {
//     setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
//     return;
//   }

//   setLoading(goBtn, spinner, goText, true);
//   setStatus(statusEl, "");

//   await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts });
//   await chrome.storage.local.set({ jwt: (jwtEl?.value || "").trim() });

//   chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {
//     setLoading(goBtn, spinner, goText, false);

//     if (chrome.runtime.lastError) {
//       setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
//       return;
//     }
//     if (!resp || !resp.ok) {
//       setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
//       return;
//     }
//     setStatus(statusEl, "Success. Opening connected profile…", "ok");
//     setTimeout(() => window.close(), 450);
//   });
// }

// // ---------- Start assisted flow (recipient) ----------
// async function startAssisted() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const jwt = (jwtEl?.value || "").trim();
//   const token = (assistedTokenEl?.value || "").trim();

//   if (!jwt) {
//     setStatus(statusEl, "Paste Backend JWT first.", "err");
//     return;
//   }
//   if (!token) {
//     setStatus(statusEl, "Paste the share token to request owner approval.", "err");
//     return;
//   }

//   setStatus(statusEl, "");
//   setLoading2(
//     assistedStartBtn,
//     assistedSpinner,
//     assistedStartText,
//     true,
//     "Requesting...",
//     "Request owner approval"
//   );

//   await chrome.storage.local.set({ baseUrl, jwt, assistedToken: token });

//   chrome.runtime.sendMessage(
//     { type: "ASSISTED_START", baseUrl, jwt, shareToken: token },
//     (resp) => {
//       setLoading2(
//         assistedStartBtn,
//         assistedSpinner,
//         assistedStartText,
//         false,
//         "",
//         "Request owner approval"
//       );

//       if (chrome.runtime.lastError) {
//         setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
//         return;
//       }
//       if (!resp || !resp.ok) {
//         setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
//         return;
//       }
//       setStatus(statusEl, "Request sent. Waiting for owner approval��", "ok");
//     }
//   );
// }

// // ---------- Owner: finish manual login ----------
// async function finishOwnerCapture() {
//   const { baseUrl, jwt, pendingCaptureRequestId } = await chrome.storage.local.get([
//     "baseUrl",
//     "jwt",
//     "pendingCaptureRequestId",
//   ]);

//   if (!baseUrl || !jwt) {
//     alert("Missing baseUrl/jwt");
//     return;
//   }
//   if (!pendingCaptureRequestId) {
//     alert("No pending request id");
//     return;
//   }

//   const r = await fetch(
//     `${baseUrl}/sharing/assisted/${encodeURIComponent(pendingCaptureRequestId)}/finish`,
//     {
//       method: "POST",
//       headers: {
//         "Content-Type": "application/json",
//         Authorization: `Bearer ${jwt}`,
//       },
//     }
//   );

//   if (!r.ok) {
//     alert(`Finish failed: ${r.status} ${safeText(await r.text(), 1200)}`);
//     return;
//   }

//   // We don't rely on payload; keep for debug if you want
//   await tryJson(r);

//   alert("Session captured and sent to recipient!");
//   window.close();
// }

// // ---------- Update UI for handoff/assisted mode ----------
// function updateModeUI() {
//   if (!modeHandoffEl || !modeAssistedEl || !assistedBox) return;
//   assistedBox.style.display = modeAssistedEl.checked ? "block" : "none";
// }

// // ---------- Pending requests for owner ----------
// async function loadPendingRequests() {
//   if (!pendingListEl) return;

//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const jwt = (jwtEl?.value || "").trim();

//   if (!baseUrl || !jwt) {
//     pendingListEl.innerText = "Set Backend URL and JWT first.";
//     return;
//   }

//   try {
//     const response = await fetch(`${baseUrl}/sharing/assisted/pending`, {
//       headers: { Authorization: `Bearer ${jwt}` },
//     });

//     if (!response.ok) {
//       const body = safeText(await response.text(), 900);
//       throw new Error(`HTTP ${response.status}: ${body}`);
//     }

//     const pending = await response.json();

//     if (!Array.isArray(pending) || pending.length === 0) {
//       pendingListEl.innerText = "No pending requests.";
//       return;
//     }

//     // Note: some backends may not return recipient_email; handle gracefully.
//     pendingListEl.innerHTML = pending
//       .map((req) => {
//         const rid = req.request_id ?? req.id ?? "—";
//         const serviceUrl = req.service_url ?? "—";
//         const recipientEmail = req.recipient_email ?? "(hidden)";
//         const expires = epochToLocal(req.expires_at);

//         return `
//           <div class="reqItem">
//             <div><strong>${serviceUrl}</strong></div>
//             <div class="reqMeta">Request ID: ${rid}</div>
//             <div class="reqMeta">Recipient: ${recipientEmail}</div>
//             <div class="reqMeta">Expires: ${expires}</div>
//             <div class="reqActions">
//               <button class="btn approveBtn" data-id="${rid}" data-url="${encodeURIComponent(
//           serviceUrl
//         )}">Approve & assist</button>
//             </div>
//           </div>
//         `;
//       })
//       .join("");

//     pendingListEl.querySelectorAll(".approveBtn").forEach((btn) => {
//       btn.addEventListener("click", async () => {
//         const requestId = btn.dataset.id;
//         const serviceUrl = decodeURIComponent(btn.dataset.url || "");
//         await approveRequest(requestId, serviceUrl);
//       });
//     });
//   } catch (err) {
//     console.error(err);
//     pendingListEl.innerText = "Error loading requests.";
//   }
// }

// async function approveRequest(requestId, serviceUrl) {
//   const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
//   const jwt = (jwtEl?.value || "").trim();

//   if (!baseUrl || !jwt) {
//     alert("Missing Backend URL or JWT");
//     return;
//   }

//   try {
//     const response = await fetch(`${baseUrl}/sharing/assisted/${encodeURIComponent(requestId)}/approve`, {
//       method: "POST",
//       headers: {
//         "Content-Type": "application/json",
//         Authorization: `Bearer ${jwt}`,
//       },
//       body: JSON.stringify({}),
//     });

//     if (!response.ok) {
//       const body = safeText(await response.text(), 1200);
//       throw new Error(`HTTP ${response.status}: ${body}`);
//     }

//     const resp = (await tryJson(response)) || {};
//     if (!resp.assist_login_url) throw new Error("Missing assist_login_url");

//     const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });

//     await chrome.storage.local.set({
//       pendingCaptureRequestId: requestId,
//       pendingCaptureTabId: tab.id,
//       pendingServiceUrl: serviceUrl,
//     });

//     chrome.action.setBadgeText({ text: "!" });
//     setStatus(statusEl, "Approved. Complete login in the opened tab, then click 'I finished login'.", "ok");
//   } catch (err) {
//     console.error(err);
//     alert("Approval failed: " + err.message);
//   }
// }

// // ---------- Auto-refresh pending list ----------
// function startPendingPolling() {
//   if (pendingInterval) clearInterval(pendingInterval);
//   pendingInterval = setInterval(() => {
//     loadPendingRequests().catch(console.error);
//   }, 5000);
// }

// // ---------- Manual capture: capture and send session from current tab ----------
// async function captureAndSend() {
//   const requestId = (document.getElementById("captureRequestId")?.value || "").trim();
//   if (!requestId) {
//     alert("Please paste the Request ID");
//     return;
//   }

//   const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
//   if (!tab || !tab.url) {
//     alert("No active tab found. Make sure you are on the logged‑in page.");
//     return;
//   }

//   const serviceUrl = tab.url;

//   chrome.runtime.sendMessage(
//     { type: "CAPTURE_SESSION", tabId: tab.id, serviceUrl, requestId },
//     (response) => {
//       if (chrome.runtime.lastError) {
//         alert("Error: " + chrome.runtime.lastError.message);
//         return;
//       }
//       if (response && response.ok) {
//         alert("✅ Session captured and sent to recipient!");
//       } else {
//         alert("❌ Capture failed: " + (response?.error || "unknown"));
//       }
//     }
//   );
// }









// async function createOwnerHandoffAndShowUrl() {
//   const baseUrl = document.getElementById("baseUrl").value.trim();
//   const jwt = document.getElementById("jwt").value.trim();

//   // use your existing capture function
//   const capture = await captureCurrentSession(); // already implemented by you

//   chrome.runtime.sendMessage({
//     type: "CREATE_HANDOFF_FROM_CAPTURE",
//     baseUrl,
//     jwt,
//     capture
//   }, (resp) => {
//     if (!resp?.ok) return alert(resp?.error || "Failed");
//     document.getElementById("ownerHandoffUrl").value = resp.handoff_url; // copy/paste to dashboard
//   });
// }






// async function captureCurrentSession() {
//   // Reuse your existing implementation if already present.
//   // Must return: { cookies, localStorage, sessionStorage, current_url, service_url }
//   const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
//   if (!tab || !tab.url) throw new Error("No active tab");

//   const cookies = await chrome.cookies.getAll({ url: tab.url });

//   const [{ result: storages }] = await chrome.scripting.executeScript({
//     target: { tabId: tab.id },
//     func: () => ({
//       localStorage: JSON.stringify(Object.fromEntries(Object.entries(localStorage))),
//       sessionStorage: JSON.stringify(Object.fromEntries(Object.entries(sessionStorage))),
//       current_url: window.location.href,
//     }),
//   });

//   return {
//     cookies,
//     localStorage: storages.localStorage || "{}",
//     sessionStorage: storages.sessionStorage || "{}",
//     current_url: storages.current_url || tab.url,
//     service_url: tab.url,
//   };
// }

// async function generateOwnerHandoff() {
//   try {
//     const baseUrl = (document.getElementById("baseUrl")?.value || "http://localhost:8001").trim();
//     const jwt = (document.getElementById("jwt")?.value || "").trim();
//     if (!jwt) throw new Error("JWT required");

//     const capture = await captureCurrentSession();

//     chrome.runtime.sendMessage(
//       { type: "CREATE_HANDOFF_FROM_CAPTURE", baseUrl, jwt, capture },
//       (resp) => {
//         if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//         if (!resp?.ok) return alert(resp?.error || "Failed to generate handoff URL");
//         document.getElementById("ownerHandoffUrl").value = resp.handoff_url || "";
//       }
//     );
//   } catch (e) {
//     alert(e.message || String(e));
//   }
// }

// async function copyOwnerHandoffUrl() {
//   const el = document.getElementById("ownerHandoffUrl");
//   const txt = (el?.value || "").trim();
//   if (!txt) return alert("No handoff URL to copy");
//   await navigator.clipboard.writeText(txt);
//   alert("Copied ✅");
// }

// document.getElementById("generateOwnerHandoff")?.addEventListener("click", generateOwnerHandoff);
// document.getElementById("copyOwnerHandoff")?.addEventListener("click", copyOwnerHandoffUrl);









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
//         service_url: window.location.origin
//       };
//     }
//   });

//   const result = exec?.[0]?.result || {};
//   return { cookies, ...result };
// }

// async function generateOwnerHandoff() {
//   try {
//     const baseUrl = (document.getElementById("baseUrl")?.value || "http://localhost:8001").trim();
//     const jwt = (document.getElementById("jwt")?.value || "").trim();
//     const requestId = (document.getElementById("ownerRequestId")?.value || "").trim();

//     if (!jwt) throw new Error("JWT required");
//     if (!requestId) throw new Error("Request ID required");

//     const capture = await captureCurrentSession();

//     chrome.runtime.sendMessage(
//       { type: "CREATE_OWNER_HANDOFF_WITH_REQUEST", baseUrl, jwt, requestId, capture },
//       (resp) => {
//         if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//         if (!resp?.ok) return alert(resp?.error || "Failed");
//         document.getElementById("ownerHandoffUrl").value = resp.handoffUrl || "";
//       }
//     );
//   } catch (e) {
//     alert(e.message || String(e));
//   }
// }

// // document.getElementById("generateOwnerHandoff")?.addEventListener("click", generateOwnerHandoff);







// async function copyOwnerHandoff() {
//   const txt = (document.getElementById("ownerHandoffUrl").value || "").trim();
//   if (!txt) return alert("No handoff URL");
//   await navigator.clipboard.writeText(txt);
//   alert("Copied ✅");
// }

// // document.getElementById("generateOwnerHandoff")?.addEventListener("click", generateOwnerHandoff);
// document.getElementById("copyOwnerHandoff")?.addEventListener("click", copyOwnerHandoff);



















// async function openConnectedByFinalToken() {
//   const baseUrl = (document.getElementById("baseUrl").value || "http://localhost:8001").trim();
//   const jwt = (document.getElementById("jwt").value || "").trim();
//   const finalToken = (document.getElementById("finalToken").value || "").trim();

//   if (!jwt || !finalToken) return alert("JWT and final token are required");

//   // chrome.runtime.sendMessage({
//   //   type: "OPEN_CONNECTED_PROFILE_BY_TOKEN",
//   //   baseUrl,
//   //   jwt,
//   //   finalToken,
//   //   opts: {}
//   // }, (resp) => {
//   //   if (chrome.runtime.lastError) return alert(chrome.runtime.lastError.message);
//   //   if (!resp?.ok) return alert(resp?.error || "Failed");
//   //   alert("Connected profile opened ✅");
//   // });




//   chrome.runtime.sendMessage(payload, (resp) => {
//   if (chrome.runtime.lastError) {
//     alert("runtime error: " + chrome.runtime.lastError.message);
//     return;
//   }
//   if (!resp) {
//     alert("No response from background");
//     return;
//   }
//   if (!resp.ok) {
//     alert(resp.error || "Failed");
//     return;
//   }
//   document.getElementById("ownerHandoffUrl").value = resp.handoffUrl || "";
// });




// }

// document.getElementById("openByFinalToken")?.addEventListener("click", openConnectedByFinalToken);










// // ---------- DOMContentLoaded ----------
// document.addEventListener("DOMContentLoaded", async () => {
//   // Get elements
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
//   modeHandoffEl = document.getElementById("modeHandoff");
//   modeAssistedEl = document.getElementById("modeAssisted");

//   assistedBox = document.getElementById("assistedBox");
//   assistedTokenEl = document.getElementById("assistedToken");
//   assistedStartBtn = document.getElementById("assistedStart");
//   assistedSpinner = document.getElementById("assistedSpinner");
//   assistedStartText = document.getElementById("assistedStartText");
//   finishBtn = document.getElementById("finishLogin");

//   pendingListEl = document.getElementById("pendingList");
//   refreshPendingBtn = document.getElementById("refreshPending");

//   const captureBtn = document.getElementById("captureBtn");

//   // Load saved settings
//   const saved = await chrome.storage.local.get([
//     "handoffUrl",
//     "baseUrl",
//     "delayBetweenCookies",
//     "delayAfterInject",
//     "jwt",
//     "assistedToken",
//   ]);

//   if (saved.baseUrl) baseUrlEl.value = saved.baseUrl;
//   if (saved.delayBetweenCookies != null) delayBetweenCookiesEl.value = saved.delayBetweenCookies;
//   if (saved.delayAfterInject != null) delayAfterInjectEl.value = saved.delayAfterInject;
//   if (saved.jwt) jwtEl.value = saved.jwt;
//   if (saved.assistedToken) assistedTokenEl.value = saved.assistedToken;

//   if (saved.handoffUrl) {
//     const m = String(saved.handoffUrl).match(/\/sharing\/handoff\/([^/?#]+)/);
//     tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
//   }

//   // Core events (buttons unchanged)
//   goBtn?.addEventListener("click", runHandoff);
//   clearBtn?.addEventListener("click", async () => {
//     tokenEl.value = "";
//     await chrome.storage.local.remove(["handoffUrl"]);
//     setStatus(statusEl, "Cleared saved token.", "ok");
//   });

//   tokenEl?.addEventListener("keydown", (e) => {
//     if (e.key === "Enter" && !e.shiftKey) {
//       e.preventDefault();
//       runHandoff();
//     }
//   });

//   assistedStartBtn?.addEventListener("click", startAssisted);
//   finishBtn?.addEventListener("click", finishOwnerCapture);
//   captureBtn?.addEventListener("click", captureAndSend);

//   refreshPendingBtn?.addEventListener("click", () => loadPendingRequests());

//   // mode UI
//   if (modeHandoffEl && modeAssistedEl) {
//     modeHandoffEl.addEventListener("change", updateModeUI);
//     modeAssistedEl.addEventListener("change", updateModeUI);
//     updateModeUI();
//   }

//   // Autosave (use input, avoid duplicates)
//   const saveAndMaybeReload = async () => {
//     const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//     const jwt = (jwtEl.value || "").trim();
//     await chrome.storage.local.set({ baseUrl, jwt });
//   };

//   baseUrlEl?.addEventListener("input", saveAndMaybeReload);
//   jwtEl?.addEventListener("input", saveAndMaybeReload);

//   // Keep your background owner polling trigger (but only once)
//   if ((saved.baseUrl || baseUrlEl?.value) && (saved.jwt || jwtEl?.value)) {
//     chrome.runtime.sendMessage({
//       type: "START_OWNER_POLLING",
//       baseUrl: normalizeBaseUrl(baseUrlEl.value),
//       jwt: (jwtEl.value || "").trim(),
//     });
//   }

//   // Start owner pending list polling + initial render
//   startPendingPolling();
//   loadPendingRequests();
// });

