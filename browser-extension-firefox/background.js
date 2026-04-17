







































/* =====================//  * ZKP Credential Sharing - Extension Background
 * Supports:
 *  - RUN_HANDOFF: fetch /sharing/handoff/<id>, inject cookies + storages, open connected tab
 *  - ASSISTED_START (recipient): POST /sharing/assisted/request, poll /status, open /handoff?handoff_token=...
 *  - OWNER polling: GET /sharing/assisted/pending, show notification, click => POST /approve then open assist_login_url
 * ========================= */



const chrome = typeof browser !== "undefined" ? browser : chrome;





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
  } catch (e) {
    console.error("Failed to parse JSON:", s, e);
    return null;
  }
}

// function normalizeSameSite(v) {
//   if (!v) return "lax";
//   const s = String(v).toLowerCase();
//   if (s === "strict") return "strict";
//   if (s === "none") return "no_restriction";
//   return "lax";
// }

function normalizeSameSite(v) {
  if (!v) return "lax";
  const s = String(v).toLowerCase();

  if (s === "strict") return "strict";
  if (s === "none" || s === "no_restriction") return "no_restriction";

  // chrome/firefox return values often seen from exported cookies
  if (s === "unspecified" || s === "unspec" || s === "no_restriction") return "lax";

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
  const delayBeforeMs = Number(opts.delayBeforeMs ?? 1000);
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



async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
  if (!localStorageObj && !sessionStorageObj) return;
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
        console.log("Storage injected:", { ls, ss });
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











async function injectStorageOnly(tabId, localStorageObj, sessionStorageObj) {
  if (!localStorageObj && !sessionStorageObj) return;
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
        console.log("✅ Storage injected:", { ls, ss });
      } catch (e) {
        console.error("Storage injection error:", e);
      }
    },
    args: [localStorageObj, sessionStorageObj],
  });
  await sleep(300); // petit délai pour stabiliser
}










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








// 2. doHandoff simplifié – sans rechargement automatique
async function doHandoff(handoffUrl, opts = {}) {
  const data = await fetchJson(handoffUrl);
  const serviceUrl = data.service_url;
  let currentUrl = data.current_url || serviceUrl;

  if (!serviceUrl) throw new Error("handoff response missing service_url");

  // Nettoyage léger de l'URL (optionnel)
  try {
    const urlObj = new URL(currentUrl);
    urlObj.search = "";
    urlObj.hash = "";
    if (urlObj.pathname.endsWith("/verify_otp") || urlObj.pathname.includes("/auth")) {
      const parts = urlObj.pathname.split("/");
      parts.pop();
      urlObj.pathname = parts.join("/") + "/";
    }
    currentUrl = urlObj.toString();
    console.log("URL nettoyée pour handoff:", currentUrl);
  } catch (e) {
    console.warn("Impossible de parser l'URL, utilisation brute", currentUrl);
  }

  const cookies = data.cookies || [];
  const localStorageObj = safeParseJsonObject(data.localStorage);
  const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

  const finalUrl = currentUrl || serviceUrl;

  // 1. Ouvrir l'onglet sur l'URL finale
  const tab = await chrome.tabs.create({ url: finalUrl, active: true });
  await waitForTabComplete(tab.id, 15000);

  // 2. Injecter d'abord le localStorage/sessionStorage
  await injectStorageOnly(tab.id, localStorageObj, sessionStorageObj);

  // 3. Injecter les cookies (y compris HttpOnly)
  const serviceOrigin = originFromUrl(finalUrl);
  for (const c of cookies) {
    try {
      await setOneCookie(serviceOrigin, c);
      await sleep(Number(opts.delayBetweenCookies ?? 100));
    } catch (e) {
      console.warn("Cookie injection failed:", c?.name, e);
    }
  }

  // 4. Attendre un peu pour laisser le site traiter les données
  await sleep(1000);

  // 5. Afficher une notification pour demander un rechargement manuel
  // if (chrome.notifications?.create) {
  //   chrome.notifications.create("reload_needed", {
  //     type: "basic",
  //     title: "Injection terminée",
  //     message: "Les cookies et le stockage local ont été injectés. Rechargez la page manuellement (F5) pour appliquer la session.",
  //     priority: 2,
  //   });
  // }

  console.log("Handoff terminé – l'utilisateur doit recharger la page manuellement.");
}














