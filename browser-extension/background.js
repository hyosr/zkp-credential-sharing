/* =========================
 * ZKP Credential Sharing - Extension Background
 * Supports:
 *  - RUN_HANDOFF: fetch /sharing/handoff/<id>, inject cookies + storages, open connected tab
 *  - ASSISTED_START (recipient): POST /sharing/assisted/request, poll /status, open /handoff?handoff_token=...
 *  - OWNER polling: GET /sharing/assisted/pending, show notification, click => POST /approve then open assist_login_url
 * ========================= */

/* ---------- helpers ---------- */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function originFromUrl(u) {
  try {
    const x = new URL(u);
    return `${x.protocol}//${x.host}/`;
  } catch {
    return u;
  }
}

function hostFromUrl(u) {
  try {
    return new URL(u).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function normalizeDomain(d) {
  return (d || "").toLowerCase().replace(/^\./, "");
}

function domainMatches(cookieDomain, targetHost) {
  const cd = normalizeDomain(cookieDomain);
  const h = normalizeDomain(targetHost);
  return cd === h || h.endsWith("." + cd) || cd.endsWith("." + h);
}

function waitForTabComplete(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    const t0 = Date.now();

    function cleanup() {
      chrome.tabs.onUpdated.removeListener(onUpdated);
    }

    function done() {
      cleanup();
      resolve();
    }

    function onUpdated(id, info) {
      if (id === tabId && info.status === "complete") return done();
      if (Date.now() - t0 > timeoutMs) return done();
    }

    chrome.tabs.onUpdated.addListener(onUpdated);
    setTimeout(done, timeoutMs);
  });
}

function safeParseJsonObject(s) {
  if (!s) return null;
  try {
    const obj = JSON.parse(s);
    return obj && typeof obj === "object" ? obj : null;
  } catch {
    return null;
  }
}

function normalizeSameSite(v) {
  if (!v) return "lax";
  const s = String(v).toLowerCase();
  if (s === "strict") return "strict";
  if (s === "none") return "no_restriction";
  return "lax";
}

function cookieSetUrlForDomain(serviceOriginUrl, cookieDomain) {
  const u = new URL(serviceOriginUrl);
  const d = (cookieDomain || u.hostname).replace(/^\./, "");
  return `${u.protocol}//${d}/`;
}

async function setOneCookie(serviceOriginUrl, c, forcedDomain = null) {
  const domain = forcedDomain || c.domain;

  const details = {
    url: cookieSetUrlForDomain(serviceOriginUrl, domain),
    name: c.name,
    value: c.value,
    path: c.path || "/",
    httpOnly: !!c.httpOnly,
    secure: !!c.secure,
    sameSite: normalizeSameSite(c.sameSite),
  };

  if (domain) details.domain = domain;

  // Session cookie: expires = -1 or undefined => do not set expirationDate
  if (typeof c.expires === "number" && c.expires > 0) {
    details.expirationDate = c.expires;
  }

  return await chrome.cookies.set(details);
}

async function injectCookies(serviceOriginUrl, cookies, opts = {}) {
  const delayBeforeMs = Number(opts.delayBeforeMs ?? 300);
  const delayBetweenMs = Number(opts.delayBetweenMs ?? 150);
  const delayAfterMs = Number(opts.delayAfterMs ?? 600);

  const list = Array.isArray(cookies) ? cookies : [];

  await sleep(delayBeforeMs);

  for (const c of list) {
    try {
      await setOneCookie(serviceOriginUrl, c);

      // Optional: special case recolyse token cookie (if you still need it)
      if (c.name === "token" && normalizeDomain(c.domain) === "recolyse.com") {
        await setOneCookie(serviceOriginUrl, c, ".recolyse.com");
      }
    } catch (e) {
      console.warn("Failed to set cookie:", c?.name, c?.domain, e);
    }
    await sleep(delayBetweenMs);
  }

  await sleep(delayAfterMs);
}

async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
  await chrome.scripting.executeScript({
    target: { tabId },
    func: (ls, ss) => {
      try {
        if (ls && typeof ls === "object") {
          for (const [k, v] of Object.entries(ls)) localStorage.setItem(k, v);
        }
        if (ss && typeof ss === "object") {
          for (const [k, v] of Object.entries(ss)) sessionStorage.setItem(k, v);
        }
      } catch (e) {
        console.error("Storage injection error:", e);
      }
    },
    args: [localStorageObj, sessionStorageObj],
  });

  await sleep(500);
  await chrome.tabs.reload(tabId);
}

