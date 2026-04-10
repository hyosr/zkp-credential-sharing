// popup.js - ZKP Credential Sharing Extension
// Improvements: single DOMContentLoaded, no duplicate listeners, less inline styles reliance,
// better autosave, safer polling, clearer status messages.
// NOTE: No buttons/IDs changed.

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

function setLoading2(btn, spinner, textEl, isLoading, loadingText, idleText) {
  if (btn) btn.disabled = !!isLoading;
  if (spinner) spinner.classList.toggle("hidden", !isLoading);
  if (textEl) textEl.textContent = isLoading ? loadingText : idleText;
}

function safeText(s, max = 5000) {
  const v = String(s ?? "");
  return v.length > max ? v.slice(0, max) + "…" : v;
}

async function tryJson(resp) {
  try {
    return await resp.json();
  } catch {
    return null;
  }
}

function epochToLocal(epochSeconds) {
  if (!epochSeconds) return "—";
  try {
    return new Date(Number(epochSeconds) * 1000).toLocaleString();
  } catch {
    return String(epochSeconds);
  }
}

// ---------- DOM elements ----------
let baseUrlEl, tokenEl, goBtn, clearBtn, statusEl, spinner, goText;
let delayBetweenCookiesEl, delayAfterInjectEl;
let jwtEl, modeHandoffEl, modeAssistedEl, assistedBox, assistedTokenEl, assistedStartBtn, assistedSpinner, assistedStartText;
let finishBtn;

// owner list
let pendingListEl, refreshPendingBtn;

// timers
let pendingInterval = null;

// ---------- Run handoff injection ----------
async function runHandoff() {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl?.value);

  const opts = {
    delayBetweenCookies: Number(delayBetweenCookiesEl?.value || 0),
    delayAfterInject: Number(delayAfterInjectEl?.value || 0),
  };

  if (!handoffUrl) {
    setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
    return;
  }

  setLoading(goBtn, spinner, goText, true);
  setStatus(statusEl, "");

  await chrome.storage.local.set({ handoffUrl, baseUrl, ...opts });
  await chrome.storage.local.set({ jwt: (jwtEl?.value || "").trim() });

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
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const jwt = (jwtEl?.value || "").trim();
  const token = (assistedTokenEl?.value || "").trim();

  if (!jwt) {
    setStatus(statusEl, "Paste Backend JWT first.", "err");
    return;
  }
  if (!token) {
    setStatus(statusEl, "Paste the share token to request owner approval.", "err");
    return;
  }

  setStatus(statusEl, "");
  setLoading2(
    assistedStartBtn,
    assistedSpinner,
    assistedStartText,
    true,
    "Requesting...",
    "Request owner approval"
  );

  await chrome.storage.local.set({ baseUrl, jwt, assistedToken: token });

  chrome.runtime.sendMessage(
    { type: "ASSISTED_START", baseUrl, jwt, shareToken: token },
    (resp) => {
      setLoading2(
        assistedStartBtn,
        assistedSpinner,
        assistedStartText,
        false,
        "",
        "Request owner approval"
      );

      if (chrome.runtime.lastError) {
        setStatus(statusEl, "Error: " + chrome.runtime.lastError.message, "err");
        return;
      }
      if (!resp || !resp.ok) {
        setStatus(statusEl, "Failed: " + (resp?.error || "unknown error"), "err");
        return;
      }
      setStatus(statusEl, "Request sent. Waiting for owner approval��", "ok");
    }
  );
}