// async function doHandoffFromData(data, opts = {}) {
//   const serviceUrl = data.service_url;
//   const currentUrl = data.current_url || serviceUrl;
//   if (!serviceUrl) throw new Error("Missing service_url");

//   const cookies = Array.isArray(data.cookies) ? data.cookies : [];
//   const localStorageObj =
//     typeof data.localStorage === "string" ? JSON.parse(data.localStorage || "{}") : (data.localStorage || {});
//   const sessionStorageObj =
//     typeof data.sessionStorage === "string" ? JSON.parse(data.sessionStorage || "{}") : (data.sessionStorage || {});

//   // 1) open service origin
//   const tab = await chrome.tabs.create({ url: serviceUrl, active: true });
//   await waitForTabComplete(tab.id, 20000);

//   // 2) inject storages on same origin
//   await chrome.scripting.executeScript({
//     target: { tabId: tab.id },
//     world: "MAIN",
//     func: (ls, ss) => {
//       for (const [k, v] of Object.entries(ls || {})) localStorage.setItem(k, String(v));
//       for (const [k, v] of Object.entries(ss || {})) sessionStorage.setItem(k, String(v));
//     },
//     args: [localStorageObj, sessionStorageObj],
//   });

//   // 3) inject cookies
//   for (const c of cookies) {
//     try {
//       await setOneCookie(originFromUrl(serviceUrl), {
//         name: c.name,
//         value: c.value,
//         domain: c.domain,
//         path: c.path || "/",
//         secure: !!c.secure,
//         httpOnly: !!c.httpOnly,
//         sameSite: c.sameSite || "Lax",
//         expires: c.expirationDate || c.expires
//       });
//     } catch (e) {
//       console.warn("cookie set failed", c?.name, e);
//     }
//     await sleep(Number(opts.delayBetweenCookies ?? 80));
//   }

//   // 4) reload to let app read storage/cookies
//   await chrome.tabs.reload(tab.id);
//   await waitForTabComplete(tab.id, 20000);

//   // 5) go final page
//   if (currentUrl && currentUrl !== serviceUrl) {
//     await chrome.tabs.update(tab.id, { url: currentUrl });
//     await waitForTabComplete(tab.id, 20000);
//   }
// }





async function doHandoffFromData(data, opts = {}) {
  const serviceUrl = data.service_url;
  const currentUrl = data.current_url || serviceUrl;
  if (!serviceUrl) throw new Error("Missing service_url");

  const cookies = Array.isArray(data.cookies) ? data.cookies : [];
  const ls =
    typeof data.localStorage === "string"
      ? JSON.parse(data.localStorage || "{}")
      : (data.localStorage || {});
  const ss =
    typeof data.sessionStorage === "string"
      ? JSON.parse(data.sessionStorage || "{}")
      : (data.sessionStorage || {});

  // 1) open service origin first
  const tab = await ext.tabs.create({ url: serviceUrl, active: true });
  await waitForTabComplete(tab.id, 30000);

  // 2) inject storage first
  await injectStorage(tab.id, ls, ss);

  // 3) inject cookies on service origin
  const serviceOrigin = originFromUrl(serviceUrl);
  for (const c of cookies) {
    try {
      await setOneCookie(serviceOrigin, {
        ...c,
        expires: c.expires ?? c.expirationDate
      });
    } catch (e) {
      console.warn("cookie set failed:", c?.name, e);
    }
    await sleep(Number(opts.delayBetweenCookies ?? 120));
  }

  // IMPORTANT: no reload here (SSO sites may clear session on reload)
  await sleep(250);

  // 4) navigate directly to captured connected page
  if (currentUrl && currentUrl !== serviceUrl) {
    await ext.tabs.update(tab.id, { url: currentUrl });
    await waitForTabComplete(tab.id, 30000);
  }

  // 5) re-apply cookies once on final origin (stabilization)
  const finalOrigin = originFromUrl(currentUrl || serviceUrl);
  for (const c of cookies) {
    try {
      await setOneCookie(finalOrigin, {
        ...c,
        expires: c.expires ?? c.expirationDate
      });
    } catch (e) {
      console.warn("cookie re-set failed:", c?.name, e);
    }
    await sleep(80);
  }

  // 6) inject storage again (some SPAs clear it on first boot)
  await injectStorage(tab.id, ls, ss);

  await sleep(Number(opts.delayAfterInject ?? 300));
}






























