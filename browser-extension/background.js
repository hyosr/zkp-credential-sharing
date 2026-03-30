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
    secure: !!c.secure, // ✅ do not force true
    sameSite: normalizeSameSite(c.sameSite),
  };

  const domain = forcedDomain || c.domain;
  if (domain) details.domain = domain;

  // Session cookie: expires = -1, do not set expirationDate
  if (typeof c.expires === "number" && c.expires > 0) {
    details.expirationDate = c.expires;
  }

  await chrome.cookies.set(details);
}

async function injectCookies(serviceUrl, cookies) {
  for (const c of cookies) {
    // normal injection
    await setOneCookie(serviceUrl, c);

    // OPTIONAL: if token cookie is set for recolyse.com, also set for .recolyse.com
    // (some apps read cookie on the dot-domain)
    if (c.name === "token" && c.domain === "recolyse.com") {
      await setOneCookie(serviceUrl, c, ".recolyse.com");
    }
  }
}

async function injectStorageAndReload(tabId, localStorageObj, sessionStorageObj) {
  await chrome.scripting.executeScript({
    target: { tabId },
    func: (ls, ss) => {
      try {
        if (ls && typeof ls === "object") {
          for (const [k, v] of Object.entries(ls)) {
            window.localStorage.setItem(k, v);
          }
        }
        if (ss && typeof ss === "object") {
          for (const [k, v] of Object.entries(ss)) {
            window.sessionStorage.setItem(k, v);
          }
        }
      } catch (e) {
        console.error("Storage injection error:", e);
      }
    },
    args: [localStorageObj, sessionStorageObj],
  });

  // Reload so SPA rehydrates using localStorage (persist:root/token/user)
  await new Promise((resolve) => setTimeout(resolve, 800));
  await chrome.tabs.reload(tabId);
}

async function doHandoff(handoffUrl) {
  const data = await fetchJson(handoffUrl);

  const serviceUrl = data.service_url;
  const currentUrl = data.current_url || serviceUrl;

  const cookies = data.cookies || [];
  const localStorageObj = safeParseJsonObject(data.localStorage);
  const sessionStorageObj = safeParseJsonObject(data.sessionStorage);

  if (!serviceUrl) throw new Error("handoff response missing service_url");

  // 1) Inject cookies first
  await injectCookies(serviceUrl, cookies);

  // 2) Open the destination page (current_url should be the logged-in landing page)
  chrome.tabs.create({ url: currentUrl }, async (tab) => {
    try {
      // Wait a moment for the document to be ready enough to accept scripting
      await new Promise((resolve) => setTimeout(resolve, 700));

      // 3) Inject storages + reload
      await injectStorageAndReload(tab.id, localStorageObj, sessionStorageObj);

      // 4) Optional: focus the tab
      chrome.tabs.update(tab.id, { active: true });
    } catch (e) {
      console.error("Tab injection failed:", e);
    }
  });
}