// ---------- Owner: finish manual login ----------
async function finishOwnerCapture() {
  const { baseUrl, jwt, pendingCaptureRequestId } = await chrome.storage.local.get([
    "baseUrl",
    "jwt",
    "pendingCaptureRequestId",
  ]);

  if (!baseUrl || !jwt) {
    alert("Missing baseUrl/jwt");
    return;
  }
  if (!pendingCaptureRequestId) {
    alert("No pending request id");
    return;
  }

  const r = await fetch(
    `${baseUrl}/sharing/assisted/${encodeURIComponent(pendingCaptureRequestId)}/finish`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${jwt}`,
      },
    }
  );

  if (!r.ok) {
    alert(`Finish failed: ${r.status} ${safeText(await r.text(), 1200)}`);
    return;
  }

  // We don't rely on payload; keep for debug if you want
  await tryJson(r);

  alert("Session captured and sent to recipient!");
  window.close();
}

// ---------- Update UI for handoff/assisted mode ----------
function updateModeUI() {
  if (!modeHandoffEl || !modeAssistedEl || !assistedBox) return;
  assistedBox.style.display = modeAssistedEl.checked ? "block" : "none";
}

// ---------- Pending requests for owner ----------
async function loadPendingRequests() {
  if (!pendingListEl) return;

  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const jwt = (jwtEl?.value || "").trim();

  if (!baseUrl || !jwt) {
    pendingListEl.innerText = "Set Backend URL and JWT first.";
    return;
  }

  try {
    const response = await fetch(`${baseUrl}/sharing/assisted/pending`, {
      headers: { Authorization: `Bearer ${jwt}` },
    });

    if (!response.ok) {
      const body = safeText(await response.text(), 900);
      throw new Error(`HTTP ${response.status}: ${body}`);
    }

    const pending = await response.json();

    if (!Array.isArray(pending) || pending.length === 0) {
      pendingListEl.innerText = "No pending requests.";
      return;
    }

    // Note: some backends may not return recipient_email; handle gracefully.
    pendingListEl.innerHTML = pending
      .map((req) => {
        const rid = req.request_id ?? req.id ?? "—";
        const serviceUrl = req.service_url ?? "—";
        const recipientEmail = req.recipient_email ?? "(hidden)";
        const expires = epochToLocal(req.expires_at);

        return `
          <div class="reqItem">
            <div><strong>${serviceUrl}</strong></div>
            <div class="reqMeta">Request ID: ${rid}</div>
            <div class="reqMeta">Recipient: ${recipientEmail}</div>
            <div class="reqMeta">Expires: ${expires}</div>
            <div class="reqActions">
              <button class="btn approveBtn" data-id="${rid}" data-url="${encodeURIComponent(
          serviceUrl
        )}">Approve & assist</button>
            </div>
          </div>
        `;
      })
      .join("");

    pendingListEl.querySelectorAll(".approveBtn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const requestId = btn.dataset.id;
        const serviceUrl = decodeURIComponent(btn.dataset.url || "");
        await approveRequest(requestId, serviceUrl);
      });
    });
  } catch (err) {
    console.error(err);
    pendingListEl.innerText = "Error loading requests.";
  }
}

async function approveRequest(requestId, serviceUrl) {
  const baseUrl = normalizeBaseUrl(baseUrlEl?.value);
  const jwt = (jwtEl?.value || "").trim();

  if (!baseUrl || !jwt) {
    alert("Missing Backend URL or JWT");
    return;
  }

  try {
    const response = await fetch(`${baseUrl}/sharing/assisted/${encodeURIComponent(requestId)}/approve`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${jwt}`,
      },
      body: JSON.stringify({}),
    });

    if (!response.ok) {
      const body = safeText(await response.text(), 1200);
      throw new Error(`HTTP ${response.status}: ${body}`);
    }

    const resp = (await tryJson(response)) || {};
    if (!resp.assist_login_url) throw new Error("Missing assist_login_url");

    const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });

    await chrome.storage.local.set({
      pendingCaptureRequestId: requestId,
      pendingCaptureTabId: tab.id,
      pendingServiceUrl: serviceUrl,
    });

    chrome.action.setBadgeText({ text: "!" });
    setStatus(statusEl, "Approved. Complete login in the opened tab, then click 'I finished login'.", "ok");
  } catch (err) {
    console.error(err);
    alert("Approval failed: " + err.message);
  }
}

// ---------- Auto-refresh pending list ----------
function startPendingPolling() {
  if (pendingInterval) clearInterval(pendingInterval);
  pendingInterval = setInterval(() => {
    loadPendingRequests().catch(console.error);
  }, 5000);
}

