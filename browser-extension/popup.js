const DEFAULT_BASE = "http://localhost:8001";

function normalizeBaseUrl(input) {
  const v = (input || "").trim().replace(/\/+$/, "");
  if (!v) return DEFAULT_BASE;
  return v;
}

function normalizeToHandoffUrl(baseUrl, input) {
  const v = (input || "").trim();
  if (!v) return null;

  // If user pasted full URL
  if (v.startsWith("http://") || v.startsWith("https://")) return v;

  // Otherwise treat it as session_id
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

document.addEventListener("DOMContentLoaded", async () => {
  const baseUrlEl = document.getElementById("baseUrl");
  const tokenEl = document.getElementById("token");
  const goBtn = document.getElementById("go");
  const clearBtn = document.getElementById("clear");
  const statusEl = document.getElementById("status");
  const spinner = document.getElementById("spinner");
  const goText = document.getElementById("goText");

  const saved = await chrome.storage.local.get(["handoffUrl", "baseUrl"]);
  baseUrlEl.value = saved.baseUrl || DEFAULT_BASE;

  if (saved.handoffUrl) {
    const m = saved.handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
    tokenEl.value = m ? decodeURIComponent(m[1]) : saved.handoffUrl;
  }

  async function run() {
    const baseUrl = normalizeBaseUrl(baseUrlEl.value);
    const handoffUrl = normalizeToHandoffUrl(baseUrl, tokenEl.value);

    if (!handoffUrl) {
      setStatus(statusEl, "Paste a token/session_id or a full handoff URL.", "err");
      return;
    }

    setLoading(goBtn, spinner, goText, true);
    setStatus(statusEl, "");

    await chrome.storage.local.set({ handoffUrl, baseUrl });

    chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl }, (resp) => {
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

  goBtn.addEventListener("click", run);

  tokenEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      run();
    }
  });

  clearBtn.addEventListener("click", async () => {
    tokenEl.value = "";
    await chrome.storage.local.remove(["handoffUrl"]);
    setStatus(statusEl, "Cleared saved token.", "ok");
  });
});


























// const DEFAULT_BASE = "http://localhost:8001";

// function normalizeToHandoffUrl(input) {
//   const v = (input || "").trim();
//   if (!v) return null;

//   // If user pasted full URL
//   if (v.startsWith("http://") || v.startsWith("https://")) return v;

//   // Otherwise treat it as session_id
//   return `${DEFAULT_BASE}/sharing/handoff/${encodeURIComponent(v)}`;
// }

// async function setStatus(msg) {
//   document.getElementById("status").textContent = msg;
// }

// document.addEventListener("DOMContentLoaded", async () => {
//   document.getElementById("base").textContent = DEFAULT_BASE;

//   // Pre-fill with last used token/url if any
//   const { handoffUrl } = await chrome.storage.local.get(["handoffUrl"]);
//   if (handoffUrl) {
//     // show only token part if possible
//     const m = handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
//     document.getElementById("token").value = m ? decodeURIComponent(m[1]) : handoffUrl;
//   }

//   document.getElementById("go").addEventListener("click", async () => {
//     const tokenInput = document.getElementById("token").value;
//     const handoffUrl = normalizeToHandoffUrl(tokenInput);
//     if (!handoffUrl) {
//       await setStatus("Please paste a token/session_id or full handoff URL.");
//       return;
//     }

//     await chrome.storage.local.set({ handoffUrl });

//     await setStatus("Connecting...");

//     // Ask background service worker to run the handoff now
//     chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl }, async (resp) => {
//       if (chrome.runtime.lastError) {
//         await setStatus("Error: " + chrome.runtime.lastError.message);
//         return;
//       }
//       if (!resp || !resp.ok) {
//         await setStatus("Failed: " + (resp?.error || "unknown error"));
//         return;
//       }
//       await setStatus("Done. Opening connected profile...");
//       window.close();
//     });
//   });
// });