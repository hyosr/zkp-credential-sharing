// popup.js - ZKP Credential Sharing Extension

const DEFAULT_BASE = "http://localhost:8001";

// ---------- Helper functions ----------
function normalizeBaseUrl(input) {
  const v = (input || "").trim().replace(/\/+$/, "");
  if (!v) return DEFAULT_BASE;
  return v;
}

function normalizeToHandoffUrl(baseUrl, input) {
  const v = (input || "").trim();
  if (!v) return null;
  if (v.startsWith("http://") || v.startsWith("https://")) return v;
  return `${baseUrl}/sharing/handoff/${encodeURIComponent(v)}`;
}

function setStatus(el, msg, kind = "") {
  el.textContent = msg || "";
  el.classList.remove("ok", "err");
  if (kind) el.classList.add(kind);
}

function setLoading(btn, spinner, goText, isLoading) {
  btn.disabled = !!isLoading;
  spinner.classList.toggle("hidden", !isLoading);
  goText.textContent = isLoading ? "Connecting..." : "Connect";
}

function setLoading2(btn, spinner, textEl, isLoading, loadingText, idleText) {
  btn.disabled = !!isLoading;
  spinner.classList.toggle("hidden", !isLoading);
  textEl.textContent = isLoading ? loadingText : idleText;
}

// ---------- DOM elements ----------
let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
let delayBetweenCookiesEl, delayAfterInjectEl;
let jwtEl, modeHandoffEl, modeAssistedEl, assistedBox, assistedTokenEl, assistedStartBtn, assistedSpinner, assistedStartText;
let finishBtn;

// ---------- Run handoff injection ----------
async function runHandoff() {
  const baseUrl = normalizeBaseUrl(baseUrlEl.value);
  const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl.value);
  const opts = {
    delayBetweenCookies: Number(delayBetweenCookiesEl.value || 0),
    delayAfterInject: Number(delayAfterInjectEl.value || 0),
  };

  if (!handoffUrl) {
    setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
    return;
  }

  setLoading(goBtn, spinner, goText, true);
  setStatus(statusEl, "");

  await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts });
  await chrome.storage.local.set({ jwt: (jwtEl.value || "").trim() });

  chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {
    setLoading(goBtn, spinner, goText, false);
    if (chrome.runtime.lastError) {
      setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
      return;
    }
    if (!resp || !resp.ok) {
      setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
      return;
    }
    setStatus(statusEl, "Success. Opening connected profile…", "ok");
    setTimeout(() => window.close(), 450);
  });
}

// ---------- Start assisted flow (recipient) ----------
async function startAssisted() {
  const baseUrl = normalizeBaseUrl(baseUrlEl.value);
  const jwt = (jwtEl.value || "").trim();
  const token = (assistedTokenEl.value || "").trim();

  if (!jwt) {
    setStatus(statusEl, "Paste Backend JWT first.", "err");
    return;
  }
  if (!token) {
    setStatus(statusEl, "Paste the share token to request owner approval.", "err");
    return;
  }

  setStatus(statusEl, "");
  setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, true, "Requesting...", "Request owner approval");

  await chrome.storage.local.set({ baseUrl, jwt, assistedToken: token });

  chrome.runtime.sendMessage(
    { type: "ASSISTED_START", baseUrl, jwt, shareToken: token },
    (resp) => {
      setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, false, "", "Request owner approval");
      if (chrome.runtime.lastError) {
        setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
        return;
      }
      if (!resp || !resp.ok) {
        setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
        return;
      }
      setStatus(statusEl, "Request sent. Waiting for owner approval…", "ok");
    }
  );
}