// Click on extension icon triggers handoff
chrome.action.onClicked.addListener(async () => {
  const { handoffUrl } = await chrome.storage.local.get(["handoffUrl"]);
  if (!handoffUrl) {
    console.error("No handoffUrl in storage. Save it first via console.");
    return;
  }
  try {
    console.log("Using handoffUrl:", handoffUrl);
    await doHandoff(handoffUrl);
    console.log("Handoff success");
  } catch (e) {
    console.error("Handoff failed:", e);
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

// // function buildCookieSetUrl(cookie, serviceUrl) {
// //   const u = new URL(serviceUrl);
// //   const domain = (cookie.domain || u.hostname).replace(/^\./, "");
// //   const path = cookie.path || "/";
// //   return `${u.protocol}//${domain}${path}`;
// // }




// function buildCookieSetUrl(cookie, serviceUrl) {
//   const u = new URL(serviceUrl);
//   const domain = (cookie.domain || u.hostname).replace(/^\./, "");
//   return `${u.protocol}//${domain}/`;   // ✅ always root
// }





// async function setCookies(serviceUrl, cookies) {
//   for (const c of cookies) {
//     const details = {
//       url: buildCookieSetUrl(c, serviceUrl),
//       name: c.name,
//       value: c.value,
//       path: c.path || "/",
//       httpOnly: !!c.httpOnly,
//       secure: !!c.secure,
//       sameSite: normalizeSameSite(c.sameSite),
//     };

//     if (c.domain) details.domain = c.domain;
//     if (typeof c.expires === "number" && c.expires > 0) {
//       details.expirationDate = c.expires;
//     }

//     await chrome.cookies.set(details);
//   }
// }

// function parseStorageJson(s) {
//   if (!s) return null;
//   try {
//     return JSON.parse(s);
//   } catch {
//     return null;
//   }
// }

// // async function doHandoff(handoffUrl) {
// //   const data = await fetchJson(handoffUrl);

// //   const serviceUrl = data.service_url;
// //   const currentUrl = data.current_url || serviceUrl;
// //   const cookies = data.cookies || [];
// //   const localStorageData = data.localStorage;       // ✅ camelCase
// //   const sessionStorageData = data.sessionStorage;   // ✅ camelCase

// //   if (!serviceUrl) throw new Error("handoff response missing service_url");

// //   // 1) set cookies first
// //   await setCookies(serviceUrl, cookies);

// //   // 2) open tab, then inject storages
// //   chrome.tabs.create({ url: currentUrl }, (tab) => {
// //     const ls = parseStorageJson(localStorageData);
// //     const ss = parseStorageJson(sessionStorageData);

// //     if (!ls && !ss) return;

// //     chrome.scripting.executeScript({
// //       target: { tabId: tab.id },
// //       func: (localObj, sessionObj) => {
// //         if (localObj) {
// //           for (const [k, v] of Object.entries(localObj)) {
// //             window.localStorage.setItem(k, v);
// //           }
// //         }
// //         if (sessionObj) {
// //           for (const [k, v] of Object.entries(sessionObj)) {
// //             window.sessionStorage.setItem(k, v);
// //           }
// //         }
// //       },
// //       args: [ls, ss],
// //     });


// //     setTimeout(() => {
// //     chrome.tabs.reload(tab.id);
// //   }, 800);


// //   });
// // }





// async function doHandoff(handoffUrl) {
//   const data = await fetchJson(handoffUrl);
//   const serviceUrl = data.service_url;
//   const currentUrl = data.current_url || serviceUrl;
//   const cookies = data.cookies || [];

//   // keys from backend (camelCase)
//   const localStorageData = data.localStorage;
//   const sessionStorageData = data.sessionStorage;

//   if (!serviceUrl) throw new Error("handoff response missing service_url");

//   // 1) Inject cookies first
//   for (const c of cookies) {
//     const details = {
//       url: buildCookieSetUrl(c, serviceUrl),
//       name: c.name,
//       value: c.value,
//       path: c.path || "/",
//       httpOnly: !!c.httpOnly,
//       secure: !!c.secure,
//       sameSite: normalizeSameSite(c.sameSite)
//     };
//     if (c.domain) details.domain = c.domain;
//     if (typeof c.expires === "number" && c.expires > 0) details.expirationDate = c.expires;
//     await chrome.cookies.set(details);
//   }

//   // ✅ PUT YOUR FALLBACK RIGHT HERE (after cookies + after localStorageData is read)
//   let lsToInject = localStorageData;
//   if (!lsToInject) {
//     const tokenCookie = cookies.find(c => c.name === "token" && c.value);
//     if (tokenCookie) {
//       lsToInject = JSON.stringify({ token: tokenCookie.value });
//     }
//   }

//   // 2) Open tab, inject storage, reload
//   chrome.tabs.create({ url: currentUrl }, (tab) => {
//     chrome.scripting.executeScript({
//       target: { tabId: tab.id },
//       func: (lsJson, ssJson) => {
//         try {
//           if (lsJson) {
//             const ls = JSON.parse(lsJson);
//             for (const [k, v] of Object.entries(ls)) localStorage.setItem(k, v);
//           }
//           if (ssJson) {
//             const ss = JSON.parse(ssJson);
//             for (const [k, v] of Object.entries(ss)) sessionStorage.setItem(k, v);
//           }
//         } catch (e) {
//           console.error("Storage injection error:", e);
//         }
//       },
//       // ✅ use lsToInject instead of localStorageData
//       args: [lsToInject, sessionStorageData],
//     });

//     setTimeout(() => chrome.tabs.reload(tab.id), 800);
//   });
// }










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

