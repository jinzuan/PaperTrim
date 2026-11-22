export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function esc(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function icon(name, className = "icon") {
  return `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

export function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function formatCount(n) {
  return new Intl.NumberFormat("zh-CN").format(Number(n) || 0);
}

export function naturalCompare(a, b) {
  return String(a).localeCompare(String(b), "zh-CN", {
    numeric: true, sensitivity: "base",
  });
}

export function debounce(fn, wait = 250) {
  let timer = 0;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  wrapped.flush = (...args) => { clearTimeout(timer); fn(...args); };
  return wrapped;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function loadImage(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("图片加载失败"));
    image.src = src;
  });
}

export function toast(message, type = "info", timeout = 4200) {
  const stack = $("#toastStack");
  if (!stack) return;
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  const iconName = type === "error" ? "warning" : type === "success" ? "check" : "info";
  node.innerHTML = `${icon(iconName)}<span>${esc(message)}</span>`;
  node.addEventListener("click", () => node.remove());
  stack.appendChild(node);
  setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    node.style.transition = "opacity .18s, transform .18s";
    setTimeout(() => node.remove(), 200);
  }, Math.max(1200, timeout));
}

export function confirmDialog({ title = "确认", message = "", okLabel = "确认", danger = false } = {}) {
  return new Promise((resolve) => {
    const modal = $("#confirmModal");
    if (!modal) { resolve(window.confirm(message)); return; }
    $("#confirmTitle").textContent = title;
    $("#confirmMessage").textContent = message;
    const ok = $("#confirmOk");
    ok.textContent = okLabel;
    ok.className = danger ? "btn danger-solid" : "btn primary";
    modal.hidden = false;
    const finish = (value) => {
      modal.hidden = true;
      $$("[data-confirm]", modal).forEach((node) => { node.onclick = null; });
      document.removeEventListener("keydown", onKey);
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
    };
    $$("[data-confirm]", modal).forEach((node) => {
      node.onclick = () => finish(node.dataset.confirm === "ok");
    });
    document.addEventListener("keydown", onKey);
    setTimeout(() => ok.focus(), 30);
  });
}

export function spinner(label = "处理中") {
  return `<span class="spinner" aria-label="${esc(label)}"></span>`;
}

export function showWarnings(container, warnings, confidence) {
  if (!container) return;
  const list = Array.isArray(warnings) ? warnings : [];
  if (!list.length) {
    container.innerHTML = `<div class="ok">${icon("check")}<span>边缘识别正常${confidence != null ? ` · 可信度 ${Math.round(confidence * 100)}%` : ""}</span></div>`;
    return;
  }
  container.innerHTML = list.map((warning) => {
    const info = warning.level === "info";
    return `<div class="warning-item ${info ? "info" : ""}">${icon(info ? "info" : "warning")}<span>${esc(warning.message || warning.code || "")}</span></div>`;
  }).join("");
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      return true;
    } catch {
      return false;
    }
  }
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