/* ---------- Mode 2: Assisted (safe) ---------- */
let assistedPollTimer = null;

// async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
//   // Create assisted request
//   const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { share_token: shareToken });
//   // const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { token: shareToken });
//   const requestId = created.request_id;

//   await chrome.storage.local.set({ assistedRequestId: requestId });

//   // Poll status until completed
//   if (assistedPollTimer) clearInterval(assistedPollTimer);

//   assistedPollTimer = setInterval(async () => {
//     try {
//       const st = await apiGetJson(
//         baseUrl,
//         `/sharing/assisted/${encodeURIComponent(requestId)}/status`,
//         jwtToken
//       );

//       if (st.status === "completed" && st.handoff_token) {
//         clearInterval(assistedPollTimer);
//         assistedPollTimer = null;

//         const url = `${baseUrl}/handoff?handoff_token=${encodeURIComponent(st.handoff_token)}&redirect_to=/app`;
//         await chrome.tabs.create({ url, active: true });

//         if (chrome.notifications?.create) {
//           chrome.notifications.create(`assisted:done:${requestId}`, {
//             type: "basic",
//             // iconUrl: "icon128.png",
//             title: "Assisted access ready",
//             message: "Opening delegated session…",
//             priority: 1,
//           });
//         }
//       } else if (st.status === "expired" || st.status === "rejected") {
//         clearInterval(assistedPollTimer);
//         assistedPollTimer = null;

//         if (chrome.notifications?.create) {
//           chrome.notifications.create(`assisted:fail:${requestId}`, {
//             type: "basic",
//             // iconUrl: "icon128.png",
//             title: "Assisted access failed",
//             message: `Status: ${st.status}`,
//             priority: 2,
//           });
//         }
//       }
//     } catch (e) {
//       console.warn("assisted poll error:", e);
//     }
//   }, 2000);

//   return { requestId };
// }



async function assistedStartFlow(baseUrl, jwtToken, shareToken) {
  const created = await apiPostJson(baseUrl, "/sharing/assisted/request", jwtToken, { share_token: shareToken });
  const requestId = created.request_id;

  await chrome.storage.local.set({ assistedRequestId: requestId });

  if (assistedPollTimer) clearInterval(assistedPollTimer);

  assistedPollTimer = setInterval(async () => {
    try {
      const st = await apiGetJson(
        baseUrl,
        `/sharing/assisted/${encodeURIComponent(requestId)}/status`,
        jwtToken
      );

      if (st.status === "completed" && st.handoff_url) {  // ← CHANGÉ
        clearInterval(assistedPollTimer);
        assistedPollTimer = null;

        // Construire l'URL complète du handoff
        const fullHandoffUrl = `${baseUrl}${st.handoff_url}`;  // ← CHANGÉ
        
        await doHandoff(fullHandoffUrl, {});  // Utiliser directement doHandoff

        if (chrome.notifications?.create) {
          chrome.notifications.create({
            type: "basic",
            iconUrl: "icon32.png",
            title: "Assisted Share Complete",
            message: "Connected profile opened.",
          });
        }
      }
    } catch (err) {
      console.error("Assisted status poll error:", err);
    }
  }, 2000);
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
          try {
            await new Promise((resolve, reject) => {
              chrome.notifications.create(`assist:${key}`, {
                type: "basic",
                title: "Assisted login request",
                message: `Recipient requests access to: ${item.service_url}`,
                priority: 2,
              }, (notificationId) => {
                if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
                else resolve(notificationId);
              });
            });
          } catch (notifErr) {
            console.error("Notification creation failed:", notifErr);
          }
        }
      }
    } catch (e) {



      const msg = String(e?.message || e);

      // ✅ si JWT expiré/invalide: stop polling pour éviter un spam infini
      if (msg.includes("failed: 401") || msg.includes(" 401 ") || msg.includes("Token invalide")) {
        console.warn("[ownerStartPolling] JWT invalid/expired -> stopping polling. Please re-login / paste a new JWT.");
        clearInterval(ownerPollTimer);
        ownerPollTimer = null;

        // optionnel: notifier l'utilisateur
        if (chrome.notifications?.create) {
          chrome.notifications.create("owner:jwt:expired", {
          type: "basic",
          title: "JWT expiré",
          message: "Ton JWT backend est invalide/expiré. Ouvre l’extension et colle un nouveau JWT.",
          priority: 2,
      });
    }
      return;
  }

    console.warn("owner pending poll error:", e);




      // console.warn("owner pending poll error:", e);




    }
  }, 5000);
}




