async function fetchJson(url) {
  const r = await fetch(url, { method: "GET" });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`Fetch failed ${r.status}: ${t}`);
  }
  return await r.json();
}

async function apiGetJson(baseUrl, path, jwtToken) {
  const r = await fetch(`${baseUrl}${path}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${jwtToken}` },
  });
  if (!r.ok) throw new Error(`GET ${path} failed: ${r.status} ${await r.text()}`);
  return await r.json();
}

async function apiPostJson(baseUrl, path, jwtToken, body) {
  const r = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${jwtToken}`,
    },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) throw new Error(`POST ${path} failed: ${r.status} ${await r.text()}`);
  return await r.json();
}

/* ---------- Mode 1: Handoff injection ---------- */
async function doHandoff(handoffUrl, opts = {}) {
  const delayBetweenCookies = Number(opts.delayBetweenCookies ?? 150);
  const delayAfterInject = Number(opts.delayAfterInject ?? 800);

  const data = await fetchJson(handoffUrl);

  const serviceUrl = data.service_url;
  const currentUrl = data.current_url || serviceUrl;

  if (!serviceUrl) throw new Error("handoff response missing service_url");

  const cookies = Array.isArray(data.cookies) ? data.cookies : [];
  const localStorageObj = safeParseJsonObject(data.localStorage);
  const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

  const targetHost = hostFromUrl(currentUrl || serviceUrl);
  const serviceOrigin = originFromUrl(currentUrl || serviceUrl);

  // Filter only target-domain cookies
  const filteredCookies = cookies.filter((c) => domainMatches(c.domain, targetHost));

  // Inject cookies (slow)
  await injectCookies(serviceOrigin, filteredCookies, {
    delayBeforeMs: 300,
    delayBetweenMs: delayBetweenCookies,
    delayAfterMs: 400,
  });

  // Let Chrome apply them
  await sleep(delayAfterInject);

  // Open target page
  const tab = await chrome.tabs.create({ url: currentUrl, active: true });
  await waitForTabComplete(tab.id, 15000);

  // Inject storage + reload
  await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);

  // Optional: wait reload complete
  await waitForTabComplete(tab.id, 15000);
}

/* ---------- Mode 2: Assisted (safe) ---------- */
let assistedPollTimer = null;

async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
  // Create assisted request
  const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { token: shareToken });
  const requestId = created.request_id;

  await chrome.storage.local.set({ assistedRequestId: requestId });

  // Poll status until completed
  if (assistedPollTimer) clearInterval(assistedPollTimer);

  assistedPollTimer = setInterval(async () => {
    try {
      const st = await apiGetJson(
        baseUrl,
        `/sharing/assisted/${encodeURIComponent(requestId)}/status`,
        jwtToken
      );

      if (st.status === "completed" && st.handoff_token) {
        clearInterval(assistedPollTimer);
        assistedPollTimer = null;

        const url = `${baseUrl}/handoff?handoff_token=${encodeURIComponent(st.handoff_token)}&redirect_to=/app`;
        await chrome.tabs.create({ url, active: true });

        if (chrome.notifications?.create) {
          chrome.notifications.create(`assisted:done:${requestId}`, {
            type: "basic",
            iconUrl: "icon128.png",
            title: "Assisted access ready",
            message: "Opening delegated session…",
            priority: 1,
          });
        }
      } else if (st.status === "expired" || st.status === "rejected") {
        clearInterval(assistedPollTimer);
        assistedPollTimer = null;

        if (chrome.notifications?.create) {
          chrome.notifications.create(`assisted:fail:${requestId}`, {
            type: "basic",
            iconUrl: "icon128.png",
            title: "Assisted access failed",
            message: `Status: ${st.status}`,
            priority: 2,
          });
        }
      }
    } catch (e) {
      console.warn("assisted poll error:", e);
    }
  }, 2000);

  return { requestId };
}

/* ---------- Owner: pending polling + notification ---------- */
let ownerPollTimer = null;
let seenPending = new Set();

async function ownerStartPolling(baseUrl, jwtToken) {
  if (ownerPollTimer) return;

  ownerPollTimer = setInterval(async () => {
    try {
      const pending = await apiGetJson(baseUrl, "/sharing/assisted/pending", jwtToken);

      for (const item of pending) {
        const key = item.request_id;
        if (seenPending.has(key)) continue;
        seenPending.add(key);

        if (chrome.notifications?.create) {
          chrome.notifications.create(`assist:${key}`, {
            type: "basic",
            iconUrl: "icon128.png",
            title: "Assisted login request",
            message: `Recipient requests access to: ${item.service_url}`,
            priority: 2,
          });
        }
      }
    } catch (e) {
      console.warn("owner pending poll error:", e);
    }
  }, 5000);
}

if (chrome.notifications?.onClicked) {
  chrome.notifications.onClicked.addListener(async (notifId) => {
    if (!notifId.startsWith("assist:")) return;
    const requestId = notifId.slice("assist:".length);

    const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
    if (!baseUrl || !jwt) return;

    try {
      const resp = await apiPostJson(
        baseUrl,
        `/sharing/assisted/${encodeURIComponent(requestId)}/approve`,
        jwt,
        {}
      );

      if (!resp.assist_login_url) throw new Error("approve response missing assist_login_url");

      // Owner will complete CAPTCHA/2FA on YOUR site; your site then calls /complete.
      await chrome.tabs.create({ url: resp.assist_login_url, active: true });
    } catch (e) {
      console.error("approve failed:", e);
    }
  });
}

/* ---------- Messages from popup ---------- */
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Existing mode: injection from handoff URL
  if (msg?.type === "RUN_HANDOFF") {
    (async () => {
      try {
        const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
        if (!url) throw new Error("Missing handoffUrl");
        await doHandoff(url, msg.opts || {});
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }

  // New mode: assisted recipient flow (safe)
  if (msg?.type === "ASSISTED_START") {
    (async () => {
      try {
        const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
        const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
        if (!baseUrl) throw new Error("Missing baseUrl");
        if (!jwt) throw new Error("Missing jwt");
        if (!msg.shareToken) throw new Error("Missing shareToken");

        await chrome.storage.local.set({ baseUrl, jwt });

        // Start owner polling too (if user is owner in another session, they will see pending)
        ownerStartPolling(baseUrl, jwt).catch(() => {});

        const r = await assistedStartFlow(baseUrl, jwt, msg.shareToken);
        sendResponse({ ok: true, requestId: r.requestId });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }
});

/* ---------- Optional: Bridge URL support (handoff injection) ---------- */
function isBridgeUrl(url) {
  try {
    const u = new URL(url);
    return (
      u.origin === "http://localhost:8001" &&
      u.pathname === "/extension/connect" &&
      u.searchParams.get("handoff")
    );
  } catch {
    return false;
  }
}

chrome.webNavigation.onCommitted.addListener(async (details) => {
  if (details.frameId !== 0) return;
  if (!isBridgeUrl(details.url)) return;

  const u = new URL(details.url);
  const handoffUrl = u.searchParams.get("handoff");

  try {
    await chrome.storage.local.set({ handoffUrl });
    await doHandoff(handoffUrl, {});
    chrome.tabs.remove(details.tabId);
  } catch (e) {
    console.error("Bridge handoff failed:", e);
  }
});





































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
//   try { return new URL(u).hostname.toLowerCase(); } catch { return ""; }
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



































// async function fetchJson(url) {
//   const r = await fetch(url, { method: "GET" });
//   if (!r.ok) {
//     const t = await r.text();
//     throw new Error(`Fetch failed ${r.status}: ${t}`);
//   }
//   return await r.json();
// }

// function normalizeSameSite(v) {
//   if (!v) return "lax";
//   const s = String(v).toLowerCase();
//   if (s === "strict") return "strict";
//   if (s === "none") return "no_restriction";
//   return "lax";
// }

// function buildCookieSetUrl(cookie, serviceUrl) {
//   const u = new URL(serviceUrl);
//   const domain = (cookie.domain || u.hostname).replace(/^\./, "");
//   return `${u.protocol}//${domain}/`;
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

// async function setOneCookie(serviceUrl, c, forcedDomain = null) {
//   const details = {
//     url: buildCookieSetUrl(c, serviceUrl),
//     name: c.name,
//     value: c.value,
//     path: c.path || "/",
//     httpOnly: !!c.httpOnly,
//     secure: !!c.secure,
//     sameSite: normalizeSameSite(c.sameSite),
//   };

//   const domain = forcedDomain || c.domain;
//   if (domain) details.domain = domain;

//   if (typeof c.expires === "number" && c.expires > 0) {
//     details.expirationDate = c.expires;
//   }

//   await chrome.cookies.set(details);
// }








// async function injectCookies(serviceUrl, cookies, opts = {}) {
//   const delayBeforeMs = Number(opts.delayBeforeMs ?? 300);
//   const delayBetweenMs = Number(opts.delayBetweenMs ?? 120);
//   const delayAfterMs = Number(opts.delayAfterMs ?? 400);

//   const list = Array.isArray(cookies) ? cookies : [];

//   // small pause before starting
//   await sleep(delayBeforeMs);

//   for (const c of list) {
//     await setOneCookie(serviceUrl, c);

//     // keep your recolyse special case if you need it
//     if (c.name === "token" && c.domain === "recolyse.com") {
//       await setOneCookie(serviceUrl, c, ".recolyse.com");
//     }

//     // pause between cookies
//     await sleep(delayBetweenMs);
//   }

//   // pause after all cookies are set
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

//   await new Promise((resolve) => setTimeout(resolve, 800));
//   await chrome.tabs.reload(tabId);
// }






// async function doHandoff(handoffUrl, opts = {}) {
//   const delayBetweenCookies = Number(opts.delayBetweenCookies ?? 150);
//   const delayAfterInject = Number(opts.delayAfterInject ?? 800);

//   const data = await fetchJson(handoffUrl);

//   const serviceUrl = data.service_url;
//   const currentUrl = data.current_url || serviceUrl;

//   const cookies = Array.isArray(data.cookies) ? data.cookies : [];
//   const localStorageObj = safeParseJsonObject(data.localStorage);
//   const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

//   const targetHost = hostFromUrl(currentUrl || serviceUrl);
//   const cookieScopeUrl = originFromUrl(currentUrl || serviceUrl);

//   // Inject only cookies for the target site (avoid 3rd-party noise)
//   const filteredCookies = cookies.filter((c) => domainMatches(c.domain, targetHost));

//   // 1) inject cookies slowly
//   for (const c of filteredCookies) {
//     await setOneCookie(cookieScopeUrl, c);
//     await sleep(delayBetweenCookies);
//   }

//   // 2) wait a bit so browser applies cookies before opening target
//   await sleep(delayAfterInject);

//   // 3) open target page
//   const tab = await chrome.tabs.create({ url: currentUrl, active: true });

//   // Wait until the page actually loads before injecting storage
//   await waitForTabComplete(tab.id, 15000);

//   // 4) inject storage (best-effort)
//   // await injectStorageIntoTab(tab.id, localStorageObj, sessionStorageObj);
//   await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
//   // 5) reload so site re-reads cookies/storage
//   // await chrome.tabs.reload(tab.id);
// }





















// // Receive message from popup
// chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
//   if (msg?.type === "RUN_HANDOFF") {
//     (async () => {
//       try {
//         const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
//         if (!url) throw new Error("Missing handoffUrl");
//         // await doHandoff(url);
//         await doHandoff(msg.handoffUrl, msg.opts || {});
//         sendResponse({ ok: true });
//       } catch (e) {
//         sendResponse({ ok: false, error: String(e?.message || e) });
//       }
//     })();
//     return true; // keep message channel open for async response
//   }
// });






// function isBridgeUrl(url) {
//   try {
//     const u = new URL(url);
//     return (
//       u.origin === "http://localhost:8001" &&
//       u.pathname === "/extension/connect" &&
//       u.searchParams.get("handoff")
//     );
//   } catch {
//     return false;
//   }
// }

// chrome.webNavigation.onCommitted.addListener(async (details) => {
//   if (details.frameId !== 0) return; // top frame only
//   if (!isBridgeUrl(details.url)) return;

//   const u = new URL(details.url);
//   const handoffUrl = u.searchParams.get("handoff");

//   try {
//     // Save for history and run immediately
//     await chrome.storage.local.set({ handoffUrl });
//     await doHandoff(handoffUrl);

//     // optional: close the bridge tab
//     chrome.tabs.remove(details.tabId);
//   } catch (e) {
//     console.error("Bridge handoff failed:", e);
//   }
// });








































































