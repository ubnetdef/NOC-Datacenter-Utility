"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const UNIT_PX = 48;
const SVG_WIDTH = 1000;
const PANEL_X = 92;
const PANEL_WIDTH = 820;
let inventory = null;
let catalog = null;
let healthStates = {};
let revision = null;
let selectedRackId = null;
let saving = false;

const $ = selector => document.querySelector(selector);
const make = (tag, className = "", text = "") => {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
};
const uid = () => crypto.randomUUID();

function svg(tag, attrs = {}, text = "") {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (text) node.textContent = text;
  return node;
}

function componentSlotStatus(components, missing, index, prefix) {
  const number = String(index + 1);
  const componentNumber = item => {
    const identity = `${item.id || ""} ${item.name || ""}`.toLowerCase();
    const match = prefix === "psu"
      ? identity.match(/(?:psu|ps|power supply)\s*[-#]?\s*(\d+)/)
      : identity.match(/(?:drive|disk|bay|slot)\s*[-#]?\s*(\d+)/);
    return match?.[1] || identity.match(/\/(\d+)$/)?.[1];
  };
  const component = components.find(item => {
    return componentNumber(item) === number;
  }) || components[index];
  if (missing.some(value => componentNumber({ id: value }) === number)) return "critical";
  return componentStatus(component);
}

function add(parent, tag, attrs, text) {
  const node = svg(tag, attrs, text);
  parent.append(node);
  return node;
}

function modelFor(device) {
  return catalog.models.find(model => model.id === device.model_id) || null;
}

function variantFor(device, model = modelFor(device)) {
  return model?.variants.find(variant => variant.id === device.variant_id) || model?.variants[0] || null;
}

function deviceHealth(device) {
  return healthStates[device.id] || null;
}

function livePort(device, key, label) {
  const ports = deviceHealth(device)?.telemetry?.network_ports || [];
  const number = key.match(/(\d+)$/)?.[1];
  const aliases = [key, label, number, number ? `interface ${number}` : null, number ? `port ${number}` : null]
    .filter(Boolean).map(value => value.toLowerCase());
  return ports.find(port => {
    const identifiers = [port.id, port.name, port.mac].filter(Boolean).map(value => String(value).toLowerCase());
    return identifiers.some(value => aliases.some(alias => value === alias || value.endsWith(`/${alias}`) || value.endsWith(` ${alias}`)));
  }) || null;
}

function portLinkState(device, key, label, kind) {
  if (!["ethernet", "qsfp"].includes(kind)) return "unknown";
  const port = livePort(device, key, label);
  if (!port) return "unknown";
  const link = String(port.link || "").toLowerCase();
  const degraded = ["warning", "critical", "failed"].includes(String(port.health || "").toLowerCase());
  const aliases = new Set([key, label, key.match(/(\d+)$/)?.[1]].filter(Boolean).map(value => value.toLowerCase()));
  const attached = inventory.cables.some(cable =>
    (cable.device_a === device.id && aliases.has(cable.port_a.trim().toLowerCase()))
    || (cable.device_b === device.id && aliases.has(cable.port_b.trim().toLowerCase())));
  if (!attached) return "down";
  if (degraded || ["down", "nolink", "lowerlayerdown", "notpresent", "dormant"].includes(link)) return "error";
  if (deviceHealth(device)?.telemetry?.network_telemetry_stale) return "unknown";
  if (["up", "linkup", "active"].includes(link)) return "up";
  return "unknown";
}

function addPort(parent, x, y, label, size = 14, kind = "ethernet", device = null, key = null, description = label, labelMode = "below") {
  const width = kind === "qsfp" ? size * 1.65 : size;
  const linkState = device && key ? portLinkState(device, key, label, kind) : "unknown";
  const group = svg("g", {
    class: `svg-port-control link-${linkState}`,
    tabindex: device && key ? "0" : "-1",
    role: device && key ? "button" : "img",
    "aria-label": device && key ? `Configure ${description}` : description,
  });
  add(group, "rect", { x: x - 2, y: y - 2, width: width + 4, height: Math.max(8, size * .72) + 4, rx: 2, class: "svg-port-hit" });
  add(group, "rect", { x, y, width, height: Math.max(8, size * .72), rx: 1.5, class: `svg-port ${kind}` });
  add(group, "circle", { cx: x + width - 3, cy: y + 3, r: 1.3, class: "svg-led" });
  if (label) add(group, "text", {
    x: x + width / 2,
    y: labelMode === "inside" ? y + Math.max(7, size * .72 - 1) : y + Math.max(8, size * .72) + 7,
    class: labelMode === "inside" ? "svg-port-number" : "svg-mini-label",
    "text-anchor": "middle",
  }, label);
  const stateLabel = linkState === "up" ? "uplink running" : linkState === "error" ? "uplink error" : "link state unknown";
  add(group, "title", {}, `${description} · ${stateLabel}${device && key ? " · click to edit" : ""}`);
  if (device && key) {
    const open = event => {
      event.stopPropagation();
      editPort(device, key, description);
    };
    group.addEventListener("click", open);
    group.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open(event);
      }
    });
  }
  parent.append(group);
}

function addDrive(parent, x, y, width, height, index, state = "unknown") {
  add(parent, "rect", { x, y, width, height, rx: 1, class: "svg-drive" });
  add(parent, "rect", { x: x + 2, y: y + 2, width: Math.max(3, width - 4), height: Math.max(3, height - 4), rx: 1, class: "svg-drive-inner" });
  add(parent, "rect", { x: x + width - 5, y: y + height / 2 - 2, width: 2, height: 4, class: "svg-handle" });
  add(parent, "circle", { cx: x + 4, cy: y + height - 4, r: 1.2, class: `svg-led status-${state}` });
  if (width > 24 && height > 14) add(parent, "text", { x: x + width / 2, y: y + height / 2 + 3, class: "svg-drive-label", "text-anchor": "middle" }, String(index + 1));
}

function devicePanel(parent, device, side, top, height) {
  const model = modelFor(device);
  const variant = variantFor(device, model);
  const family = model?.family || "custom";
  const brandClass = family.startsWith("hpe") ? "svg-chassis hpe" : family.includes("switch") ? "svg-chassis switch" : "svg-chassis dell";
  const g = svg("g", { class: "svg-device", tabindex: "0", role: "button", "aria-label": `${side} view of ${device.name}; click to edit` });
  const y = top + 1.5;
  const h = Math.max(18, height - 3);
  add(g, "rect", { x: PANEL_X, y, width: PANEL_WIDTH, height: h, rx: 3, class: brandClass });
  const health = deviceHealth(device);
  const powerState = String(health?.telemetry?.system?.state || "").toLowerCase();
  const poweredOff = ["off", "poweroff", "poweredoff"].includes(powerState);
  const systemHealth = health?.status === "offline" || poweredOff ? "critical"
    : health?.status === "online" ? componentStatus(health.telemetry?.system) : "unknown";
  add(g, "circle", {
    cx: PANEL_X + 16,
    cy: y + h / 2,
    r: 3.2,
    class: `svg-health-indicator status-${systemHealth}`,
  });
  add(g, "title", {}, health?.status === "offline" ? `${device.name}: management heartbeat lost` : health?.status === "online" ? `${device.name}: heartbeat online` : `${device.name}: monitoring not enrolled`);
  add(g, "rect", { x: PANEL_X + 3, y: y + 3, width: 8, height: h - 6, class: "svg-rack-ear" });
  add(g, "rect", { x: PANEL_X + PANEL_WIDTH - 11, y: y + 3, width: 8, height: h - 6, class: "svg-rack-ear" });
  for (const boltY of [y + 7, y + h - 7]) {
    add(g, "circle", { cx: PANEL_X + 7, cy: boltY, r: 1.8, class: "svg-screw" });
    add(g, "circle", { cx: PANEL_X + PANEL_WIDTH - 7, cy: boltY, r: 1.8, class: "svg-screw" });
  }
  const brand = model?.manufacturer || "CUSTOM";
  const modelName = model?.name || "UNLISTED DEVICE";
  if (side === "front") drawFront(g, device, model, variant, y, h, brand, modelName);
  else drawRear(g, device, model, variant, y, h, brand, modelName);
  add(g, "title", {}, `${brand} ${modelName} · ${device.name} · ${side} · ${device.height}U`);
  g.addEventListener("click", () => editDevice(device));
  g.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); editDevice(device); }
  });
  parent.append(g);
}

