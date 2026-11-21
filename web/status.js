"use strict";

const servicesElement = document.querySelector("#services");
const summary = document.querySelector("#summary");

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function render(data) {
  const services = data.services;
  const outages = services.filter(service => service.status === "outage").length;
  const unknown = services.filter(service => service.status === "unknown").length;
  let title = "All services operational";
  let detail = "All monitored services are responding.";
  let state = "good";
  if (!services.length) {
    title = "No services configured";
    detail = "Availability will appear here once website checks are configured.";
    state = "unknown";
  } else if (outages) {
    title = "Service disruption";
    detail = `${outages} ${outages === 1 ? "service is" : "services are"} not responding normally.`;
    state = "bad";
  } else if (unknown) {
    title = "Checks in progress";
    detail = "Some services have not been checked yet.";
    state = "unknown";
  }
  summary.className = `hero ${state}`;
  summary.replaceChildren(
    element("div", "hero-icon", state === "good" ? "✓" : state === "bad" ? "!" : "…"),
  );
  const content = element("div", "", "");
  content.append(element("div", "hero-kicker", "CURRENT STATUS"), element("h2", "", title), element("p", "", detail));
  summary.append(content, element("span", "hero-indicator", "LIVE MONITORING"));
  document.querySelector("#service-count").textContent = `${services.length} SERVICES`;
  servicesElement.replaceChildren();
  if (!services.length) {
    servicesElement.append(element("div", "empty", "No services have been configured yet."));
    return;
  }
  for (const service of services) {
    const row = element("div", "row", "");
    const identity = element("div", "row-identity", "");
    identity.append(element("span", "service-icon", "↗"), element("strong", "", service.name));
    const label = service.status === "operational" ? "Operational" : service.status === "outage" ? "Outage" : "Awaiting check";
    row.append(identity, element("span", `badge ${service.status}`, label));
    servicesElement.append(row);
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    console.error("Unable to load status:", error);
    summary.className = "hero unknown";
    summary.replaceChildren(element("div", "hero-icon", "!"));
    const content = element("div", "", "");
    content.append(element("div", "hero-kicker", "STATUS UNAVAILABLE"), element("h2", "", "Unable to load status"), element("p", "", "Monitoring data could not be retrieved."));
    summary.append(content);
    document.querySelector("#service-count").textContent = "— SERVICES";
    servicesElement.replaceChildren(element("div", "empty", "Service status is currently unavailable."));
  }
}

refresh();
scheduleLiveRefresh(refresh);
