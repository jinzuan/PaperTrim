const boot = window.__PAPERTRIM__ || {};
export const TOKEN = boot.token || "";
export const VERSION = boot.version || "";

function headers(extra = {}) {
  return { "X-PaperTrim-Token": TOKEN, ...extra };
}

async function request(path, options = {}) {
  const opts = { ...options, headers: headers(options.headers || {}) };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  let response;
  try {
    response = await fetch(path, opts);
  } catch (error) {
    throw new Error("无法连接本地服务，请确认启动窗口仍在运行。");
  }
  let data = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try { data = await response.json(); } catch { data = null; }
  }
  if (!response.ok) {
    const message = data && data.error ? data.error : `请求失败（${response.status}）`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function requestBlob(path, options = {}) {
  const opts = { ...options, headers: headers(options.headers || {}) };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      if (data && data.error) message = data.error;
    } catch { /* not json */ }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.blob();
}

export const api = {
  getSession: () => request("/api/session"),
  getImage: (id) => request(`/api/images/${encodeURIComponent(id)}`),
  patchImage: (id, patch) => request(`/api/images/${encodeURIComponent(id)}`, {
    method: "PATCH", body: patch,
  }),
  deleteImage: (id) => request(`/api/images/${encodeURIComponent(id)}`, { method: "DELETE" }),
  recompute: (id, params) => request(`/api/images/${encodeURIComponent(id)}/recompute`, {
    method: "POST", body: { params },
  }),
  deskew: (id) => request(`/api/images/${encodeURIComponent(id)}/deskew`, { method: "POST" }),
  alignEdge: (id, align) => request(`/api/images/${encodeURIComponent(id)}/align`, {
    method: "POST", body: { align },
  }),
  renderPreview: (id, output, maxSide = 1100, overlay = false) => requestBlob(
    `/api/images/${encodeURIComponent(id)}/render-preview`,
    { method: "POST", body: { output, max_side: maxSide, overlay } },
  ),
  batch: (ids, action, value = 0, params = null, extra = {}) =>
    request("/api/batch", { method: "POST", body: { ids, action, value, params, ...extra } }),
  saveOutputDefaults: (output) =>
    request("/api/output-defaults", { method: "POST", body: { output } }),
  sort: () => request("/api/sort", { method: "POST" }),
  importPaths: (paths, options = {}) =>
    request("/api/import/paths", { method: "POST", body: { paths, ...options } }),
  clearAll: () => request("/api/clear", { method: "POST", body: { confirm: true } }),
  setProject: (name) => request("/api/project", { method: "POST", body: { name } }),
  saveProject: () => request("/api/project/save", { method: "POST" }),
  listProjects: () => request("/api/projects"),
  newProject: (name) => request("/api/projects/new", { method: "POST", body: { name } }),
  openProject: (id) => request("/api/projects/open", { method: "POST", body: { id } }),
  getJob: (id) => request(`/api/jobs/${encodeURIComponent(id)}`),
  exportDefault: (kind) => request(`/api/export/default?kind=${encodeURIComponent(kind)}`),
  startExport: (payload) => request("/api/export", { method: "POST", body: payload }),
  openPath: (path, select = false) =>
    request("/api/open", { method: "POST", body: { path, select } }),
  uploadFiles: (files, options = {}) => {
    const form = new FormData();
    for (const file of files) form.append("files", file, file.name);
    form.append("dedupe", options.dedupe === false ? "false" : "true");
    form.append("deskew", options.deskew === false ? "false" : "true");
    if (options.params) form.append("params", JSON.stringify(options.params));
    return request("/api/import", { method: "POST", body: form });
  },
};