function drawFront(g, device, model, variant, y, h, brand, modelName) {
  if (model?.family === "dell-switch") {
    add(g, "text", { x: 113, y: y + 12, class: "svg-brand svg-brand-compact" }, "DELL EMC");
    add(g, "text", { x: 113, y: y + 24, class: "svg-model svg-model-compact" }, "S4048T-ON");
    const portStart = 285;
    for (let index = 0; index < 48; index++) {
      const col = index % 24;
      const row = Math.floor(index / 24);
      const number = index + 1;
      addPort(g, portStart + col * 19, y + 4 + row * 19, "", 17, "ethernet",
        device, `front-ethernet-${number}`, `Ethernet port ${number}`, "inside");
    }
    add(g, "text", { x: 750, y: y + 10, class: "svg-group-label" }, "QSFP+");
    for (let index = 0; index < 6; index++) {
      const number = index + 1;
      addPort(g, 750 + (index % 3) * 38, y + 3 + Math.floor(index / 3) * 19, String(number), 22, "qsfp",
        device, `front-qsfp-${number}`, `QSFP+ uplink ${number}`, "inside");
    }
    return;
  }
  const frontLayout = model?.front || {};
  const controlWidth = frontLayout.control_width || 142;
  const controlX = frontLayout.control_side === "right"
    ? PANEL_X + PANEL_WIDTH - controlWidth - 13
    : PANEL_X + 13;
  const driveStart = frontLayout.control_side === "right" ? PANEL_X + 13 : PANEL_X + 190;
  const driveEnd = frontLayout.control_side === "right" ? controlX - 13 : PANEL_X + PANEL_WIDTH - 13;
  const usable = driveEnd - driveStart;
  const bezel = model?.family === "hpe-gen9" ? "#aeb4b6" : "#101316";
  add(g, "rect", { x: controlX, y: y + 3, width: controlWidth, height: h - 6, rx: 2, fill: bezel, class: "svg-control-bay" });
  const compact = device.height === 1;
  add(g, "text", {
    x: controlX + 7,
    y: y + (compact ? 12 : 16),
    class: compact ? "svg-brand svg-brand-compact" : "svg-brand",
  }, brand.toUpperCase());
  add(g, "text", {
    x: controlX + 7,
    y: y + (compact ? 23 : 32),
    class: compact ? "svg-model svg-model-compact" : "svg-model",
  }, modelName.toUpperCase());
  if (!compact) {
    add(g, "rect", { x: controlX + 8, y: y + 38, width: 38, height: 13, rx: 1, class: "svg-lcd" });
    add(g, "text", { x: controlX + 27, y: y + 47, class: "svg-lcd-text", "text-anchor": "middle" }, "SYSTEM");
    add(g, "circle", { cx: controlX + 65, cy: y + 45, r: 5, class: "svg-button" });
    add(g, "circle", { cx: controlX + 82, cy: y + 45, r: 2, class: "svg-led" });
  } else {
    add(g, "circle", { cx: controlX + controlWidth - 16, cy: y + h / 2, r: 3, class: "svg-button" });
    add(g, "circle", { cx: controlX + controlWidth - 6, cy: y + h / 2, r: 1.8, class: "svg-led" });
  }

  if (model?.family === "dell-switch") return;
  const driveCount = variant?.front_bays || device.drives?.length || 0;
  if (driveCount > 0) {
    const orientation = variant?.drive_orientation || (variant?.bay_size === "2.5 in" ? "vertical" : "horizontal");
    const rows = variant?.drive_rows || 1;
    const columns = Math.ceil(driveCount / rows);
    const gapX = columns > 16 ? 2 : 3;
    const gapY = 4;
    const maxSlotW = variant?.drive_slot_width || (orientation === "vertical" ? 44 : 130);
    const slotW = Math.min(maxSlotW, (usable - (columns - 1) * gapX) / columns);
    const slotH = (h - 8 - (rows - 1) * gapY) / rows;
    const gridHeight = rows * slotH + (rows - 1) * gapY;
    const gridTop = y + (h - gridHeight) / 2;
    for (let index = 0; index < driveCount; index++) {
      const col = index % columns;
      const row = Math.floor(index / columns);
      const dx = driveStart + col * (slotW + gapX);
      const dy = gridTop + row * (slotH + gapY);
      const monitored = deviceHealth(device);
      const drives = monitored?.telemetry?.drives || [];
      addDrive(g, dx, dy, slotW, slotH, index,
        componentSlotStatus(drives, monitored?.missing_components?.drives || [], index, "drive"));
    }
    if (h >= 65 && rows === 1) {
      add(g, "text", { x: driveStart, y: gridTop + gridHeight + 12, class: "svg-state" }, `${driveCount} × ${variant?.bay_size || "DRIVE"} BAYS`);
    }
  } else {
    for (let index = 0; index < 12; index++) {
      add(g, "line", { x1: driveStart + index * 42, y1: y + 5, x2: driveStart + index * 42, y2: y + h - 5, class: "svg-vent" });
    }
    const ports = device.front_ports || [];
    ports.forEach((port, index) => addPort(g, driveStart + index * 30, y + Math.max(5, h / 2 - 7),
      port, 25, "ethernet", device, `front-custom-${index + 1}`, port));
    if (!ports.length && !model) add(g, "text", { x: driveStart + 10, y: y + h / 2 + 3, class: "svg-state" }, "CUSTOM FACEPLATE — ADD PORT / BAY LABELS");
  }
}