// ---------- Owner: finish manual login and capture session ----------
async function finishOwnerCapture() {
  const { pendingCaptureRequestId, pendingCaptureTabId, pendingServiceUrl } = await chrome.storage.local.get([
    "pendingCaptureRequestId", "pendingCaptureTabId", "pendingServiceUrl"
  ]);
  if (!pendingCaptureRequestId) {
    alert("No pending capture request. Did you approve a request?");
    return;
  }
  chrome.runtime.sendMessage({
    type: "CAPTURE_SESSION",
    tabId: pendingCaptureTabId,
    serviceUrl: pendingServiceUrl,
    requestId: pendingCaptureRequestId
  }, (response) => {
    if (response && response.ok) {
      alert("Session captured and sent to recipient!");
      window.close();
    } else {
      alert("Error: " + (response?.error || "unknown"));
    }
  });
}

// ---------- Update UI for handoff/assisted mode ----------
function updateModeUI() {
  if (!modeHandoffEl || !modeAssistedEl || !assistedBox) return;
  assistedBox.style.display = modeAssistedEl.checked ? "block" : "none";
}

// ---------- Pending requests for owner ----------
async function loadPendingRequests() {
  const baseUrl = normalizeBaseUrl(baseUrlEl.value);
  const jwt = (jwtEl.value || "").trim();
  const container = document.getElementById("pendingList");
  if (!baseUrl || !jwt) {
    container.innerText = "Set Backend URL and JWT first.";
    return;
  }
  try {
    const response = await fetch(`${baseUrl}/sharing/assisted/pending`, {
      headers: { Authorization: `Bearer ${jwt}` }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const pending = await response.json();
    if (!pending.length) {
      container.innerText = "No pending requests.";
      return;
    }
    container.innerHTML = pending.map(req => `
      <div class="request-item" style="border:1px solid #ccc; border-radius:8px; padding:8px; margin-bottom:8px;">
        <div><strong>${req.service_url}</strong></div>
        <div>Recipient: ${req.recipient_email}</div>
        <div>Expires: ${new Date(req.expires_at * 1000).toLocaleString()}</div>
        <button class="approveBtn" data-id="${req.request_id}" data-url="${req.service_url}" style="margin-top:6px;">Approve & assist</button>
      </div>
    `).join("");
    document.querySelectorAll(".approveBtn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const requestId = btn.dataset.id;
        const serviceUrl = btn.dataset.url;
        await approveRequest(requestId, serviceUrl);
      });
    });
  } catch (err) {
    console.error(err);
    container.innerText = "Error loading requests.";
  }
}

