// Praxis de-id local UI — vanilla, no framework, no CDN.
// Talks only to same-origin /api/* routes.

(function () {
  "use strict";

  const form = document.getElementById("run-form");
  const runBtn = document.getElementById("run-btn");
  const statusMsg = document.getElementById("status-msg");
  const resultCard = document.getElementById("result-card");
  const resultStatus = document.getElementById("result-status");
  const outputDirText = document.getElementById("output-dir-text");
  const auditLogPathText = document.getElementById("audit-log-path-text");
  const filesTableBody = document.querySelector("#files-table tbody");
  const auditRecordText = document.getElementById("audit-record-text");
  const phiResults = document.getElementById("phi-scan-results");
  const openFolderBtn = document.getElementById("open-folder-btn");
  const genSaltBtn = document.getElementById("gen-salt-btn");
  const saltInput = document.getElementById("salt-input");

  // --- file picker UX ----------------------------------------------------
  document.querySelectorAll('input[type="file"]').forEach((input) => {
    input.addEventListener("change", () => {
      const role = input.dataset.role;
      const meta = document.querySelector(`small[data-meta="${role}"]`);
      if (!meta) return;
      const file = input.files && input.files[0];
      if (!file) {
        meta.textContent = "no file selected";
        return;
      }
      meta.textContent = `${file.name} — ${formatBytes(file.size)}`;
    });
  });

  // --- generate salt -----------------------------------------------------
  genSaltBtn.addEventListener("click", () => {
    // 32 random bytes -> 64 hex chars. Crypto.getRandomValues is in-browser,
    // not a network call. Mirrors `secrets.token_hex(32)` server-side.
    const buf = new Uint8Array(32);
    window.crypto.getRandomValues(buf);
    saltInput.value = Array.from(buf)
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  });

  // --- form submit -------------------------------------------------------
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    runBtn.disabled = true;
    statusMsg.textContent = "running…";
    statusMsg.className = "status";
    resultCard.classList.add("hidden");

    const fd = new FormData(form);
    // Strip empty file inputs so FastAPI sees them as None.
    for (const role of ["patients", "appointments", "providers", "procedures", "referrals", "invoices"]) {
      const f = fd.get(role);
      if (f && f.size === 0) fd.delete(role);
    }

    let res;
    try {
      res = await fetch("/api/run", { method: "POST", body: fd });
    } catch (err) {
      runBtn.disabled = false;
      statusMsg.textContent = "request failed: " + err.message;
      statusMsg.className = "status err";
      return;
    }

    let payload;
    try {
      payload = await res.json();
    } catch (err) {
      runBtn.disabled = false;
      statusMsg.textContent = "bad response from server";
      statusMsg.className = "status err";
      return;
    }

    runBtn.disabled = false;
    if (payload.status === "success") {
      statusMsg.textContent = "done";
      statusMsg.className = "status ok";
    } else {
      statusMsg.textContent = "failed";
      statusMsg.className = "status err";
    }

    renderResult(payload);
  });

  function renderResult(payload) {
    resultCard.classList.remove("hidden");

    if (payload.status === "success") {
      resultStatus.textContent = "Success — de-identification complete.";
      resultStatus.className = "result-status ok";
    } else {
      resultStatus.textContent = "Failed: " + (payload.error || "unknown error");
      resultStatus.className = "result-status err";
    }

    outputDirText.textContent = payload.output_dir || "(none)";
    auditLogPathText.textContent = payload.audit_log_path || "(none)";

    filesTableBody.innerHTML = "";
    (payload.files || []).forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td><code>${escapeHtml(f.role)}</code></td>` +
        `<td>${f.row_count}</td>` +
        `<td>${formatBytes(f.byte_count)}</td>`;
      filesTableBody.appendChild(tr);
    });

    auditRecordText.textContent = JSON.stringify(payload.audit_record, null, 2);

    phiResults.innerHTML = "";
    (payload.phi_scan || []).forEach((scan) => {
      const row = document.createElement("div");
      const fileName = scan.file.split("/").pop();
      if (!scan.hits || scan.hits.length === 0) {
        row.className = "phi-row clean";
        row.innerHTML =
          `<span>${escapeHtml(fileName)}</span>` +
          `<span>✓ no PHI patterns detected (${scan.rows_scanned} rows)</span>`;
        phiResults.appendChild(row);
      } else {
        row.className = "phi-row dirty";
        row.innerHTML =
          `<span>${escapeHtml(fileName)}</span>` +
          `<span>${scan.hits.length} possible match(es) in ${scan.rows_scanned} rows</span>`;
        phiResults.appendChild(row);

        const ul = document.createElement("ul");
        ul.className = "phi-hits";
        scan.hits.slice(0, 10).forEach((h) => {
          const li = document.createElement("li");
          li.textContent =
            `row ${h.row_index} · ${h.column} · ${h.pattern} · ${h.sample}`;
          ul.appendChild(li);
        });
        if (scan.hits.length > 10) {
          const li = document.createElement("li");
          li.textContent = `... and ${scan.hits.length - 10} more`;
          ul.appendChild(li);
        }
        phiResults.appendChild(ul);
      }
    });
  }

  // --- open folder -------------------------------------------------------
  openFolderBtn.addEventListener("click", async () => {
    const path = outputDirText.textContent;
    if (!path || path === "(none)") return;
    try {
      const res = await fetch("/api/open-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      if (!res.ok) {
        const text = await res.text();
        alert("open folder failed: " + text);
      }
    } catch (err) {
      alert("open folder failed: " + err.message);
    }
  });

  // --- helpers -----------------------------------------------------------
  function formatBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
