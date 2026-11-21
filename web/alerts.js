"use strict";

const alertButton = document.createElement("button");
alertButton.type = "button";
alertButton.className = "alert-button";
alertButton.setAttribute("aria-label", "Open active alarms");
alertButton.setAttribute("aria-expanded", "false");
alertButton.innerHTML = '<span class="alert-bell" aria-hidden="true">&#128276;</span><span class="alert-count" hidden>0</span>';

const panel = document.createElement("aside");
panel.className = "alert-panel";
panel.setAttribute("aria-label", "Active alarms");
panel.hidden = true;
panel.innerHTML = '<div class="alert-panel-heading"><div><div class="eyebrow">MONITORING</div><h2>Active alarms</h2></div><button type="button" class="icon-button alert-close" aria-label="Close active alarms">×</button></div><div class="alert-list"><h3>Active</h3><div class="active-alert-list"><div class="empty">Loading alarms…</div></div><h3>Recently resolved</h3><div class="resolved-alert-list"><div class="empty">No recently resolved alarms.</div></div><a class="alarm-history-link" href="/#alarm-history">Open alarm history →</a></div>';

function renderRows(list, alerts, resolved, clearAfter) {
  list.replaceChildren();
  if (!alerts.length) {
    list.append(Object.assign(document.createElement("div"), {
      className: "empty",
      textContent: resolved ? "No recently resolved alarms." : "No active alarms.",
    }));
    return;
  }
  for (const alert of alerts) {
    const row = document.createElement("div");
    row.className = `alert-row ${resolved ? "resolved" : alert.severity}`;
    row.innerHTML = '<span class="alert-row-icon">!</span><div><strong></strong><p></p><small></small></div>';
    row.querySelector("strong").textContent = alert.title;
    row.querySelector("p").textContent = alert.detail;
    if (resolved) {
      const age = Math.max(0, Math.floor((Date.now() - Date.parse(alert.resolved_at)) / 1000));
      const remaining = Math.max(0, clearAfter - age);
      row.querySelector("small").textContent = `Alarm resolved, auto-clearing in ${remaining} seconds`;
    } else {
      row.querySelector("small").textContent = alert.first_seen_at
        ? `Active since ${new Date(alert.first_seen_at).toLocaleString()}`
        : "";
    }
    list.append(row);
  }
}

function renderAlerts(data) {
  const count = alertButton.querySelector(".alert-count");
  const alerts = data.alerts || [];
  const resolved = data.resolved_alerts || [];
  count.textContent = alerts.length;
  count.hidden = !alerts.length;
  renderRows(panel.querySelector(".active-alert-list"), alerts, false, 60);
  renderRows(panel.querySelector(".resolved-alert-list"), resolved, true,
    data.resolved_auto_clear_seconds || 60);
  panel.querySelector(".alarm-history-link").hidden = !document.body.classList.contains("management")
    && !document.body.classList.contains("rack-page");
}

async function refreshAlerts() {
  try {
    const response = await fetch("/api/alerts", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderAlerts(await response.json());
  } catch (error) {
    console.error("Unable to load alarms:", error);
    renderRows(panel.querySelector(".active-alert-list"), [], false, 60);
    panel.querySelector(".active-alert-list .empty").textContent = "Alarm data is unavailable.";
    renderRows(panel.querySelector(".resolved-alert-list"), [], true, 60);
  }
}

document.querySelector(".topbar .nav, .topbar .rack-nav")?.append(alertButton);
document.body.append(panel);
alertButton.addEventListener("click", () => {
  panel.hidden = !panel.hidden;
  alertButton.setAttribute("aria-expanded", String(!panel.hidden));
});
panel.querySelector(".alert-close").addEventListener("click", () => {
  panel.hidden = true;
  alertButton.setAttribute("aria-expanded", "false");
});
refreshAlerts();
scheduleLiveRefresh(refreshAlerts);