// Notification click handler (owner)
if (chrome.notifications?.onClicked) {
  chrome.notifications.onClicked.addListener(async (notifId) => {
    if (!notifId.startsWith("assist:")) return;
    const requestId = notifId.slice("assist:".length);
    const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
    if (!baseUrl || !jwt) return;
    try {
      const resp = await apiPostJson(baseUrl, `/sharing/assisted/${encodeURIComponent(requestId)}/approve`, jwt, {});
      if (!resp.assist_login_url) throw new Error("approve response missing assist_login_url");
      const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });
      // Store pending capture info with service URL (the target site URL)
      // await chrome.storage.local.set({
      //   pendingCaptureRequestId: requestId,
      //   pendingCaptureTabId: tab.id,
      //   pendingServiceUrl: resp.assist_login_url  // URL du site cible
      // });

      await chrome.storage.local.set({
        pendingCaptureRequestId: requestId
      });



      chrome.action.setBadgeText({ text: "!" });
    } catch (e) {
      console.error("approve failed:", e);
    }
  });
}














async function captureCurrentSession(tabId, serviceUrl, requestId) {
  console.log("[CAPTURE] Starting session capture...");
  console.log("[CAPTURE] Tab ID:", tabId);
  console.log("[CAPTURE] Service URL:", serviceUrl);
  console.log("[CAPTURE] Request ID:", requestId);

  let targetUrl = serviceUrl;
  if (targetUrl.includes('/login') || targetUrl.includes('/auth')) {
    const origin = new URL(targetUrl).origin;
    targetUrl = origin + '/';
    console.log("[CAPTURE] URL corrected to origin:", targetUrl);
  }

  // Get cookies for the domain
  const cookies = await chrome.cookies.getAll({ url: serviceUrl });
  console.log(`[CAPTURE] Found ${cookies.length} cookies for domain`);
  
  for (const c of cookies) {
    console.log(`[CAPTURE] Cookie: ${c.name} | Domain: ${c.domain} | Secure: ${c.secure} | SameSite: ${c.sameSite}`);
  }

  // NEW: Validate cookie structure before sending
  const validatedCookies = cookies.map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain || new URL(serviceUrl).hostname,
    path: c.path || "/",
    expires: c.expirationDate || -1,
    httpOnly: c.httpOnly || false,
    secure: c.secure || false,
    sameSite: c.sameSite || "Lax",
  }));

  // Execute in page to get localStorage and sessionStorage
  const injectionResult = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const ls = {};
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        ls[key] = localStorage.getItem(key);
      }
      const ss = {};
      for (let i = 0; i < sessionStorage.length; i++) {
        const key = sessionStorage.key(i);
        ss[key] = sessionStorage.getItem(key);
      }
      return { localStorage: ls, sessionStorage: ss };
    }
  });
  
  const { localStorage, sessionStorage } = injectionResult[0].result;
  console.log("[CAPTURE] localStorage keys:", Object.keys(localStorage).length);
  console.log("[CAPTURE] sessionStorage keys:", Object.keys(sessionStorage).length);

  const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
  
  if (!jwt) {
    throw new Error("No JWT token found in storage");
  }

  console.log("[CAPTURE] Submitting session to backend...");
  const response = await fetch(`${baseUrl}/sharing/assisted/${requestId}/session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${jwt}`,
    },
    body: JSON.stringify({
      cookies: validatedCookies,
      localStorage: JSON.stringify(localStorage),
      sessionStorage: JSON.stringify(sessionStorage),
      current_url: targetUrl
    }),
  });
  
  if (!response.ok) {
    const errorText = await response.text();
    console.error("[CAPTURE] Submit failed:", response.status, errorText);
    throw new Error(`Failed to submit session (HTTP ${response.status})`);
  }
  
  const result = await response.json();
  console.log("[CAPTURE] ✅ Session submitted successfully");
  console.log("[CAPTURE] Handoff session ID:", result.handoff_session_id);
  
  return result;
}