function drawRear(g, device, model, variant, y, h, brand, modelName) {
  const rear = model?.rear || { psus: 2, fans: 2, nic_ports: device.rear_ports?.length || 0, idrac: false, ilo: false, serial: false, vga: false, usb: 0, pcie: 0 };
  if (model?.family === "dell-switch") {
    drawPowerModules(g, rear, y, h, device);
    drawFanGroup(g, 330, y + 4, 3, 250, Math.max(14, h - 8));
    if (rear.management_port) {
      addPort(g, 604, y + 5, "MGMT", 14, "ethernet", device,
        "rear-management-ethernet", "Switch management Ethernet");
    }
    if (rear.serial) addPort(g, 646, y + 5, "SER", 12, "service", device, "rear-serial", "Serial console");
    if (rear.usb) addPort(g, 684, y + 5, "USB", 12, "service", device, "rear-usb-1", "USB service port");
    return;
  }
  drawPowerModules(g, rear, y, h, device);
  const portStart = 356;
  const portY = y + (device.height >= 2 ? 28 : 8);
  if (device.height >= 2) add(g, "text", { x: portStart, y: y + 16, class: "svg-group-label" }, "NETWORK / MANAGEMENT");
  for (let i = 0; i < rear.nic_ports; i++) {
    addPort(g, portStart + i * 27, portY, String(i + 1), 22, "ethernet",
      device, `rear-nic-${i + 1}`, `Network port ${i + 1}`);
  }
  let auxX = portStart + rear.nic_ports * 28 + 8;
  if (rear.idrac || rear.ilo) {
    const name = rear.ilo ? "iLO" : "iDRAC";
    addPort(g, auxX, portY, "BMC", h > 42 ? 28 : 18, "service", device, "rear-bmc", name);
    auxX += 37;
  }
  if (rear.serial) {
    addPort(g, auxX, portY + 1, "SER", 14, "service", device, "rear-serial", "Serial console");
    auxX += 25;
  }
  if (rear.vga) {
    addPort(g, auxX, portY, "VGA", 22, "service", device, "rear-vga", "Rear VGA");
    auxX += 29;
  }
  for (let i = 0; i < rear.usb; i++) {
    addPort(g, auxX + i * 13, portY, "", 11, "service", device, `rear-usb-${i + 1}`, `Rear USB ${i + 1}`);
  }
  const rearBayCount = variant?.rear_bays || 0;
  const pciCount = rearBayCount ? Math.min(rear.pcie, 2) : rear.pcie;
  const pciStart = PANEL_X + 20;
  const pciLabelHeight = h > 60 ? 11 : 0;
  if (pciLabelHeight) add(g, "text", { x: pciStart, y: y + 11, class: "svg-group-label" }, "PCIe EXPANSION");
  for (let i = 0; i < pciCount; i++) {
    const slotWidth = rearBayCount ? 42 : 52;
    const px = pciStart + i * (slotWidth + 6);
    const slot = add(g, "rect", {
      x: px,
      y: y + 7 + pciLabelHeight,
      width: slotWidth,
      height: Math.max(13, h - 14 - pciLabelHeight),
      rx: 1,
      class: "svg-slot",
    });
    add(slot, "title", {}, `PCIe expansion slot ${i + 1}`);
    add(g, "line", { x1: px + 4, y1: y + h / 2, x2: px + slotWidth - 5, y2: y + h / 2, class: "svg-slot-line" });
  }
  if (rearBayCount > 0) {
    const width = 28;
    const driveX = PANEL_X + PANEL_WIDTH - 2 * (width + 6) - 13 - 92 * 2 - 8;
    for (let i = 0; i < rearBayCount; i++) addDrive(g, driveX + i * (width + 6), y + (h - 30) / 2, width, 30, i);
    add(g, "text", { x: driveX, y: y + 9, class: "svg-mini-label" }, "REAR DRIVE BAYS");
  }
  const customPorts = !model ? (device.rear_ports || []) : [];
  customPorts.forEach((port, index) => addPort(g, portStart + index * 32, portY, port, 24,
    "ethernet", device, `rear-custom-${index + 1}`, port));
}

function componentStatus(component) {
  if (!component) return "unknown";
  const health = String(component.health || "").toLowerCase();
  const state = String(component.state || "").toLowerCase();
  if (["critical", "failed"].includes(health) || ["absent", "disabled"].includes(state)) return "critical";
  if (health === "warning") return "warning";
  if (health === "ok" || state === "enabled" || state === "on") return "good";
  return "unknown";
}

function drawPowerModules(parent, rear, y, height, device) {
  const monitored = deviceHealth(device);
  const supplies = monitored?.telemetry?.power_supplies || [];
  const missing = monitored?.missing_components?.power_supplies || [];
  const count = rear.psus ?? 2;
  if (count < 1) return;
  const width = count > 1 ? 92 : 100;
  const gap = 8;
  const totalWidth = count * width + (count - 1) * gap;
  const side = rear.psu_side || "right";
  const startX = side === "left" ? PANEL_X + 20 : PANEL_X + PANEL_WIDTH - 13 - totalWidth;
  const moduleHeight = Math.min(44, height - 8);
  const moduleY = y + (height - moduleHeight) / 2;
  for (let index = 0; index < count; index++) {
    drawPowerModule(parent, startX + index * (width + gap), moduleY, width, moduleHeight,
      `PSU ${index + 1}`,
      componentSlotStatus(supplies, missing, index, "psu"));
  }
}

function drawPowerModule(parent, x, y, width, height, label, state = "unknown") {
  const module = add(parent, "rect", { x, y, width, height, rx: 2, class: `svg-psu status-${state}` });
  add(module, "title", {}, label);
  const cx = x + width * .53, cy = y + height * .48;
  add(parent, "circle", { cx, cy, r: Math.max(4, Math.min(9, height * .24)), class: "svg-fan-ring" });
  for (let i = 0; i < 8; i++) {
    const angle = Math.PI * 2 * i / 8;
    add(parent, "line", { x1: cx, y1: cy, x2: cx + Math.cos(angle) * 6, y2: cy + Math.sin(angle) * 6, class: "svg-fan-blade" });
  }
  add(parent, "circle", { cx: x + width - 8, cy: y + 8, r: 2, class: `svg-led status-${state}` });
  if (height >= 24) add(parent, "text", { x: x + width / 2, y: y + height - 3, class: "svg-mini-label", "text-anchor": "middle" }, label);
}