async function approveRequest(requestId, serviceUrl) {
  const baseUrl = normalizeBaseUrl(baseUrlEl.value);
  const jwt = (jwtEl.value || "").trim();
  if (!baseUrl || !jwt) {
    alert("Missing Backend URL or JWT");
    return;
  }
  try {
    const response = await fetch(`${baseUrl}/sharing/assisted/${requestId}/approve`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${jwt}`
      },
      body: JSON.stringify({})
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const resp = await response.json();
    if (!resp.assist_login_url) throw new Error("Missing assist_login_url");

    const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });
    await chrome.storage.local.set({
      pendingCaptureRequestId: requestId,
      pendingCaptureTabId: tab.id,
      pendingServiceUrl: serviceUrl
    });
    chrome.action.setBadgeText({ text: "!" });
    alert("Approved! Login page opened. After manual login, click 'I finished login'.");
  } catch (err) {
    console.error(err);
    alert("Approval failed: " + err.message);
  }
}

// ---------- Auto-refresh pending list ----------
let pendingInterval = null;
function startPendingPolling() {
  if (pendingInterval) clearInterval(pendingInterval);
  pendingInterval = setInterval(() => {
    loadPendingRequests().catch(console.error);
  }, 5000);
}

// ---------- DOMContentLoaded ----------
document.addEventListener("DOMContentLoaded", async () => {
  // Get elements
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
  modeHandoffEl = document.getElementById("modeHandoff");
  modeAssistedEl = document.getElementById("modeAssisted");
  assistedBox = document.getElementById("assistedBox");
  assistedTokenEl = document.getElementById("assistedToken");
  assistedStartBtn = document.getElementById("assistedStart");
  assistedSpinner = document.getElementById("assistedSpinner");
  assistedStartText = document.getElementById("assistedStartText");
  finishBtn = document.getElementById("finishLogin");

  // Load saved settings
  const saved = await chrome.storage.local.get([
    "handoffUrl", "baseUrl", "delayBetweenCookies", "delayAfterInject", "jwt", "assistedToken"
  ]);
  if (saved.baseUrl) baseUrlEl.value = saved.baseUrl;
  if (saved.delayBetweenCookies) delayBetweenCookiesEl.value = saved.delayBetweenCookies;
  if (saved.delayAfterInject) delayAfterInjectEl.value = saved.delayAfterInject;
  if (saved.jwt) jwtEl.value = saved.jwt;
  if (saved.assistedToken) assistedTokenEl.value = saved.assistedToken;
  if (saved.handoffUrl) {
    const m = saved.handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
    tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
  }

  // Event listeners
  goBtn.addEventListener("click", runHandoff);
  clearBtn.addEventListener("click", async () => {
    tokenEl.value = "";
    await chrome.storage.local.remove(["handoffUrl"]);
    setStatus(statusEl, "Cleared saved token.", "ok");
  });
  tokenEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      runHandoff();
    }
  });
  if (assistedStartBtn) assistedStartBtn.addEventListener("click", startAssisted);
  if (finishBtn) finishBtn.addEventListener("click", finishOwnerCapture);
  if (modeHandoffEl && modeAssistedEl) {
    modeHandoffEl.addEventListener("change", updateModeUI);
    modeAssistedEl.addEventListener("change", updateModeUI);
    updateModeUI();
  }








  // Sauvegarde automatique des champs
  baseUrlEl.addEventListener('change', () => {
    const val = normalizeBaseUrl(baseUrlEl.value);
    chrome.storage.local.set({ baseUrl: val });
    // Redémarrer le polling avec les nouvelles valeurs
    chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: val, jwt: jwtEl.value.trim() });
  });
  jwtEl.addEventListener('change', () => {
    const val = jwtEl.value.trim();
    chrome.storage.local.set({ jwt: val });
    chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: normalizeBaseUrl(baseUrlEl.value), jwt: val });
  });


  if (saved.baseUrl && saved.jwt) {
  chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: saved.baseUrl, jwt: saved.jwt });
}





  baseUrlEl.addEventListener('change', async () => {
    const val = normalizeBaseUrl(baseUrlEl.value);
    await chrome.storage.local.set({ baseUrl: val });
    chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: val, jwt: jwtEl.value.trim() });
    loadPendingRequests(); // rafraîchir la liste
  });

  jwtEl.addEventListener('change', async () => {
    const val = jwtEl.value.trim();
    await chrome.storage.local.set({ jwt: val });
    chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: normalizeBaseUrl(baseUrlEl.value), jwt: val });
    loadPendingRequests();
  });









  // Start polling for pending requests (owner side)
  startPendingPolling();
  // Initial load
  loadPendingRequests();
});



































// // popup.js - ZKP Credential Sharing Extension

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
//   // If user pasted full URL
//   if (v.startsWith("http://") || v.startsWith("https://")) return v;
//   // Otherwise treat it as session_id
//   return `${baseUrl}/sharing/handoff/${encodeURIComponent(v)}`;
// }

// function setStatus(el, msg, kind = "") {
//   el.textContent = msg || "";
//   el.classList.remove("ok", "err");
//   if (kind) el.classList.add(kind);
// }

// function setLoading(btn, spinner, goText, isLoading) {
//   btn.disabled = !!isLoading;
//   spinner.classList.toggle("hidden", !isLoading);
//   goText.textContent = isLoading ? "Connecting..." : "Connect";
// }

// function setLoading2(btn, spinner, textEl, isLoading, loadingText, idleText) {
//   btn.disabled = !!isLoading;
//   spinner.classList.toggle("hidden", !isLoading);
//   textEl.textContent = isLoading ? loadingText : idleText;
// }

// // ---------- DOM elements (declare once) ----------
// let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
// let delayBetweenCookiesEl, delayAfterInjectEl;
// let jwtEl, modeHandoffEl, modeAssistedEl, assistedBox, assistedTokenEl, assistedStartBtn, assistedSpinner, assistedStartText;
// let finishBtn;

// // ---------- Run handoff injection ----------
// async function runHandoff() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//   const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl.value);
//   const opts = {
//     delayBetweenCookies: Number(delayBetweenCookiesEl.value || 0),
//     delayAfterInject: Number(delayAfterInjectEl.value || 0),
//   };

//   if (!handoffUrl) {
//     setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
//     return;
//   }

//   setLoading(goBtn, spinner, goText, true);
//   setStatus(statusEl, "");

//   await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts });
//   await chrome.storage.local.set({ jwt: (jwtEl.value || "").trim() });

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
//   const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//   const jwt = (jwtEl.value || "").trim();
//   const token = (assistedTokenEl.value || "").trim();

//   if (!jwt) {
//     setStatus(statusEl, "Paste Backend JWT first.", "err");
//     return;
//   }
//   if (!token) {
//     setStatus(statusEl, "Paste the share token to request owner approval.", "err");
//     return;
//   }

//   setStatus(statusEl, "");
//   setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, true, "Requesting...", "Request owner approval");

//   await chrome.storage.local.set({ baseUrl, jwt, assistedToken: token });

//   chrome.runtime.sendMessage(
//     { type: "ASSISTED_START", baseUrl, jwt, shareToken: token },
//     (resp) => {
//       setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, false, "", "Request owner approval");
//       if (chrome.runtime.lastError) {
//         setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
//         return;
//       }
//       if (!resp || !resp.ok) {
//         setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
//         return;
//       }
//       setStatus(statusEl, "Request sent. Waiting for owner approval…", "ok");
//     }
//   );
// }

// // ---------- Owner: finish manual login and capture session ----------
// async function finishOwnerCapture() {
//   const { pendingCaptureRequestId, pendingCaptureTabId } = await chrome.storage.local.get([
//     "pendingCaptureRequestId", "pendingCaptureTabId"
//   ]);
//   if (!pendingCaptureRequestId) {
//     alert("No pending capture request. Did you approve a request?");
//     return;
//   }
//   // Lancer la capture (background.js doit avoir la fonction captureCurrentSession)
//   chrome.runtime.sendMessage({ type: "CAPTURE_SESSION" }, (response) => {
//     if (response && response.ok) {
//       alert("Session captured and sent to recipient!");
//       window.close();
//     } else {
//       alert("Error: " + (response?.error || "unknown"));
//     }
//   });
// }

// // ---------- Update UI for handoff/assisted mode ----------
// function updateModeUI() {
//   if (!modeHandoffEl || !modeAssistedEl || !assistedBox) return;
//   assistedBox.style.display = modeAssistedEl.checked ? "block" : "none";
// }

// // ---------- DOMContentLoaded ----------
// document.addEventListener("DOMContentLoaded", async () => {
//   // Get all elements
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

//   // Load saved settings
//   const saved = await chrome.storage.local.get([
//     "handoffUrl", "baseUrl", "delayBetweenCookies", "delayAfterInject", "jwt", "assistedToken"
//   ]);

//   if (saved.baseUrl) baseUrlEl.value = saved.baseUrl;
//   if (saved.delayBetweenCookies) delayBetweenCookiesEl.value = saved.delayBetweenCookies;
//   if (saved.delayAfterInject) delayAfterInjectEl.value = saved.delayAfterInject;
//   if (saved.jwt) jwtEl.value = saved.jwt;
//   if (saved.assistedToken) assistedTokenEl.value = saved.assistedToken;

//   if (saved.handoffUrl) {
//     const m = saved.handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
//     tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
//   }

//   // Event listeners
//   goBtn.addEventListener("click", runHandoff);
//   clearBtn.addEventListener("click", async () => {
//     tokenEl.value = "";
//     await chrome.storage.local.remove(["handoffUrl"]);
//     setStatus(statusEl, "Cleared saved token.", "ok");
//   });
//   tokenEl.addEventListener("keydown", (e) => {
//     if (e.key === "Enter" && !e.shiftKey) {
//       e.preventDefault();
//       runHandoff();
//     }
//   });

//   if (assistedStartBtn) {
//     assistedStartBtn.addEventListener("click", startAssisted);
//   }
//   if (finishBtn) {
//     finishBtn.addEventListener("click", finishOwnerCapture);
//   }
//   if (modeHandoffEl && modeAssistedEl) {
//     modeHandoffEl.addEventListener("change", updateModeUI);
//     modeAssistedEl.addEventListener("change", updateModeUI);
//     updateModeUI();
//   }
// });





// // ---------- Pending requests for owner ----------
// async function loadPendingRequests() {
//   const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//   const jwt = (jwtEl.value || "").trim();
//   if (!baseUrl || !jwt) {
//     document.getElementById("pendingList").innerText = "Set Backend URL and JWT first.";
//     return;
//   }
//   try {
//     const response = await fetch(`${baseUrl}/sharing/assisted/pending`, {
//       headers: { Authorization: `Bearer ${jwt}` }
//     });
//     if (!response.ok) throw new Error(`HTTP ${response.status}`);
//     const pending = await response.json();
//     const container = document.getElementById("pendingList");
//     if (!pending.length) {
//       container.innerText = "No pending requests.";
//       return;
//     }
//     container.innerHTML = pending.map(req => `
//       <div class="request-item" style="border:1px solid #ccc; border-radius:8px; padding:8px; margin-bottom:8px;">
//         <div><strong>${req.service_url}</strong></div>
//         <div>Recipient: ${req.recipient_email}</div>
//         <div>Expires: ${new Date(req.expires_at * 1000).toLocaleString()}</div>
//         <button class="approveBtn" data-id="${req.request_id}" style="margin-top:6px;">Approve & assist</button>
//       </div>
//     `).join("");
//     // Attach event listeners to approve buttons
//     document.querySelectorAll(".approveBtn").forEach(btn => {
//       btn.addEventListener("click", async () => {
//         const requestId = btn.dataset.id;
//         await approveRequest(requestId);
//       });
//     });
//   } catch (err) {
//     console.error(err);
//     document.getElementById("pendingList").innerText = "Error loading requests.";
//   }
// }

// async function approveRequest(requestId) {
//   const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//   const jwt = (jwtEl.value || "").trim();
//   if (!baseUrl || !jwt) {
//     alert("Missing Backend URL or JWT");
//     return;
//   }
//   try {
//     const response = await fetch(`${baseUrl}/sharing/assisted/${requestId}/approve`, {
//       method: "POST",
//       headers: {
//         "Content-Type": "application/json",
//         Authorization: `Bearer ${jwt}`
//       },
//       body: JSON.stringify({})
//     });
//     if (!response.ok) throw new Error(`HTTP ${response.status}`);
//     const resp = await response.json();
//     if (!resp.assist_login_url) throw new Error("Missing assist_login_url");

//     // Store pending capture info and open login tab
//     const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });
//     await chrome.storage.local.set({
//       pendingCaptureRequestId: requestId,
//       pendingCaptureTabId: tab.id,
//       pendingServiceUrl: resp.assist_login_url
//     });
//     chrome.action.setBadgeText({ text: "!" });
//     alert("Approved! Login page opened. After manual login, click 'I finished login'.");
//   } catch (err) {
//     console.error(err);
//     alert("Approval failed: " + err.message);
//   }
// }

// // Call loadPendingRequests periodically (every 5 seconds) and on demand
// let pendingInterval = null;
// function startPendingPolling() {
//   if (pendingInterval) clearInterval(pendingInterval);
//   pendingInterval = setInterval(() => {
//     loadPendingRequests().catch(console.error);
//   }, 5000);
// }

// In DOMContentLoaded, after loading saved settings, call startPendingPolling()

































// const DEFAULT_BASE = "http://localhost:8001";

// function normalizeBaseUrl(input) {
//   const v = (input || "").trim().replace(/\/+$/, "");
//   if (!v) return DEFAULT_BASE;
//   return v;
// }

// function normalizeToHandoffUrl(baseUrl, input) {
//   const v = (input || "").trim();
//   if (!v) return null;

//   // If user pasted full URL
//   if (v.startsWith("http://") || v.startsWith("https://")) return v;

//   // Otherwise treat it as session_id
//   return `${baseUrl}/sharing/handoff/${encodeURIComponent(v)}`;
// }

// function setStatus(el, msg, kind = "") {
//   el.textContent = msg || "";
//   el.classList.remove("ok", "err");
//   if (kind) el.classList.add(kind);
// }

// function setLoading(btn, spinner, goText, isLoading) {
//   btn.disabled = !!isLoading;
//   spinner.classList.toggle("hidden", !isLoading);
//   goText.textContent = isLoading ? "Connecting..." : "Connect";
// }

// document.addEventListener("DOMContentLoaded", async () => {
//   const baseUrlEl = document.getElementById("baseUrl");
//   const tokenEl = document.getElementById("token");
  
//   const goBtn = document.getElementById("go");
//   const clearBtn = document.getElementById("clear");
//   const statusEl = document.getElementById("status");
//   const spinner = document.getElementById("spinner");
//   const goText = document.getElementById("goText");

//   // const saved = await chrome.storage.local.get(["handoffUrl", "baseUrl"]);
  
//   const saved = await chrome.storage.local.get([
//     "handoffUrl",
//     "baseUrl",
//     "delayBetweenCookies",
//     "delayAfterInject",
//   ]);





//   jwtEl.value = saved.jwt || "";
//   assistedTokenEl.value = saved.assistedToken || "";


//   baseUrlEl.value = saved.baseUrl || DEFAULT_BASE;


//   const delayBetweenCookiesEl = document.getElementById("delayBetweenCookies");
//   const delayAfterInjectEl = document.getElementById("delayAfterInject");

//   delayBetweenCookiesEl.value = saved.delayBetweenCookies ?? 150;
//   delayAfterInjectEl.value = saved.delayAfterInject ?? 800;





//   const jwtEl = document.getElementById("jwt");

//   const modeHandoffEl = document.getElementById("modeHandoff");
//   const modeAssistedEl = document.getElementById("modeAssisted");
//   const assistedBox = document.getElementById("assistedBox");

//   const assistedTokenEl = document.getElementById("assistedToken");
//   const assistedStartBtn = document.getElementById("assistedStart");
//   const assistedSpinner = document.getElementById("assistedSpinner");
//   const assistedStartText = document.getElementById("assistedStartText");


//   if (saved.handoffUrl) {
//     const m = saved.handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
//     tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
//   }

//   async function run() {
//     const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//     const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl.value);

//     const opts = {
//       delayBetweenCookies: Number(delayBetweenCookiesEl.value || 0),
//       delayAfterInject: Number(delayAfterInjectEl.value || 0),
//     };



//     if (!handoffUrl) {
//       setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
//       return;
//     }

//     setLoading(goBtn, spinner, goText, true);
//     setStatus(statusEl, "");

//     // await chrome.storage.local.set({ handoffUrl, baseUrl });


//     await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts });


//     await chrome.storage.local.set({ jwt: (jwtEl.value || "").trim() });
    
    
//     chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl, opts }, (resp) => {      setLoading(goBtn, spinner, goText, false);

//       if (chrome.runtime.lastError) {
//         setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
//         return;
//       }
//       if (!resp || !resp.ok) {
//         setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
//         return;
//       }
//       setStatus(statusEl, "Success. Opening connected profile…", "ok");
//       setTimeout(() => window.close(), 450);
//     });






    
//   }

//   goBtn.addEventListener("click", run);

//   tokenEl.addEventListener("keydown", (e) => {
//     if (e.key === "Enter" && !e.shiftKey) {
//       e.preventDefault();
//       run();
//     }
//   });

//   clearBtn.addEventListener("click", async () => {
//     tokenEl.value = "";
//     await chrome.storage.local.remove(["handoffUrl"]);
//     setStatus(statusEl, "Cleared saved token.", "ok");
//   });








//   function setLoading2(btn, spinner, textEl, isLoading, loadingText, idleText) {
//     btn.disabled = !!isLoading;
//     spinner.classList.toggle("hidden", !isLoading);
//     textEl.textContent = isLoading ? loadingText : idleText;
// }

//   async function startAssisted() {
//     const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//     const jwt = (jwtEl.value || "").trim();
//     const token = (assistedTokenEl.value || "").trim();

//     if (!jwt) {
//       setStatus(statusEl, "Paste Backend JWT first.", "err");
//       return;
//   }
//     if (!token) {
//       setStatus(statusEl, "Paste the share token to request owner approval.", "err");
//       return;
//   }

//     setStatus(statusEl, "");
//     setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, true, "Requesting...", "Request owner approval");

//     await chrome.storage.local.set({ baseUrl, jwt, assistedToken: token });

//     chrome.runtime.sendMessage(
//       { type: "ASSISTED_START", baseUrl, jwt, shareToken: token },
//       (resp) => {
//         setLoading2(assistedStartBtn, assistedSpinner, assistedStartText, false, "", "Request owner approval");

//         if (chrome.runtime.lastError) {
//           setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
//           return;
//       }
//         if (!resp || !resp.ok) {
//           setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
//           return;
//       }
//         setStatus(statusEl, "Request sent. Waiting for owner approval…", "ok");
//     }
//   );
// }

//   assistedStartBtn.addEventListener("click", startAssisted);
// });







// function updateModeUI() {
//   assistedBox.style.display = modeAssistedEl.checked ? "block" : "none";
// }
// modeHandoffEl.addEventListener("change", updateModeUI);
// modeAssistedEl.addEventListener("change", updateModeUI);
// updateModeUI();








// // popup.js

// // Assure-toi que le DOM est chargé
// document.addEventListener('DOMContentLoaded', () => {
//   const finishBtn = document.getElementById('finishLogin');
//   if (finishBtn) {
//     finishBtn.addEventListener('click', async () => {
//       // Envoyer un message au background pour déclencher la capture
//       chrome.runtime.sendMessage({ type: "CAPTURE_SESSION" }, (response) => {
//         if (response && response.ok) {
//           alert('Session capturée et envoyée au destinataire !');
//           // Optionnel : fermer la popup
//           window.close();
//         } else {
//           alert('Erreur : ' + (response?.error || 'inconnue'));
//         }
//       });
//     });
//   }
// });








// document.getElementById("finishLogin").addEventListener("click", async () => {
//   const { pendingCaptureRequestId, pendingCaptureTabId } = await chrome.storage.local.get([
//     "pendingCaptureRequestId", "pendingCaptureTabId"
//   ]);
//   if (!pendingCaptureRequestId) {
//     alert("Aucune capture en attente.");
//     return;
//   }
//   // Lancer la capture
//   const { baseUrl, jwt } = await chrome.storage.local.get(["baseUrl", "jwt"]);
//   const result = await captureCurrentSession(pendingCaptureTabId, serviceUrl, pendingCaptureRequestId);
//   // Nettoyer
//   await chrome.storage.local.remove(["pendingCaptureRequestId", "pendingCaptureTabId"]);
//   chrome.action.setBadgeText({ text: "" });
//   alert("Session envoyée au destinataire !");
// });











