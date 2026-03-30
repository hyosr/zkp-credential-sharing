const DEFAULT_BASE = "http://localhost:8001";

function normalizeToHandoffUrl(input) {
  const v = (input || "").trim();
  if (!v) return null;

  // If user pasted full URL
  if (v.startsWith("http://") || v.startsWith("https://")) return v;

  // Otherwise treat it as session_id
  return `${DEFAULT_BASE}/sharing/handoff/${encodeURIComponent(v)}`;
}

async function setStatus(msg) {
  document.getElementById("status").textContent = msg;
}

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("base").textContent = DEFAULT_BASE;

  // Pre-fill with last used token/url if any
  const { handoffUrl } = await chrome.storage.local.get(["handoffUrl"]);
  if (handoffUrl) {
    // show only token part if possible
    const m = handoffUrl.match(/\/sharing\/handoff\/([^/?#]+)/);
    document.getElementById("token").value = m ? decodeURIComponent(m[1]) : handoffUrl;
  }

  document.getElementById("go").addEventListener("click", async () => {
    const tokenInput = document.getElementById("token").value;
    const handoffUrl = normalizeToHandoffUrl(tokenInput);
    if (!handoffUrl) {
      await setStatus("Please paste a token/session_id or full handoff URL.");
      return;
    }

    await chrome.storage.local.set({ handoffUrl });

    await setStatus("Connecting...");

    // Ask background service worker to run the handoff now
    chrome.runtime.sendMessage({ type: "RUN_HANDOFF", handoffUrl }, async (resp) => {
      if (chrome.runtime.lastError) {
        await setStatus("Error: " + chrome.runtime.lastError.message);
        return;
      }
      if (!resp || !resp.ok) {
        await setStatus("Failed: " + (resp?.error || "unknown error"));
        return;
      }
      await setStatus("Done. Opening connected profile...");
      window.close();
    });
  });
});