function drawFanGroup(parent, x, y, count, width, height) {
  const gap = 4;
  const fanW = (width - gap * (count - 1)) / count;
  for (let i = 0; i < count; i++) {
    const fx = x + i * (fanW + gap), fy = y;
    add(parent, "rect", { x: fx, y: fy, width: fanW, height, rx: 2, class: "svg-fan-cage" });
    const cx = fx + fanW / 2, cy = fy + height / 2;
    add(parent, "circle", { cx, cy, r: Math.max(3, Math.min(8, height * .28)), class: "svg-fan-ring" });
    for (let blade = 0; blade < 6; blade++) {
      const a = blade * Math.PI / 3;
      add(parent, "line", { x1: cx, y1: cy, x2: cx + Math.cos(a) * Math.max(2, fanW * .2), y2: cy + Math.sin(a) * Math.max(2, fanW * .2), class: "svg-fan-blade" });
    }
  }
}

function renderFace(rack, side) {
  const target = $(`#${side}-view`);
  target.replaceChildren();
  const height = 28 + rack.units * UNIT_PX;
  const drawing = svg("svg", { viewBox: `0 0 ${SVG_WIDTH} ${height}`, role: "img", "aria-label": `${rack.name} ${side} elevation, ${rack.units} rack units` });
  add(drawing, "rect", { x: 30, y: 9, width: 940, height: height - 16, rx: 5, class: "svg-rack-outline" });
  add(drawing, "rect", { x: 56, y: 16, width: 20, height: height - 30, class: "svg-rack-rail" });
  add(drawing, "rect", { x: 924, y: 16, width: 20, height: height - 30, class: "svg-rack-rail" });
  for (let unit = rack.units; unit >= 1; unit--) {
    const top = 18 + (rack.units - unit) * UNIT_PX;
    add(drawing, "text", { x: 47, y: top + 20, class: "svg-unit-label", "text-anchor": "end" }, String(unit).padStart(2, "0"));
    add(drawing, "text", { x: 953, y: top + 20, class: "svg-unit-label" }, String(unit).padStart(2, "0"));
    add(drawing, "line", { x1: 80, y1: top + UNIT_PX, x2: 920, y2: top + UNIT_PX, class: "svg-unit-line" });
    for (const x of [65, 935]) add(drawing, "circle", { cx: x, cy: top + 7, r: 1.5, class: "svg-hole" });
  }
  for (const device of inventory.devices) {
    if (device.rack_id !== rack.id) continue;
    const topUnit = device.unit + device.height - 1;
    const top = 18 + (rack.units - topUnit) * UNIT_PX;
    devicePanel(drawing, device, side, top, device.height * UNIT_PX);
  }
  target.append(drawing);
}

function refreshRackPicker() {
  const picker = $("#rack-picker");
  picker.replaceChildren();
  for (const rack of inventory.racks) {
    const option = make("option", "", `${rack.name} · ${rack.units}U`);
    option.value = rack.id;
    picker.append(option);
  }
  if (selectedRackId && inventory.racks.some(rack => rack.id === selectedRackId)) picker.value = selectedRackId;
  else selectedRackId = inventory.racks[0]?.id || null;
  picker.disabled = !inventory.racks.length;
  $("#edit-rack").disabled = !selectedRackId;
  $("#remove-rack").disabled = !selectedRackId;
}

function drawLists() {
  const unassigned = inventory.devices.filter(device => !device.rack_id);
  $("#unassigned-count").textContent = unassigned.length;
  const list = $("#unassigned");
  list.replaceChildren();
  if (!unassigned.length) list.append(make("div", "empty", "No unassigned devices."));
  for (const device of unassigned) {
    const row = make("div", "rack-list-row");
    row.append(make("div", "", `${device.name} · ${modelFor(device)?.name || "Custom device"} · ${device.height}U`));
    const actions = make("div", "item-actions");
    actions.append(actionButton("Place / edit", () => editDevice(device)), actionButton("Remove", () => removeDevice(device)));
    row.append(actions);
    list.append(row);
  }
  $("#cable-count").textContent = inventory.cables.length;
  const cables = $("#cables");
  cables.replaceChildren();
  if (!inventory.cables.length) cables.append(make("div", "empty", "No cable records. Monitoring still reports only device interfaces the selected protocol exposes."));
  for (const cable of inventory.cables) {
    const a = inventory.devices.find(device => device.id === cable.device_a)?.name || "Unknown";
    const b = inventory.devices.find(device => device.id === cable.device_b)?.name || "Unknown";
    const row = make("div", "rack-list-row");
    row.append(make("div", "", `${a} / ${cable.port_a} ↔ ${b} / ${cable.port_b} · ${cable.label || "Purpose not labeled"}`));
    const actions = make("div", "item-actions");
    actions.append(actionButton("Edit", () => editCable(cable)), actionButton("Remove", () => removeCable(cable)));
    row.append(actions);
    cables.append(row);
  }
}

function render() {
  refreshRackPicker();
  const rack = inventory.racks.find(item => item.id === selectedRackId);
  for (const side of ["front", "rear"]) {
    if (rack) renderFace(rack, side);
    else $(`#${side}-view`).replaceChildren(make("div", "empty", "Add a rack to begin."));
  }
  drawLists();
}

function actionButton(label, action) {
  const button = make("button", "secondary", label);
  button.type = "button";
  button.addEventListener("click", action);
  return button;
}

