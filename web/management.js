"use strict";

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function formatTime(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function renderAlarmRows(container, alarms, resolved = false, clearAfter = 60) {
  container.replaceChildren();
  if (!alarms.length) {
    container.append(element("div", "empty",
      resolved ? "No recently resolved alarms." : "No active alarms."));
    return;
  }
  for (const alarm of alarms) {
    const row = element("div", "row alarm-entry", "");
    const identity = element("div", "row-identity", "");
    identity.append(element("strong", "", alarm.title), element("small", "alarm-detail", alarm.detail));
    if (resolved) {
      const age = Math.max(0, Math.floor((Date.now() - Date.parse(alarm.resolved_at)) / 1000));
      identity.append(element("small", "alarm-clear",
        `Alarm resolved, auto-clearing in ${Math.max(0, clearAfter - age)} seconds`));
    } else if (alarm.first_seen_at) {
      identity.append(element("small", "alarm-time", `Active since ${formatTime(alarm.first_seen_at)}`));
    }
    row.append(identity, element("span",
      `badge ${alarm.severity === "critical" ? "outage" : alarm.severity === "info" ? "operational" : "unknown"}`,
      `${alarm.kind.toUpperCase()} ALARM`));
    container.append(row);
  }
}

const historyLimit = 100;
let historyOffset = 0;
let historyTotal = 0;
let historyLoading = false;
let historyItems = [];

function renderHistoryItems() {
  const list = document.querySelector("#alarm-history-list");
  list.replaceChildren();
  if (!historyItems.length) {
    list.append(element("div", "empty", "No resolved alarms match those filters."));
    return;
  }
  for (const alarm of historyItems) {
    const row = element("div", "row alarm-entry", "");
    const identity = element("div", "row-identity", "");
    identity.append(
      element("strong", "", alarm.title),
      element("small", "alarm-detail", alarm.detail),
      element("small", "alarm-time", `Started ${formatTime(alarm.first_seen_at)} · Resolved ${formatTime(alarm.resolved_at)}`),
    );
    row.append(identity, element("span",
      `badge ${alarm.severity === "critical" ? "outage" : "unknown"}`,
      `${alarm.kind.toUpperCase()} ALARM`));
    list.append(row);
  }
}

function historyQuery(offset) {
  const values = new FormData(document.querySelector("#alarm-history-filters"));
  const params = new URLSearchParams();
  for (const name of ["from", "to", "type"]) {
    const value = values.get(name);
    if (value) params.set(name, value);
  }
  params.set("limit", String(historyLimit));
  params.set("offset", String(offset));
  return params;
}

async function loadAlarmHistory(reset = true) {
  if (historyLoading) return;
  historyLoading = true;
  const form = document.querySelector("#alarm-history-filters");
  const list = document.querySelector("#alarm-history-list");
  const error = document.querySelector("#alarm-history-error");
  const more = document.querySelector("#alarm-history-more");
  error.textContent = "";
  if (reset) {
    historyOffset = 0;
    list.replaceChildren(element("div", "empty", "Loading alarm history…"));
  }
  const params = historyQuery(historyOffset);
  try {
    const response = await fetch(`/api/alarm-history?${params}`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    if (reset) historyItems = [];
    const knownIds = new Set(historyItems.map(alarm => alarm.event_id));
    historyItems.push(...result.alarms.filter(alarm => !knownIds.has(alarm.event_id)));
    historyItems.sort((left, right) => right.resolved_at.localeCompare(left.resolved_at));
    renderHistoryItems();
    historyOffset = historyItems.length;
    historyTotal = result.total;
    more.hidden = historyOffset >= historyTotal;
  } catch (loadError) {
    console.error("Unable to load alarm history:", loadError);
    error.textContent = loadError.message;
    if (reset) list.replaceChildren(element("div", "empty", "Alarm history is unavailable."));
  } finally {
    historyLoading = false;
  }
}

async function refreshLatestAlarmHistory() {
  if (historyLoading) return;
  try {
    const response = await fetch(`/api/alarm-history?${historyQuery(0)}`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    const knownIds = new Set(historyItems.map(alarm => alarm.event_id));
    historyItems.push(...result.alarms.filter(alarm => !knownIds.has(alarm.event_id)));
    historyItems.sort((left, right) => right.resolved_at.localeCompare(left.resolved_at));
    historyTotal = result.total;
    historyOffset = historyItems.length;
    renderHistoryItems();
    document.querySelector("#alarm-history-more").hidden = historyOffset >= historyTotal;
  } catch (error) {
    console.error("Unable to refresh alarm history:", error);
  }
}

async function refresh() {
  try {
    const [rackResponse, statusResponse] = await Promise.all([
      fetch("/api/racks", { cache: "no-store" }),
      fetch("/api/management", { cache: "no-store" }),
    ]);
    if (!rackResponse.ok || !statusResponse.ok) throw new Error("Management data could not be loaded.");
    const inventory = (await rackResponse.json()).state;
    const status = await statusResponse.json();
    const outages = status.services.filter(service => service.status === "outage");
    const activeAlerts = status.alerts || [];
    const resolvedAlerts = status.resolved_alerts || [];
    const health = status.health || {};
    document.querySelector("#device-count").textContent = inventory.devices.length;
    document.querySelector("#rack-count").textContent = inventory.racks.length;
    document.querySelector("#website-count").textContent = status.services.length;
    document.querySelector("#outage-count").textContent = outages.length;
    const rackSummary = document.querySelector("#rack-summary");
    rackSummary.replaceChildren();
    document.querySelector("#rack-summary-caption").textContent = `${inventory.racks.length} racks · ${inventory.devices.length} devices · automatic health checks`;
    if (!inventory.racks.length) rackSummary.append(element("div", "empty", "No racks configured. Open the rack designer to create your first rack."));
    for (const rack of inventory.racks) {
      const devices = inventory.devices.filter(device => device.rack_id === rack.id);
      const online = devices.filter(device => health[device.id]?.status === "online").length;
      const offline = devices.filter(device => health[device.id]?.status === "offline").length;
      const notEnrolled = devices.filter(device =>
        !device.discovery || !device.management_ip || !device.credential_ref).length;
      const checking = devices.length - online - offline - notEnrolled;
      const row = element("div", "row", "");
      row.append(element("div", "row-identity", `${rack.name} · ${rack.units}U`),
        element("span", `badge ${offline ? "outage" : online ? "operational" : "unknown"}`,
          `${online} ONLINE · ${offline} OFFLINE · ${checking} CHECKING · ${notEnrolled} NOT ENROLLED`));
      rackSummary.append(row);
    }
    const unassigned = inventory.devices.filter(device => !device.rack_id);
    if (unassigned.length) {
      const row = element("div", "row", "");
      row.append(element("div", "row-identity", `${unassigned.length} unassigned devices`),
        element("a", "text-link", "Open rack designer →"));
      row.querySelector("a").href = "/rack";
      rackSummary.append(row);
    }
    renderAlarmRows(document.querySelector("#alerts"), activeAlerts);
    renderAlarmRows(document.querySelector("#resolved-alerts"), resolvedAlerts, true,
      status.resolved_auto_clear_seconds || 60);
    refreshLatestAlarmHistory();
  } catch (error) {
    console.error("Unable to load management overview:", error);
    for (const id of ["device-count", "rack-count", "website-count", "outage-count"]) document.querySelector(`#${id}`).textContent = "—";
    document.querySelector("#rack-summary").replaceChildren(element("div", "empty", "Management inventory is unavailable."));
    document.querySelector("#alerts").replaceChildren(element("div", "empty", "Management data is unavailable."));
    document.querySelector("#resolved-alerts").replaceChildren(element("div", "empty", "Resolved alarm data is unavailable."));
    document.querySelector("#rack-summary-caption").textContent = "Inventory unavailable";
  }
}

document.querySelector("#alarm-history-filters").addEventListener("submit", event => {
  event.preventDefault();
  const values = new FormData(event.currentTarget);
  const from = values.get("from"), to = values.get("to");
  if (from && to && from > to) {
    document.querySelector("#alarm-history-error").textContent = "The start date must not be after the end date.";
    return;
  }
  loadAlarmHistory().catch(error => {
    document.querySelector("#alarm-history-error").textContent = error.message;
  });
});
document.querySelector("#alarm-history-more").addEventListener("click", () => {
  loadAlarmHistory(false).catch(error => {
    document.querySelector("#alarm-history-error").textContent = error.message;
  });
});

refresh();
scheduleLiveRefresh(refresh);
loadAlarmHistory();