async function resolveFinalCaptureToken(baseUrl, jwt, finalToken) {
  const r = await fetch(`${baseUrl}/sharing/final-capture/resolve/${encodeURIComponent(finalToken)}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${jwt}`,
    },
  });
  if (!r.ok) throw new Error(`Resolve failed: ${r.status} ${await r.text()}`);
  return await r.json();
}



















console.log("[BG] loaded");




// ---------- Messages from popup ----------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {


  console.log("[BG] message:", msg?.type);

  if (msg?.type === "CREATE_OWNER_HANDOFF_WITH_REQUEST") {
    (async () => {
      try {
        const baseUrl = (msg.baseUrl || "").trim();
        const jwt = (msg.jwt || "").trim();
        const requestId = (msg.requestId || "").trim();
        const capture = msg.capture || {};

        if (!baseUrl) throw new Error("Missing baseUrl");
        if (!jwt) throw new Error("Missing jwt");
        if (!requestId) throw new Error("Missing requestId");

        const url = `${baseUrl}/sharing/owner-handoff/from-capture`;
        console.log("[BG] POST", url);

        const res = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${jwt}`,
          },
          body: JSON.stringify({
            request_id: requestId,
            cookies: capture.cookies || [],
            localStorage: capture.localStorage || "{}",
            sessionStorage: capture.sessionStorage || "{}",
            current_url: capture.current_url || null,
            service_url: capture.service_url || null,
          }),
        });

        const text = await res.text();
        console.log("[BG] status:", res.status, text);

        if (!res.ok) throw new Error(`HTTP ${res.status}: ${text}`);
        let out = {};
        try { out = JSON.parse(text); } catch {}

        const handoffUrl = out.handoff_url
          ? `${baseUrl}${out.handoff_url}`
          : `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;

        // sendResponse({ ok: true, handoffUrl });

        sendResponse({ ok: true, handoffUrl, handoff_url: handoffUrl });
      } catch (e) {
        console.error("[BG] handoff create failed:", e);
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
  return true;
  }








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

  if (msg.type === "CAPTURE_SESSION") {
    (async () => {
      try {
        const { tabId, serviceUrl, requestId } = msg;
        if (!tabId || !serviceUrl || !requestId) {
          throw new Error("Missing capture parameters");
        }
        await captureCurrentSession(tabId, serviceUrl, requestId);
        await chrome.storage.local.remove(["pendingCaptureRequestId", "pendingCaptureTabId", "pendingServiceUrl"]);
        chrome.action.setBadgeText({ text: "" });
        sendResponse({ ok: true });
      } catch (e) {
        console.error("Capture failed:", e);
        sendResponse({ ok: false, error: e.message });
      }
    })();
    return true;
  }




  if (msg.type === "START_OWNER_POLLING") {
  ownerStartPolling(msg.baseUrl, msg.jwt).catch(console.error);
  sendResponse({ ok: true });
  return true;
}










  if (msg?.type === "ASSISTED_START") {
    (async () => {
      try {
        const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
        const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
        if (!baseUrl || !jwt || !msg.shareToken) throw new Error("Missing parameters");
        await chrome.storage.local.set({ baseUrl, jwt });
        ownerStartPolling(baseUrl, jwt).catch(() => {});
        const r = await assistedStartFlow(baseUrl, jwt, msg.shareToken);
        sendResponse({ ok: true, requestId: r.requestId });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }







  if (msg?.type === "OPEN_CONNECTED_PROFILE_BY_TOKEN") {
    (async () => {
      try {
        const baseUrl = msg.baseUrl || (await chrome.storage.local.get(["baseUrl"])).baseUrl;
        const jwt = msg.jwt || (await chrome.storage.local.get(["jwt"])).jwt;
        const finalToken = (msg.finalToken || "").trim();
        if (!baseUrl || !jwt || !finalToken) throw new Error("Missing baseUrl/jwt/finalToken");

        const resolved = await resolveFinalCaptureToken(baseUrl, jwt, finalToken);
        const handoffUrl = `${baseUrl}${resolved.handoff_url}`;
        await doHandoff(handoffUrl, msg.opts || {});
        sendResponse({ ok: true, data: resolved });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }






  if (msg?.type === "CREATE_HANDOFF_FROM_CAPTURE") {
  (async () => {
    try {
      const baseUrl = msg.baseUrl;
      const jwt = msg.jwt;
      const capture = msg.capture; // {cookies, localStorage, sessionStorage, current_url, service_url}

      const r = await fetch(`${baseUrl}/sharing/create-handoff`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${jwt}`,
        },
        body: JSON.stringify(capture),
      });

      if (!r.ok) throw new Error(await r.text());
      const out = await r.json(); // expects {handoff_url: "..."} or {handoff_session_id: "..."}
      const handoffUrl = out.handoff_url || `${baseUrl}/sharing/handoff/${out.handoff_session_id}`;

      sendResponse({ ok: true, handoff_url: handoffUrl });
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message || e) });
    }
  })();
  return true;
}


  if (msg?.type === "FORCE_INJECT_JSON_CURRENT_TAB") {
  (async () => {
    try {
      const [tab] = await ext.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id) throw new Error("No active tab");
      const r = await forceInjectCurrentTabFromData(tab.id, msg.session, msg.opts || {});
      sendResponse({ ok: true, data: r });
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message || e) });
    }
  })();
  return true;
}





});








// Auto-start owner polling when extension loads
(async () => {
  const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
  if (baseUrl && jwt) {
    ownerStartPolling(baseUrl, jwt).catch(console.error);
  }
})();













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






// Auto-start owner polling when extension loads
(async () => {
  const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
  if (baseUrl && jwt) {
    console.log("Auto-starting owner polling");

    ownerStartPolling(baseUrl, jwt).catch(console.error);
  }else {
    console.log("Missing baseUrl or jwt, polling not started");
  }
  
})();


















async function forceInjectCurrentTabFromData(tabId, data, opts = {}) {
  if (!tabId) throw new Error("Missing tabId");
  if (!data || typeof data !== "object") throw new Error("Missing session data");

  const serviceUrl = data.service_url;
  if (!serviceUrl) throw new Error("Missing service_url in JSON");

  const cookies = Array.isArray(data.cookies) ? data.cookies : [];
  const localStorageObj =
    typeof data.localStorage === "string" ? JSON.parse(data.localStorage || "{}") : (data.localStorage || {});
  const sessionStorageObj =
    typeof data.sessionStorage === "string" ? JSON.parse(data.sessionStorage || "{}") : (data.sessionStorage || {});

  const activeTab = await ext.tabs.get(tabId);
  const activeOrigin = new URL(activeTab.url).origin;
  const serviceOrigin = new URL(serviceUrl).origin;

  // Important: same origin as manual workflow
  if (activeOrigin !== serviceOrigin) {
    throw new Error(`Open exactly this origin first: ${serviceOrigin}`);
  }

  // 1) inject storage in current tab (no reload)
  await ext.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: (ls, ss) => {
      for (const [k, v] of Object.entries(ls || {})) localStorage.setItem(k, String(v));
      for (const [k, v] of Object.entries(ss || {})) sessionStorage.setItem(k, String(v));
      return {
        lsCount: localStorage.length,
        ssCount: sessionStorage.length,
        href: location.href
      };
    },
    args: [localStorageObj, sessionStorageObj],
  });

  // 2) inject cookies (2 passes like manual robustness)
  const delay = Number(opts.delayBetweenCookies ?? 80);
  for (let pass = 0; pass < 2; pass++) {
    for (const c of cookies) {
      try {
        await setOneCookie(serviceOrigin + "/", {
          ...c,
          expires: c.expires ?? c.expirationDate
        });
      } catch (e1) {
        // fallback: retry without domain
        try {
          const c2 = { ...c };
          delete c2.domain;
          await setOneCookie(serviceOrigin + "/", c2);
        } catch (e2) {
          console.warn("[FORCE] cookie failed:", c?.name, e2);
        }
      }
      await sleep(delay);
    }
  }

  // 3) verify readback
  const injectedNow = await ext.cookies.getAll({ url: serviceOrigin + "/" });
  console.log("[FORCE] cookies now:", injectedNow.map(c => c.name));

  // No reload, no navigation => exactly like your manual sequence
  return { ok: true, cookieCount: injectedNow.length };
}

























