async function save(next) {
  if (saving) return;
  saving = true;
  $("#rack-feedback").textContent = "Saving inventory…";
  try {
    const response = await fetch("/api/racks", { method: "PUT", headers: {
      "Content-Type": "application/json", "X-Requested-With": "RackEditor",
    }, body: JSON.stringify({ revision, state: next }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Save failed (HTTP ${response.status})`);
    revision = result.revision;
    inventory = next;
    $("#rack-feedback").textContent = "Saved.";
    render();
  } catch (error) {
    $("#rack-feedback").textContent = error.message;
    throw error;
  } finally {
    saving = false;
  }
}

function fillField(container, name, label, value, options = null, type = "text") {
  const wrapper = make("label", "", label);
  const input = options ? document.createElement("select") : type === "textarea" ? document.createElement("textarea") : document.createElement("input");
  if (options) {
    for (const [optionValue, optionLabel] of options) {
      const option = make("option", "", optionLabel);
      option.value = optionValue;
      input.append(option);
    }
    input.value = value;
  } else if (type === "textarea") {
    input.value = value;
    input.rows = 3;
    input.maxLength = 2000;
  } else {
    input.type = type;
    input.value = value;
    if (type === "number") { input.min = 1; input.max = 60; }
    else input.maxLength = 120;
  }
  input.name = name;
  wrapper.append(input);
  container.append(wrapper);
  return input;
}

function wireDialog(dialog, form, fields, titleSelector, errorSelector, title, setup, onSave) {
  fields.replaceChildren();
  $(titleSelector).textContent = title;
  $(errorSelector).textContent = "";
  setup(fields);
  form.onsubmit = async event => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      await onSave(data);
      dialog.close();
    } catch (failure) {
      $(errorSelector).textContent = failure.message;
    }
  };
  dialog.showModal();
}

function editRack(rack = null) {
  const dialog = $("#rack-editor"), form = $("#rack-form"), fields = $("#rack-fields");
  wireDialog(dialog, form, fields, "#rack-editor-title", "#rack-error",
    rack ? "Edit rack" : "Add rack", fields => {
      fillField(fields, "name", "Rack name", rack?.name || "").required = true;
      fillField(fields, "units", "Rack height (U)", rack?.units || 42, null, "number").required = true;
    },
    async data => {
      const next = structuredClone(inventory);
      const value = { id: rack?.id || uid(), name: data.get("name").trim(), units: Number(data.get("units")) };
      if (rack) next.racks[next.racks.findIndex(item => item.id === rack.id)] = value;
      else { next.racks.push(value); selectedRackId = value.id; }
      await save(next);
    });
}

function editPort(device, key, description) {
  const dialog = $("#port-editor"), form = $("#port-form"), fields = $("#port-fields");
  const current = device.port_config[key] || { label: "", mac: "", ip: "", vlan: "", notes: "" };
  wireDialog(dialog, form, fields, "#port-editor-title", "#port-error", `Configure ${description}`, container => {
    fillField(container, "label", "Port label / purpose", current.label);
    fillField(container, "mac", "MAC address", current.mac);
    fillField(container, "ip", "IP address (optional)", current.ip);
    fillField(container, "vlan", "VLAN ID (optional)", current.vlan);
    fillField(container, "notes", "Notes", current.notes, null, "textarea");
  }, async data => {
    const config = {
      label: data.get("label").trim(),
      mac: data.get("mac").trim(),
      ip: data.get("ip").trim(),
      vlan: data.get("vlan").trim(),
      notes: data.get("notes").trim(),
    };
    if (config.mac && !/^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$/i.test(config.mac)) {
      throw new Error("Enter a MAC address in 00:11:22:33:44:55 format.");
    }
    if (config.ip && !/^[0-9a-f:.]+$/i.test(config.ip)) throw new Error("Enter a valid IPv4 or IPv6 address.");
    if (config.vlan && (!/^\d+$/.test(config.vlan) || Number(config.vlan) < 1 || Number(config.vlan) > 4094)) {
      throw new Error("VLAN ID must be between 1 and 4094.");
    }
    const next = structuredClone(inventory);
    const savedDevice = next.devices.find(item => item.id === device.id);
    if (Object.values(config).some(Boolean)) savedDevice.port_config[key] = config;
    else delete savedDevice.port_config[key];
    await save(next);
  });
}

function matchingCatalogModel(details) {
  const foundName = details?.Model;
  const foundManufacturer = details?.Manufacturer;
  if (typeof foundName !== "string" || typeof foundManufacturer !== "string") return null;
  const normalizedName = foundName.toLowerCase();
  const normalizedManufacturer = foundManufacturer.toLowerCase();
  return catalog.models.find(model =>
    normalizedName.includes(model.name.toLowerCase())
    && normalizedManufacturer.includes(model.manufacturer.toLowerCase())
  ) || null;
}

function discoveredHardwareDetails(details) {
  const asText = value => typeof value === "string" ? value.trim() : "";
  const lines = values => values.filter(Boolean).join("\n").slice(0, 1000);
  const processors = Array.isArray(details?.Processors) ? details.Processors : [];
  const memoryModules = Array.isArray(details?.MemoryModules) ? details.MemoryModules : [];
  const resources = Array.isArray(details?.Resources) ? details.Resources : [];
  const drives = Array.isArray(details?.Drives) ? details.Drives : [];
  const powerSupplies = Array.isArray(details?.PowerSupplies) ? details.PowerSupplies : [];
  const networkInterfaces = Array.isArray(details?.NetworkInterfaces) ? details.NetworkInterfaces : [];
  const cpuSummary = details?.ProcessorSummary || {};
  const memorySummary = details?.MemorySummary || {};
  const memoryTotal = Number(memorySummary.TotalSystemMemoryGiB);
  const moduleTotalMiB = memoryModules.reduce((total, item) =>
    total + (Number(item.CapacityMiB || item.SizeMiB) || 0), 0);
  const cpuLines = [];
  if (cpuSummary.Count || cpuSummary.Model) {
    cpuLines.push([cpuSummary.Count ? `${cpuSummary.Count} socket(s)` : "", cpuSummary.Model].filter(Boolean).join(" · "));
  }
  cpuLines.push(...processors.map(item =>
    [item.Model, item.TotalCores ? `${item.TotalCores} cores` : "", item.TotalThreads ? `${item.TotalThreads} threads` : ""]
      .filter(Boolean).join(" · ")));
  const cpuSensor = resources.find(item => String(item.name || "").toLowerCase() === "cpu");
  if (!cpuLines.length && cpuSensor) cpuLines.push(`${cpuSensor.value ?? "Reported"} ${cpuSensor.unit || ""}`.trim());
  const memoryLines = [];
  if (Number.isFinite(memoryTotal) && memoryTotal > 0) memoryLines.push(`${memoryTotal} GiB total`);
  else if (moduleTotalMiB > 0) memoryLines.push(`${(moduleTotalMiB / 1024).toFixed(1)} GiB across discovered modules`);
  memoryLines.push(...memoryModules.map(item =>
    [item.DeviceLocator, item.Model, item.CapacityMiB || item.SizeMiB ? `${item.CapacityMiB || item.SizeMiB} MiB` : ""]
      .filter(Boolean).join(" · ")));
  const memorySensor = resources.find(item => String(item.name || "").toLowerCase() === "memory");
  if (!memoryLines.length && memorySensor) memoryLines.push(`${memorySensor.value ?? "Reported"} ${memorySensor.unit || ""}`.trim());
  const stringifyItems = (items, fields) => lines(items.map(item =>
    fields.map(([key, label]) => {
      const value = item[key] ?? item[key.toLowerCase()];
      return value ? `${label}: ${value}` : "";
    }).filter(Boolean).join(" · ")));
  return {
    hostname: asText(details?.HostName || details?.Name || details?.["System name"]),
    manufacturer: asText(details?.Manufacturer),
    model: asText(details?.Model),
    serial_number: asText(details?.SerialNumber),
    cpu: lines(cpuLines),
    memory: lines(memoryLines),
    storage: stringifyItems(drives, [["Name", "Bay"], ["Model", "Model"], ["CapacityBytes", "Capacity"], ["health", "Health"], ["state", "State"]]),
    power_supplies: stringifyItems(powerSupplies, [["Name", "Name"], ["Model", "Model"], ["health", "Health"], ["state", "State"], ["Status", "Status"]]),
    network_interfaces: stringifyItems(networkInterfaces, [["Name", "Name"], ["MACAddress", "MAC"], ["PermanentMACAddress", "Permanent MAC"], ["LinkStatus", "Link"]]),
  };
}

const discoveryGapFields = [
  ["Host name", "hostname"],
  ["Manufacturer", "manufacturer"],
  ["Model", "model"],
  ["Serial number", "serial_number"],
  ["Processor", "cpu"],
  ["Memory", "memory"],
  ["Drive inventory", "storage"],
  ["Power-supply inventory", "power_supplies"],
  ["Network-interface inventory", "network_interfaces"],
];

function refreshDiscoveryResult(device) {
  const details = device?.discovery?.details;
  const health = device ? deviceHealth(device) : null;
  const monitorText = device?.discovery
    ? `\n\nContinuous monitoring: ${health?.status || "enrolling"}${health?.checked_at ? ` · checked ${health.checked_at}` : ""}`
    : "";
  $("#discovery-result").textContent = details
    ? `${JSON.stringify(details, null, 2)}${monitorText}`
    : "Save the device management IP and a credential profile before discovery.";
  $("#apply-discovered-model").hidden = !matchingCatalogModel(details);
  const coverage = $("#discovery-coverage");
  coverage.replaceChildren();
  coverage.hidden = !device?.discovery;
  if (!device?.discovery) return;
  const heading = make("strong", "", "Discovery completeness");
  coverage.append(heading);
  const list = make("ul", "");
  const gaps = Array.isArray(device.discovery.unavailable) ? device.discovery.unavailable : null;
  for (const gap of gaps || []) {
    const mapped = discoveryGapFields.find(([prefix]) => gap.startsWith(prefix));
    const input = mapped && $("#device-form").elements.namedItem(`hardware_${mapped[1]}`);
    const filled = input && input.value.trim();
    const item = make("li", filled ? "gap-filled" : "gap-missing");
    item.textContent = filled ? `${gap} — supplied in the editor.` : `${gap} — fill in the matching field above.`;
    list.append(item);
  }
  if (gaps && !gaps.length) {
    const item = make("li", "gap-filled", "All reportable discovery fields were returned.");
    list.append(item);
  } else if (!gaps) {
    list.append(make("li", "", "This saved discovery predates the field-completeness report. Run discovery again to see unavailable fields."));
  }
  coverage.append(list);
  const note = make("p", "form-help",
    "Rack/U placement, drive-bay variant, and cable connections need physical confirmation. Enter any missing facts above and choose Save hardware.");
  coverage.append(note);
}

async function discoverDevice(device, form, onDiscovered) {
  const data = new FormData(form);
  const saved = inventory.devices.find(item => item.id === device.id);
  if (!saved) throw new Error("Save this hardware before starting discovery.");
  if (!saved.management_ip || !saved.credential_ref) {
    throw new Error("Save a private management IP and a Docker Swarm credential profile before discovery.");
  }
  if (data.get("management_ip").trim() !== (saved.management_ip || "")
      || data.get("management_protocol") !== saved.management_protocol
      || data.get("credential_ref").trim() !== (saved.credential_ref || "")) {
    throw new Error("Save the management IP and credential profile before discovery.");
  }
  const button = $("#discover-device");
  button.disabled = true;
  $("#discovery-result").textContent = "Connecting to the configured management IP…";
  try {
    const response = await fetch(`/api/devices/${encodeURIComponent(device.id)}/discover`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "RackEditor" },
      body: JSON.stringify({
        revision,
        protocol: saved.management_protocol,
        credential_ref: saved.credential_ref,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `Discovery failed (HTTP ${response.status})`);
    revision = result.revision;
    saved.discovery = result.discovery;
    onDiscovered(saved.discovery);
    await refreshHealth();
    refreshDiscoveryResult(saved);
    render();
  } catch (error) {
    $("#discovery-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function editDevice(device = null) {
  const dialog = $("#device-editor"), form = $("#device-form"), fields = $("#device-fields");
  dialog.dataset.deviceId = device?.id || "";
  const currentRack = selectedRackId || "";
  const models = catalog.models;
  const modelOptions = [["custom", "Custom / unlisted hardware"], ...models.map(model => [model.id, `${model.manufacturer} ${model.name}`])];
  wireDialog(dialog, form, fields, "#device-editor-title", "#device-error", device ? "Edit hardware" : "Add hardware", container => {
    fillField(container, "name", "Inventory name", device?.name || "").required = true;
    const selectedModel = device?.model_id || "custom";
    const model = fillField(container, "model_id", "Hardware model", selectedModel, modelOptions);
    const deviceType = fillField(container, "type", "Device role", device?.type || "hypervisor", [
      ["hypervisor", "Hypervisor / server"], ["storage", "Storage / NAS"], ["network", "Network"], ["controller", "Controller"], ["other", "Other"],
    ]);
    const variant = fillField(container, "variant_id", "Drive-bay configuration", "", []);
    let confirmVariantSelection = false;
    const rack = fillField(container, "rack_id", "Rack", device?.rack_id || currentRack, [["", "Unassigned"], ...inventory.racks.map(item => [item.id, item.name])]);
    fillField(container, "unit", "Starting rack unit (bottom)", device?.unit || 1, null, "number");
    const custom = make("div", "custom-device-fields");
    fillField(custom, "height", "Rack height (U)", device?.height || 1, null, "number");
    fillField(custom, "front_ports", "Front labels (one per line)", (device?.front_ports || []).join("\n"), null, "textarea");
    fillField(custom, "rear_ports", "Rear labels (one per line)", (device?.rear_ports || []).join("\n"), null, "textarea");
    fillField(custom, "drives", "Drive-bay labels (one per line)", (device?.drives || []).join("\n"), null, "textarea");
    container.append(custom);
    const hardwareDetails = device?.hardware_details || {};
    for (const [key, label] of [
      ["hostname", "Detected / known host name"],
      ["manufacturer", "Manufacturer"],
      ["model", "Model or product description"],
      ["serial_number", "Serial number"],
      ["cpu", "Processor details"],
      ["memory", "Memory details"],
      ["storage", "Drive / storage inventory"],
      ["power_supplies", "Power-supply inventory"],
      ["network_interfaces", "Network-interface inventory"],
    ]) {
      const input = fillField(container, `hardware_${key}`, label, hardwareDetails[key] || "", null, "textarea");
      input.maxLength = 1000;
      input.placeholder = "Auto-filled when the management interface reports this information";
    }
    const managementIp = fillField(container, "management_ip", "Management IP (private network)", device?.management_ip || "");
    managementIp.placeholder = "e.g. 10.10.0.25";
    managementIp.autocomplete = "off";
    fillField(container, "management_protocol", "Discovery protocol", device?.management_protocol || "redfish", [
      ["redfish", "Redfish (iDRAC / iLO)"],
      ["snmp", "SNMP v2c (read-only system details)"],
      ["ipmi", "IPMI LAN+ (read-only BMC details)"],
    ]);
    const credential = fillField(container, "credential_ref", "Docker Swarm credential profile", device?.credential_ref || "");
    credential.placeholder = "profile-name";
    credential.autocomplete = "off";
    const credentialHelp = make("p", "form-help",
      "Do not enter a password here. Put BMC usernames and passwords (or the SNMP community) in the noc-device-credentials Docker Swarm secret, then enter that profile's name above. See README.md for setup.");
    container.append(credentialHelp);
    refreshDiscoveryResult(device);
    $("#discover-device").hidden = !device;
    $("#discover-device").disabled = false;
    container.addEventListener("input", () => refreshDiscoveryResult(device));
    container.addEventListener("change", () => refreshDiscoveryResult(device));
    const applyDiscovery = (discovery, forceModel = false) => {
      if (!discovery?.details) return;
      const suggestions = discoveredHardwareDetails(discovery.details);
      for (const [key, value] of Object.entries(suggestions)) {
        const input = form.elements.namedItem(`hardware_${key}`);
        const previousValue = hardwareDetails[key] || "";
        if (value && (!input.value.trim() || input.value.trim() === previousValue)) input.value = value;
      }
      const detectedModel = matchingCatalogModel(discovery.details);
      const modelUnchanged = model.value === (device?.model_id || "custom");
      if (detectedModel && (forceModel || modelUnchanged)) {
        const modelChanged = model.value !== detectedModel.id;
        model.value = detectedModel.id;
        if (modelChanged) confirmVariantSelection = true;
        updateModel();
        if (detectedModel.family?.toLowerCase().includes("switch")
            && deviceType.value === (device?.type || "hypervisor")) deviceType.value = "network";
        updateModel();
      }
      refreshDiscoveryResult({ ...device, discovery });
    };
    $("#discover-device").onclick = () => discoverDevice(device, form, discovery => applyDiscovery(discovery))
      .catch(error => { $("#discovery-result").textContent = error.message; });
    $("#apply-discovered-model").onclick = () => {
      const current = inventory.devices.find(item => item.id === device?.id);
      applyDiscovery(current?.discovery, true);
    };
    function updateModel() {
      const chosen = models.find(item => item.id === model.value);
      const configurations = chosen?.variants || [];
      const selectedVariant = variant.value;
      variant.replaceChildren();
      if (chosen && confirmVariantSelection) {
        const prompt = make("option", "", "Confirm the physical drive-bay configuration");
        prompt.value = "";
        variant.append(prompt);
      }
      for (const item of configurations) {
        const option = make("option", "", item.label);
        option.value = item.id;
        variant.append(option);
      }
      variant.disabled = !chosen;
      variant.required = Boolean(chosen && confirmVariantSelection);
      variant.closest("label").hidden = !chosen;
      custom.hidden = Boolean(chosen);
      if (configurations.some(item => item.id === selectedVariant)) variant.value = selectedVariant;
      else if (confirmVariantSelection && chosen) variant.value = "";
      else if (chosen) variant.value = configurations[0]?.id || "";
      const detail = $("#model-detail");
      detail.replaceChildren();
      if (chosen) {
        const configuration = configurations.find(item => item.id === variant.value);
        const configurationLabel = configuration?.label || (
          confirmVariantSelection ? "confirm the physical bay configuration" : "configuration"
        );
        const text = make("span", "", `${chosen.height}U · ${configurationLabel} · schematic model`);
        detail.append(text);
        if (chosen.source) {
          const link = make("a", "text-link", "Manufacturer documentation ↗");
          link.href = chosen.source;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          detail.append(link);
        }
      } else {
        detail.textContent = "Custom hardware uses a generic faceplate. Enter the labels you know; unknown component activity stays flagged.";
      }
    }
    model.value = selectedModel;
    updateModel();
    const activeVariant = device?.variant_id || models.find(item => item.id === selectedModel)?.variants[0]?.id;
    if (activeVariant) variant.value = activeVariant;
    updateModel();
    model.addEventListener("change", () => { updateModel(); });
    variant.addEventListener("change", updateModel);
  }, async data => {
    const model = models.find(item => item.id === data.get("model_id"));
    const variant = model?.variants.find(item => item.id === data.get("variant_id")) || model?.variants[0];
    const rackId = data.get("rack_id") || null;
    const managementIp = data.get("management_ip").trim() || null;
    const managementProtocol = data.get("management_protocol");
    const credentialRef = data.get("credential_ref").trim() || null;
    const savedHardwareDetails = {};
    for (const key of ["hostname", "manufacturer", "model", "serial_number", "cpu", "memory", "storage", "power_supplies", "network_interfaces"]) {
      savedHardwareDetails[key] = data.get(`hardware_${key}`).trim();
    }
    const connectionChanged = device && (
      managementIp !== device.management_ip
      || managementProtocol !== device.management_protocol
      || credentialRef !== device.credential_ref
    );
    const next = structuredClone(inventory);
    const entry = {
      id: device?.id || uid(),
      name: data.get("name").trim(),
      type: data.get("type"),
      rack_id: rackId,
      unit: rackId ? Number(data.get("unit")) : null,
      height: model ? model.height : Number(data.get("height")),
      front_ports: model ? [] : splitLabels(data.get("front_ports")),
      rear_ports: model ? [] : splitLabels(data.get("rear_ports")),
      drives: model ? [] : splitLabels(data.get("drives")),
      model_id: model?.id || null,
      variant_id: model ? variant.id : null,
      port_config: device && device.model_id === (model?.id || null) ? device.port_config : {},
      hardware_details: savedHardwareDetails,
      management_ip: managementIp,
      management_protocol: managementProtocol,
      credential_ref: credentialRef,
      discovery: connectionChanged ? null : device?.discovery || null,
    };
    if (device) next.devices[next.devices.findIndex(item => item.id === device.id)] = entry;
    else next.devices.push(entry);
    await save(next);
  });
}

function splitLabels(value) {
  const labels = value.split(/\r?\n/).map(label => label.trim()).filter(Boolean);
  if (labels.length > 48 || labels.some(label => label.length > 40)) throw new Error("Use up to 48 labels, with at most 40 characters each.");
  return labels;
}

async function removeDevice(device) {
  if (!confirm(`Remove ${device.name}? Its cable records will also be removed.`)) return;
  const next = structuredClone(inventory);
  next.devices = next.devices.filter(item => item.id !== device.id);
  next.cables = next.cables.filter(cable => cable.device_a !== device.id && cable.device_b !== device.id);
  await save(next);
}

function editCable(cable = null) {
  if (!inventory.devices.length) { $("#rack-feedback").textContent = "Add hardware before recording a connection."; return; }
  const dialog = $("#cable-editor"), form = $("#cable-form"), fields = $("#cable-fields");
  const choices = inventory.devices.map(device => [device.id, device.name]);
  wireDialog(dialog, form, fields, "#cable-editor-title", "#cable-error",
    cable ? "Edit connection" : "Add connection", container => {
      fillField(container, "device_a", "Device A", cable?.device_a || choices[0][0], choices);
      fillField(container, "port_a", "Port A", cable?.port_a || "").required = true;
      fillField(container, "device_b", "Device B", cable?.device_b || choices[0][0], choices);
      fillField(container, "port_b", "Port B", cable?.port_b || "").required = true;
      fillField(container, "label", "Purpose (optional)", cable?.label || "");
    },
    async data => {
      const next = structuredClone(inventory);
      const entry = { id: cable?.id || uid(), device_a: data.get("device_a"), port_a: data.get("port_a").trim(),
        device_b: data.get("device_b"), port_b: data.get("port_b").trim(), label: data.get("label").trim() };
      if (cable) next.cables[next.cables.findIndex(item => item.id === cable.id)] = entry;
      else next.cables.push(entry);
      await save(next);
    });
}

async function removeCable(cable) {
  if (!confirm(`Remove the connection from ${cable.port_a} to ${cable.port_b}?`)) return;
  const next = structuredClone(inventory);
  next.cables = next.cables.filter(item => item.id !== cable.id);
  await save(next);
}

async function removeSelectedRack() {
  const rack = inventory.racks.find(item => item.id === selectedRackId);
  if (!rack || !confirm(`Remove ${rack.name}? Devices will become unassigned.`)) return;
  const next = structuredClone(inventory);
  next.racks = next.racks.filter(item => item.id !== rack.id);
  for (const device of next.devices) if (device.rack_id === rack.id) { device.rack_id = null; device.unit = null; }
  selectedRackId = next.racks[0]?.id || null;
  await save(next);
}

async function load() {
  try {
    const [inventoryResponse, catalogResponse] = await Promise.all([
      fetch("/api/racks", { cache: "no-store" }), fetch("/api/catalog", { cache: "no-store" }),
    ]);
    if (!inventoryResponse.ok || !catalogResponse.ok) throw new Error("Could not load rack inventory or hardware catalog.");
    const result = await inventoryResponse.json();
    revision = result.revision;
    inventory = result.state;
    catalog = await catalogResponse.json();
    const healthResponse = await fetch("/api/health", { cache: "no-store" });
    if (!healthResponse.ok) throw new Error("Device health data could not be loaded.");
    healthStates = (await healthResponse.json()).devices;
    render();
  } catch (error) {
    $("#rack-feedback").textContent = error.message;
    for (const side of ["front", "rear"]) $(`#${side}-view`).replaceChildren(make("div", "empty", "Rack data is unavailable."));
  }
}

async function refreshHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    healthStates = (await response.json()).devices;
    const activeDevice = (inventory?.devices || []).find(item => item.id === $("#device-editor").dataset.deviceId);
    if (activeDevice && $("#device-editor").open) refreshDiscoveryResult(activeDevice);
    render();
  } catch (error) {
    console.error("Unable to refresh device health:", error);
  }
}

async function refreshLiveData() {
  if (!inventory || saving) return;
  try {
    const [inventoryResponse, healthResponse] = await Promise.all([
      fetch("/api/racks", { cache: "no-store" }),
      fetch("/api/health", { cache: "no-store" }),
    ]);
    if (!inventoryResponse.ok || !healthResponse.ok) throw new Error("Live rack data could not be loaded.");
    const snapshot = await inventoryResponse.json();
    const nextHealth = (await healthResponse.json()).devices;
    let changed = false;
    if (snapshot.revision !== revision && !document.querySelector("dialog[open]")) {
      revision = snapshot.revision;
      inventory = snapshot.state;
      changed = true;
    }
    if (JSON.stringify(nextHealth) !== JSON.stringify(healthStates)) {
      healthStates = nextHealth;
      changed = true;
    }
    const deviceDialog = $("#device-editor");
    const activeDevice = inventory.devices.find(item => item.id === deviceDialog.dataset.deviceId);
    if (activeDevice && deviceDialog.open && !$("#discover-device").disabled) refreshDiscoveryResult(activeDevice);
    if (changed) render();
  } catch (error) {
    console.error("Unable to refresh live rack data:", error);
  }
}

$("#rack-picker").addEventListener("change", event => { selectedRackId = event.target.value || null; render(); });
$("#add-rack").addEventListener("click", () => editRack());
$("#edit-rack").addEventListener("click", () => {
  const rack = inventory.racks.find(item => item.id === selectedRackId);
  if (rack) editRack(rack);
});
$("#remove-rack").addEventListener("click", () => removeSelectedRack().catch(error => { $("#rack-feedback").textContent = error.message; }));
$("#add-device").addEventListener("click", () => editDevice());
$("#add-cable").addEventListener("click", () => {
  const cableDetails = document.querySelectorAll(".rack-details")[1];
  cableDetails.open = true;
  cableDetails.scrollIntoView({ behavior: "smooth", block: "nearest" });
  editCable();
});
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
load();
scheduleLiveRefresh(refreshLiveData);