// ---------- Manual capture: capture and send session from current tab ----------
async function captureAndSend() {
  const requestId = (document.getElementById("captureRequestId")?.value || "").trim();
  if (!requestId) {
    alert("Please paste the Request ID");
    return;
  }

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) {
    alert("No active tab found. Make sure you are on the logged‑in page.");
    return;
  }

  const serviceUrl = tab.url;

  chrome.runtime.sendMessage(
    { type: "CAPTURE_SESSION", tabId: tab.id, serviceUrl, requestId },
    (response) => {
      if (chrome.runtime.lastError) {
        alert("Error: " + chrome.runtime.lastError.message);
        return;
      }
      if (response && response.ok) {
        alert("✅ Session captured and sent to recipient!");
      } else {
        alert("❌ Capture failed: " + (response?.error || "unknown"));
      }
    }
  );
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

  pendingListEl = document.getElementById("pendingList");
  refreshPendingBtn = document.getElementById("refreshPending");

  const captureBtn = document.getElementById("captureBtn");

  // Load saved settings
  const saved = await chrome.storage.local.get([
    "handoffUrl",
    "baseUrl",
    "delayBetweenCookies",
    "delayAfterInject",
    "jwt",
    "assistedToken",
  ]);

  if (saved.baseUrl) baseUrlEl.value = saved.baseUrl;
  if (saved.delayBetweenCookies != null) delayBetweenCookiesEl.value = saved.delayBetweenCookies;
  if (saved.delayAfterInject != null) delayAfterInjectEl.value = saved.delayAfterInject;
  if (saved.jwt) jwtEl.value = saved.jwt;
  if (saved.assistedToken) assistedTokenEl.value = saved.assistedToken;

  if (saved.handoffUrl) {
    const m = String(saved.handoffUrl).match(/\/sharing\/handoff\/([^/?#]+)/);
    tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
  }

  // Core events (buttons unchanged)
  goBtn?.addEventListener("click", runHandoff);
  clearBtn?.addEventListener("click", async () => {
    tokenEl.value = "";
    await chrome.storage.local.remove(["handoffUrl"]);
    setStatus(statusEl, "Cleared saved token.", "ok");
  });

  tokenEl?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      runHandoff();
    }
  });

  assistedStartBtn?.addEventListener("click", startAssisted);
  finishBtn?.addEventListener("click", finishOwnerCapture);
  captureBtn?.addEventListener("click", captureAndSend);

  refreshPendingBtn?.addEventListener("click", () => loadPendingRequests());

  // mode UI
  if (modeHandoffEl && modeAssistedEl) {
    modeHandoffEl.addEventListener("change", updateModeUI);
    modeAssistedEl.addEventListener("change", updateModeUI);
    updateModeUI();
  }

  // Autosave (use input, avoid duplicates)
  const saveAndMaybeReload = async () => {
    const baseUrl = normalizeBaseUrl(baseUrlEl.value);
    const jwt = (jwtEl.value || "").trim();
    await chrome.storage.local.set({ baseUrl, jwt });
  };

  baseUrlEl?.addEventListener("input", saveAndMaybeReload);
  jwtEl?.addEventListener("input", saveAndMaybeReload);

  // Keep your background owner polling trigger (but only once)
  if ((saved.baseUrl || baseUrlEl?.value) && (saved.jwt || jwtEl?.value)) {
    chrome.runtime.sendMessage({
      type: "START_OWNER_POLLING",
      baseUrl: normalizeBaseUrl(baseUrlEl.value),
      jwt: (jwtEl.value || "").trim(),
    });
  }

  // Start owner pending list polling + initial render
  startPendingPolling();
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
//   if (v.startsWith("http://") || v.startsWith("https://")) return v;
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

// // ---------- DOM elements ----------
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



// async function finishOwnerCapture() {
//   const { baseUrl, jwt, pendingCaptureRequestId } = await chrome.storage.local.get([
//     "baseUrl", "jwt", "pendingCaptureRequestId"
//   ]);

//   if (!baseUrl || !jwt) {
//     alert("Missing baseUrl/jwt");
//     return;
//   }
//   if (!pendingCaptureRequestId) {
//     alert("No pending request id");
//     return;
//   }

//   const r = await fetch(`${baseUrl}/sharing/assisted/${encodeURIComponent(pendingCaptureRequestId)}/finish`, {
//     method: "POST",
//     headers: {
//       "Content-Type": "application/json",
//       Authorization: `Bearer ${jwt}`,
//     },
//   });

//   if (!r.ok) {
//     alert(`Finish failed: ${r.status} ${await r.text()}`);
//     return;
//   }

//   const out = await r.json();
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
//   const baseUrl = normalizeBaseUrl(baseUrlEl.value);
//   const jwt = (jwtEl.value || "").trim();
//   const container = document.getElementById("pendingList");
//   if (!baseUrl || !jwt) {
//     container.innerText = "Set Backend URL and JWT first.";
//     return;
//   }
//   try {
//     const response = await fetch(`${baseUrl}/sharing/assisted/pending`, {
//       headers: { Authorization: `Bearer ${jwt}` }
//     });
//     if (!response.ok) throw new Error(`HTTP ${response.status}`);
//     const pending = await response.json();
//     if (!pending.length) {
//       container.innerText = "No pending requests.";
//       return;
//     }
//     container.innerHTML = pending.map(req => `
//       <div class="request-item" style="border:1px solid #ccc; border-radius:8px; padding:8px; margin-bottom:8px;">
//         <div><strong>${req.service_url}</strong></div>
//         <div>Recipient: ${req.recipient_email}</div>
//         <div>Expires: ${new Date(req.expires_at * 1000).toLocaleString()}</div>
//         <button class="approveBtn" data-id="${req.request_id}" data-url="${req.service_url}" style="margin-top:6px;">Approve & assist</button>
//       </div>
//     `).join("");
//     document.querySelectorAll(".approveBtn").forEach(btn => {
//       btn.addEventListener("click", async () => {
//         const requestId = btn.dataset.id;
//         const serviceUrl = btn.dataset.url;
//         await approveRequest(requestId, serviceUrl);
//       });
//     });
//   } catch (err) {
//     console.error(err);
//     container.innerText = "Error loading requests.";
//   }
// }

