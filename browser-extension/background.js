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
  const path = cookie.path || "/";
  return `${u.protocol}//${domain}${path}`;
}

async function injectCookies(serviceUrl, cookies) {
  for (const c of cookies) {
    const details = {
      url: buildCookieSetUrl(c, serviceUrl),
      name: c.name,
      value: c.value,
      path: c.path || "/",
      httpOnly: !!c.httpOnly,
      secure: true, // if target is https, enforce secure
      sameSite: normalizeSameSite(c.sameSite)
    };

    if (c.domain) details.domain = c.domain;

    // -1 => session cookie, do not set expirationDate
    if (typeof c.expires === "number" && c.expires > 0) {
      details.expirationDate = c.expires;
    }

    await chrome.cookies.set(details);
  }
}



chrome.action.onClicked.addListener(async () => {
  const { handoffUrl } = await chrome.storage.local.get(["handoffUrl"]);
  if (!handoffUrl) {
    console.error("No handoffUrl in storage. Save it first via console.");
    return;
  }
  try {
    await doHandoff(handoffUrl);
    console.log("Handoff success");
  } catch (e) {
    console.error("Handoff failed:", e);
  }
});



async function doHandoff(handoffUrl) {
  const data = await fetchJson(handoffUrl);
  const serviceUrl = data.service_url;
  const cookies = data.cookies || [];
  const localStorageData = data.local_storage;

  if (!serviceUrl) throw new Error("handoff response missing service_url");

  // Inject cookies
  for (const c of cookies) {
    const details = {
      url: buildCookieSetUrl(c, serviceUrl),
      name: c.name,
      value: c.value,
      path: c.path || "/",
      httpOnly: !!c.httpOnly,
      secure: true,
      sameSite: normalizeSameSite(c.sameSite)
    };
    if (c.domain) details.domain = c.domain;
    if (typeof c.expires === "number" && c.expires > 0) {
      details.expirationDate = c.expires;
    }
    await chrome.cookies.set(details);
  }

  // Open tab and inject localStorage
  chrome.tabs.create({ url: serviceUrl }, (tab) => {
    if (localStorageData) {
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: (data) => {
          const parsed = JSON.parse(data);
          for (const [key, value] of Object.entries(parsed)) {
            window.localStorage.setItem(key, value);
          }
        },
        args: [localStorageData]
      });
    }
  });
}