async function fetchJson(url) {
  const r = await fetch(url, { method: "GET" });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`Fetch failed ${r.status}: ${t}`);
  }
  return await r.json();
}

function normalizeSameSite(v) {
  if (!v) return "lax";
  const s = String(v).toLowerCase();
  if (s === "strict") return "strict";
  if (s === "none") return "no_restriction";
  return "lax";
}

function buildCookieSetUrl(cookie, serviceUrl) {
  const u = new URL(serviceUrl);
  const domain = (cookie.domain || u.hostname).replace(/^\./, "");
  return `${u.protocol}//${domain}/`;
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

async function setOneCookie(serviceUrl, c, forcedDomain = null) {
  const details = {
    url: buildCookieSetUrl(c, serviceUrl),
    name: c.name,
    value: c.value,
    path: c.path || "/",
    httpOnly: !!c.httpOnly,
    secure: !!c.secure,
    sameSite: normalizeSameSite(c.sameSite),
  };

  const domain = forcedDomain || c.domain;
  if (domain) details.domain = domain;

  if (typeof c.expires === "number" && c.expires > 0) {
    details.expirationDate = c.expires;
  }

  await chrome.cookies.set(details);
}

async function injectCookies(serviceUrl, cookies) {
  for (const c of cookies) {
    await setOneCookie(serviceUrl, c);

    // Optional: also set token cookie for .recolyse.com
    if (c.name === "token" && c.domain === "recolyse.com") {
      await setOneCookie(serviceUrl, c, ".recolyse.com");
    }

    if (!Array.isArray(cookies)) cookies = [];
    for (const c of cookies) {
      await setOneCookie(serviceUrl, c);

      // Optional: also set token cookie for .recolyse.com
      if (c.name === "token" && c.domain === "recolyse.com") {
        await setOneCookie(serviceUrl, c, ".recolyse.com");
    }
  }
  }
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

  await new Promise((resolve) => setTimeout(resolve, 800));
  await chrome.tabs.reload(tabId);
}

async function doHandoff(handoffUrl) {
  const data = await fetchJson(handoffUrl);

  const serviceUrl = data.service_url;
  const currentUrl = data.current_url || serviceUrl;
  // const cookies = data.cookies || [];


  const cookies = Array.isArray(data.cookies) ? data.cookies : [];


  const localStorageObj = safeParseJsonObject(data.localStorage);
  const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

  if (!serviceUrl) throw new Error("handoff response missing service_url");

  // Inject cookies first
  await injectCookies(serviceUrl, cookies);

  // Open tab, inject storage, reload
  await new Promise((resolve) => {
    chrome.tabs.create({ url: currentUrl }, async (tab) => {
      try {
        await new Promise((r) => setTimeout(r, 700));
        await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);
        chrome.tabs.update(tab.id, { active: true });
        resolve();
      } catch (e) {
        console.error(e);
        resolve();
      }
    });
  });
}

// Receive message from popup
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "RUN_HANDOFF") {
    (async () => {
      try {
        const url = msg.handoffUrl || (await chrome.storage.local.get(["handoffUrl"])).handoffUrl;
        if (!url) throw new Error("Missing handoffUrl");
        await doHandoff(url);
        sendResponse({ ok: true });
      } catch (e) {
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true; // keep message channel open for async response
  }
});









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
//     await chrome.storage.local.set({ handoffUrl });
//     await doHandoff(handoffUrl);

//     // Close the bridge tab (optional)
//     chrome.tabs.remove(details.tabId);
//   } catch (e) {
//     console.error("Bridge handoff failed:", e);
//   }
// });







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
  if (details.frameId !== 0) return; // top frame only
  if (!isBridgeUrl(details.url)) return;

  const u = new URL(details.url);
  const handoffUrl = u.searchParams.get("handoff");

  try {
    // Save for history and run immediately
    await chrome.storage.local.set({ handoffUrl });
    await doHandoff(handoffUrl);

    // optional: close the bridge tab
    chrome.tabs.remove(details.tabId);
  } catch (e) {
    console.error("Bridge handoff failed:", e);
  }
});

























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
//     secure: !!c.secure, // ✅ do not force true
//     sameSite: normalizeSameSite(c.sameSite),
//   };

//   const domain = forcedDomain || c.domain;
//   if (domain) details.domain = domain;

//   // Session cookie: expires = -1, do not set expirationDate
//   if (typeof c.expires === "number" && c.expires > 0) {
//     details.expirationDate = c.expires;
//   }

//   await chrome.cookies.set(details);
// }

// async function injectCookies(serviceUrl, cookies) {
//   for (const c of cookies) {
//     // normal injection
//     await setOneCookie(serviceUrl, c);

//     // OPTIONAL: if token cookie is set for recolyse.com, also set for .recolyse.com
//     // (some apps read cookie on the dot-domain)
//     if (c.name === "token" && c.domain === "recolyse.com") {
//       await setOneCookie(serviceUrl, c, ".recolyse.com");
//     }
//   }
// }

// async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
//   await chrome.scripting.executeScript({
//     target: { tabId },
//     func: (ls, ss) => {
//       try {
//         if (ls && typeof ls === "object") {
//           for (const [k, v] of Object.entries(ls)) {
//             window.localStorage.setItem(k, v);
//           }
//         }
//         if (ss && typeof ss === "object") {
//           for (const [k, v] of Object.entries(ss)) {
//             window.sessionStorage.setItem(k, v);
//           }
//         }
//       } catch (e) {
//         console.error("Storage injection error:", e);
//       }
//     },
//     args: [localStorageObj, sessionStorageObj],
//   });

//   // Reload so SPA rehydrates using localStorage (persist:root/token/user)
//   await new Promise((resolve) => setTimeout(resolve, 800));
//   await chrome.tabs.reload(tabId);
// }

// async function doHandoff(handoffUrl) {
//   const data = await fetchJson(handoffUrl);

//   const serviceUrl = data.service_url;
//   const currentUrl = data.current_url || serviceUrl;

//   const cookies = data.cookies || [];
//   const localStorageObj = safeParseJsonObject(data.localStorage);
//   const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

//   if (!serviceUrl) throw new Error("handoff response missing service_url");

//   // 1) Inject cookies first
//   await injectCookies(serviceUrl, cookies);

//   // 2) Open the destination page (current_url should be the logged-in landing page)
//   chrome.tabs.create({ url: currentUrl }, async (tab) => {
//     try {
//       // Wait a moment for the document to be ready enough to accept scripting
//       await new Promise((resolve) => setTimeout(resolve, 700));

//       // 3) Inject storages + reload
//       await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);

//       // 4) Optional: focus the tab
//       chrome.tabs.update(tab.id, { active: true });
//     } catch (e) {
//       console.error("Tab injection failed:", e);
//     }
//   });
// }

// // Click on extension icon triggers handoff
// chrome.action.onClicked.addListener(async () => {
//   const { handoffUrl } = await chrome.storage.local.get(["handoffUrl"]);
//   if (!handoffUrl) {
//     console.error("No handoffUrl in storage. Save it first via console.");
//     return;
//   }
//   try {
//     console.log("Using handoffUrl:", handoffUrl);
//     await doHandoff(handoffUrl);
//     console.log("Handoff success");
//   } catch (e) {
//     console.error("Handoff failed:", e);
//   }
// });