// async function approveRequest(requestId, serviceUrl) {
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

//     const tab = await chrome.tabs.create({ url: resp.assist_login_url, active: true });
//     await chrome.storage.local.set({
//       pendingCaptureRequestId: requestId,
//       pendingCaptureTabId: tab.id,
//       pendingServiceUrl: serviceUrl
//     });
//     chrome.action.setBadgeText({ text: "!" });
//     alert("Approved! Login page opened. After manual login, click 'I finished login'.");
//   } catch (err) {
//     console.error(err);
//     alert("Approval failed: " + err.message);
//   }
// }

// // ---------- Auto-refresh pending list ----------
// let pendingInterval = null;
// function startPendingPolling() {
//   if (pendingInterval) clearInterval(pendingInterval);
//   pendingInterval = setInterval(() => {
//     loadPendingRequests().catch(console.error);
//   }, 5000);
// }

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
//   if (assistedStartBtn) assistedStartBtn.addEventListener("click", startAssisted);
//   if (finishBtn) finishBtn.addEventListener("click", finishOwnerCapture);
//   if (modeHandoffEl && modeAssistedEl) {
//     modeHandoffEl.addEventListener("change", updateModeUI);
//     modeAssistedEl.addEventListener("change", updateModeUI);
//     updateModeUI();
//   }








//   // Sauvegarde automatique des champs
//   baseUrlEl.addEventListener('change', () => {
//     const val = normalizeBaseUrl(baseUrlEl.value);
//     chrome.storage.local.set({ baseUrl: val });
//     // Redémarrer le polling avec les nouvelles valeurs
//     chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: val, jwt: jwtEl.value.trim() });
//   });
//   jwtEl.addEventListener('change', () => {
//     const val = jwtEl.value.trim();
//     chrome.storage.local.set({ jwt: val });
//     chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: normalizeBaseUrl(baseUrlEl.value), jwt: val });
//   });

  


//   if (saved.baseUrl && saved.jwt) {
//   chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: saved.baseUrl, jwt: saved.jwt });
// }





//   baseUrlEl.addEventListener('change', async () => {
//     const val = normalizeBaseUrl(baseUrlEl.value);
//     await chrome.storage.local.set({ baseUrl: val });
//     chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: val, jwt: jwtEl.value.trim() });
//     loadPendingRequests(); // rafraîchir la liste
//   });

//   jwtEl.addEventListener('change', async () => {
//     const val = jwtEl.value.trim();
//     await chrome.storage.local.set({ jwt: val });
//     chrome.runtime.sendMessage({ type: "START_OWNER_POLLING", baseUrl: normalizeBaseUrl(baseUrlEl.value), jwt: val });
//     loadPendingRequests();
//   });









//   // Start polling for pending requests (owner side)
//   startPendingPolling();
//   // Initial load
//   loadPendingRequests();
// });











// // Capture and send session from current tab
// async function captureAndSend() {
//   const requestId = document.getElementById("captureRequestId").value.trim();
//   if (!requestId) {
//     alert("Please paste the Request ID");
//     return;
//   }
//   // Get current active tab (where the owner is logged in)
//   const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
//   if (!tab || !tab.url) {
//     alert("No active tab found. Make sure you are on the logged‑in page.");
//     return;
//   }
//   const serviceUrl = tab.url;
//   // Send message to background to capture and submit
//   chrome.runtime.sendMessage({
//     type: "CAPTURE_SESSION",
//     tabId: tab.id,
//     serviceUrl: serviceUrl,
//     requestId: requestId
//   }, (response) => {
//     if (chrome.runtime.lastError) {
//       alert("Error: " + chrome.runtime.lastError.message);
//       return;
//     }
//     if (response && response.ok) {
//       alert("✅ Session captured and sent to recipient!");
//     } else {
//       alert("❌ Capture failed: " + (response?.error || "unknown"));
//     }
//   });
// }

// // Attach event listener after DOM is ready
// document.addEventListener("DOMContentLoaded", () => {
//   const captureBtn = document.getElementById("captureBtn");
//   if (captureBtn) captureBtn.addEventListener("click", captureAndSend);
// });



































