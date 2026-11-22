import { api, VERSION } from "./api.js";
import { BatchSettings } from "./batch.js";
import { Editor } from "./editor.js";
import { ExportDialog } from "./export.js";
import {
  $, $$, clamp, confirmDialog, esc, formatBytes, formatCount, icon,
  naturalCompare, toast,
} from "./ui.js";

const state = {
  session: null,
  selected: new Set(),
  lastIndex: -1,
  jobs: new Map(),
  uploading: 0,
  editorOpen: false,
  lightbox: null,
  lightboxIndex: -1,
  recovered: false,
  loadSeq: 0,
  savingProject: false,
};

class App {
  constructor() {
    this.editor = new Editor(this);
    this.exporter = new ExportDialog(this);
    this.batchSettings = new BatchSettings(this);
    this._bind();
  }

  async init() {
    const version = $("#aboutVersion") || null;
    if (version) version.textContent = VERSION;
    try {
      await this.loadSession();
      this._handleHash();
      for (const job of (state.session?.jobs || [])) {
        const title = String(job.kind || "").startsWith("export") ? "导出" : "识别纸张边缘";
        this.trackJob(job, { title });
      }
      this._recoverPending();
    } catch (error) {
      toast(error.message || "无法连接本地服务，请刷新页面。", "error", 8000);
    }
  }

  get images() { return (state.session && state.session.images) || []; }

  get state() { return state; }

  imageById(id) { return this.images.find((item) => item.id === id) || null; }

  readyRecords() { return this.images.filter((item) => item.status === "ready"); }

  selectedRecords() { return this.images.filter((item) => state.selected.has(item.id)); }

  selectedReadyRecords() { return this.images.filter((item) => state.selected.has(item.id) && item.status === "ready"); }

  async loadSession({ silent = false } = {}) {
    const seq = ++state.loadSeq;
    const data = await api.getSession();
    if (seq !== state.loadSeq) return;
    this.setSession(data, { silent });
  }

  setSession(data, { silent = false } = {}) {
    state.session = data;
    const ids = new Set(data.images.map((item) => item.id));
    for (const id of Array.from(state.selected)) {
      if (!ids.has(id)) state.selected.delete(id);
    }
    const projectInput = $("#projectName");
    if (document.activeElement !== projectInput) {
      projectInput.value = data.project?.name || "未命名项目";
    }
    document.title = `${data.project?.name || "纸边"} · 纸边 PaperTrim`;
    this.render();
    if (!silent && !data.images.length) {
      // First launch: nothing to announce.
    }
  }

  render() {
    const images = this.images;
    const grid = $("#grid");
    const empty = $("#empty");
    const scrollY = window.scrollY;
    const ready = images.filter((item) => item.status === "ready").length;
    empty.hidden = images.length > 0;
    grid.hidden = images.length === 0;
    grid.innerHTML = images.map((record, index) => this._cardHtml(record, index)).join("");
    this.updateToolbar();
    this.renderJobBanner();
    window.scrollTo(0, scrollY);
    if (state.lightbox) {
      const list = this._lightboxRecords();
      state.lightbox = list;
      if (state.lightboxIndex >= list.length) state.lightboxIndex = list.length - 1;
      this._renderLightbox();
    }
  }

  _cardHtml(record, index) {
    const selected = state.selected.has(record.id);
    const ready = record.status === "ready";
    const pending = record.status === "pending";
    const error = record.status === "error";
    const warnings = record.warnings || [];
    const badgeBits = [];
    if (pending) badgeBits.push(`<span class="badge pending">${icon("refresh")}识别中</span>`);
    if (error) badgeBits.push(`<span class="badge error" title="${esc(record.error || "")}">${icon("warning")}识别失败</span>`);
    if (!pending && !error && warnings.length) {
      const title = warnings.map((item) => item.message).join("；");
      badgeBits.push(`<span class="badge warn" title="${esc(title)}">${icon("warning")}需检查</span>`);
    }
    if (record.manual) badgeBits.push(`<span class="badge manual">手动</span>`);
    const regionCount = Number(record.region_count) || 0;
    const enabledRegions = Number(record.enabled_regions) || 0;
    if (regionCount > 1) {
      const hidden = regionCount - enabledRegions;
      badgeBits.push(
        `<span class="badge regions" title="这张图识别到 ${regionCount} 块纸片${hidden ? `，其中 ${hidden} 块已弃选` : ""}">片 ${enabledRegions}/${regionCount}</span>`,
      );
    }
    if (record.output?.mode === "rect") badgeBits.push(`<span class="badge rot">完整矩形</span>`);
    if (record.rotation) badgeBits.push(`<span class="badge rot">${record.rotation}°</span>`);
    const thumb = ready && record.thumb_url ? record.thumb_url : record.source_url;
    const fallback = record.source_url || "";
    const dims = ready
      ? `${record.result_width || 0} × ${record.result_height || 0}`
      : `${record.width || 0} × ${record.height || 0}`;
    const skew = Math.abs(Number(record.fine_angle) || 0) >= 0.1
      ? `拉直 ${Number(record.fine_angle).toFixed(1)}°`
      : "";
    const sub = [
      `${dims} px`,
      record.status === "pending" ? "原图" : "",
      skew,
      record.manual ? "手动轮廓" : "",
      (Number(record.region_count) || 0) > 1
        ? `${Number(record.enabled_regions) || 0} 块纸片`
        : "",
    ].filter(Boolean).join(" · ");
    const warnTitle = warnings.map((item) => item.message).join("；");
    const confidence = record.confidence != null ? `可信度 ${Math.round(record.confidence * 100)}%` : "";
    return `
      <article class="card ${selected ? "selected" : ""} ${pending ? "pending" : ""} ${error ? "error" : ""}" data-id="${esc(record.id)}">
        <div class="card-check">
          <input type="checkbox" data-check ${selected ? "checked" : ""} aria-label="选择 ${esc(record.name)}">
        </div>
        <span class="card-index">${String(index + 1).padStart(2, "0")}</span>
        <div class="card-badges">${badgeBits.join("")}</div>
        <div class="card-thumb" data-open title="打开裁切与边缘编辑">
          <img src="${esc(thumb)}" alt="${esc(record.name)}" loading="lazy" draggable="false" data-fallback="${esc(fallback)}">
          ${pending ? `<div class="thumb-overlay"><div class="inner"><span class="spinner"></span><span>正在识别纸张边缘…</span></div></div>` : ""}
          ${error ? `<div class="thumb-overlay"><div class="inner">${icon("warning")}<span>需要重新识别</span></div></div>` : ""}
          ${ready ? `<div class="card-hover">
            <button data-act="preview" title="全屏查看">${icon("eye")}</button>
            <button data-act="left" title="左转 90°">${icon("rotate-left")}</button>
            <button data-act="right" title="右转 90°">${icon("rotate-right")}</button>
          </div>` : ""}
        </div>
        <div class="card-meta" data-open>
          <div class="card-name" title="${esc(record.name)}">${esc(record.name)}</div>
          <div class="card-sub" title="${esc([sub, confidence, warnTitle].filter(Boolean).join(" · "))}">
            <span>${esc(sub)}</span>${confidence ? `<span class="dot"></span><span>${esc(confidence)}</span>` : ""}
          </div>
        </div>
      </article>`;
  }

  updateToolbar() {
    const total = this.images.length;
    const selected = state.selected.size;
    const selectedReady = this.selectedReadyRecords().length;
    const ready = this.readyRecords().length;
    $("#selectionText").textContent = selected ? `${selected} 张已选` : "未选择";
    const selectAll = $("#selectAll");
    selectAll.checked = total > 0 && selected === total;
    selectAll.indeterminate = selected > 0 && selected < total;
    $$("[data-batch]").forEach((button) => {
      const action = button.dataset.batch;
      if (action === "delete" || action === "recompute") button.disabled = selected === 0;
      else button.disabled = selectedReady === 0;
    });
    $("#exportBtn").disabled = ready === 0;
    $("#browseBtn").disabled = ready === 0;
    $("#batchSettingsBtn").disabled = ready === 0;
    $("#sortBtn").disabled = total < 2;
    if (state.session) {
      $("#scopeAllCount").textContent = String(ready);
      $("#scopeSelCount").textContent = String(selectedReady);
    }
  }

  renderJobBanner() {
    const banner = $("#jobBanner");
    const entry = state.jobs.values().next().value;
    if (!entry && !state.uploading) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    if (!entry) {
      $("#jobTitle").textContent = "正在上传图片…";
      $("#jobMessage").textContent = `${state.uploading} 个文件正在复制到项目目录`;
      $("#jobBar").style.width = "12%";
      $("#jobCount").textContent = "";
      return;
    }
    const job = entry.detail || {};
    const total = Math.max(0, Number(job.total) || 0);
    const done = Math.max(0, Number(job.done) || 0);
    $("#jobTitle").textContent = entry.title;
    $("#jobMessage").textContent = job.message || "处理中…";
    const percent = total ? (job.status === "done" ? 100 : Math.min(99, Math.round(done / total * 100))) : 8;
    $("#jobBar").style.width = `${percent}%`;
    $("#jobCount").textContent = total ? `${done}/${total}` : "";
  }

  async trackJob(job, { title = "正在处理", onDone = null } = {}) {
    if (!job || !job.id) return;
    state.jobs.set(job.id, { ...job, detail: job, title });
    this.renderJobBanner();
    this._pollJob(job.id, title, onDone);
  }

  async _pollJob(id, title, onDone) {
    const entry = state.jobs.get(id);
    if (!entry) return;
    try {
      const job = await api.getJob(id);
      const current = state.jobs.get(id);
      if (!current) return;
      current.detail = job;
      current.status = job.status;
      this.renderJobBanner();
      if (job.status === "running") {
        setTimeout(() => this._pollJob(id, title, onDone), 450);
        return;
      }
      state.jobs.delete(id);
      this.renderJobBanner();
      await this.loadSession({ silent: true });
      if (job.status === "done") {
        const failed = job.result?.failed || [];
        if (failed.length) {
          toast(`${title}完成，但有 ${failed.length} 张失败：${failed[0].error || ""}`, "error", 7000);
        } else {
          toast(`${title}完成`, "success");
        }
        if (onDone) onDone(job);
      } else {
        toast(`${title}失败：${job.error || "未知错误"}`, "error", 8000);
      }
    } catch (error) {
      state.jobs.delete(id);
      this.renderJobBanner();
      toast(error.message || "任务查询失败", "error");
    }
  }

  // ----------------------------------------------------------------
  // Selection / grid

  _toggleSelect(id, force = null) {
    const value = force == null ? !state.selected.has(id) : force;
    if (value) state.selected.add(id);
    else state.selected.delete(id);
    const index = this.images.findIndex((item) => item.id === id);
    state.lastIndex = index;
    this._syncSelectionUi();
  }

  _selectRange(index) {
    if (index < 0 || !this.images.length) return;
    const start = state.lastIndex >= 0 ? state.lastIndex : index;
    const [a, b] = start < index ? [start, index] : [index, start];
    for (let i = a; i <= b; i += 1) state.selected.add(this.images[i].id);
    state.lastIndex = index;
    this._syncSelectionUi();
  }

  _syncSelectionUi() {
    $$("#grid .card").forEach((card) => {
      const selected = state.selected.has(card.dataset.id);
      card.classList.toggle("selected", selected);
      const checkbox = $("[data-check]", card);
      if (checkbox) checkbox.checked = selected;
    });
    this.updateToolbar();
  }

  _selectAll(value) {
    state.selected.clear();
    if (value) this.images.forEach((item) => state.selected.add(item.id));
    state.lastIndex = value && this.images.length ? this.images.length - 1 : -1;
    this._syncSelectionUi();
  }

  // ----------------------------------------------------------------
  // Opening

  async openEditor(id) {
    try {
      state.editorOpen = true;
      await this.editor.open(id);
      this.updateBodyLock();
    } catch (error) {
      state.editorOpen = false;
      toast(error.message || "无法打开这张图片", "error");
    }
  }

  onEditorClosed() {
    state.editorOpen = false;
    this.updateBodyLock();
  }

  onImageUpdated(detail) {
    if (!state.session || !detail) return;
    const index = this.images.findIndex((item) => item.id === detail.id);
    if (index < 0) return;
    state.session.images[index] = { ...state.session.images[index], ...detail };
    this._recount();
    this.render();
  }

  _recount() {
    const counts = { total: this.images.length, ready: 0, pending: 0, error: 0 };
    for (const item of this.images) {
      if (item.status === "ready") counts.ready += 1;
      else if (item.status === "error") counts.error += 1;
      else counts.pending += 1;
    }
    if (state.session) state.session.counts = counts;
  }

  // ----------------------------------------------------------------
  // Import

  async uploadFiles(fileList) {
    const files = Array.from(fileList || []).filter(Boolean);
    if (!files.length) return;
    state.uploading = files.length;
    this.renderJobBanner();
    try {
      const response = await api.uploadFiles(files, {
        dedupe: true,
        deskew: $("#autoDeskewUpload").checked,
      });
      state.uploading = 0;
      if (response.session) this.setSession(response.session, { silent: true });
      else await this.loadSession();
      const skipped = response.skipped || [];
      if (skipped.length) {
        const reasons = new Map();
        skipped.forEach((item) => {
          const reason = item.reason || "已跳过";
          reasons.set(reason, (reasons.get(reason) || 0) + 1);
        });
        const text = Array.from(reasons.entries()).map(([reason, count]) => `${reason} ${count} 张`).join("，");
        toast(text, "info", 6000);
      }
      if (response.job && response.job.total > 0) {
        await this.trackJob(response.job, { title: "识别纸张边缘" });
      } else if (!skipped.length) {
        toast("没有可导入的图片。", "info");
      }
    } catch (error) {
      state.uploading = 0;
      this.renderJobBanner();
      toast(error.message || "导入失败", "error", 7000);
    }
  }

  async importPaths() {
    const raw = $("#pathInput").value.trim();
    if (!raw) { toast("请填写至少一个路径。", "info"); return; }
    const paths = raw.split(/\r?\n/)
      .map((line) => line.trim().replace(/^"(.*)"$/, "$1"))
      .filter(Boolean);
    const options = {
      recursive: $("#pathRecursive").checked,
      dedupe: $("#pathDedupe").checked,
      deskew: $("#pathDeskew").checked,
    };
    $("#pathStart").disabled = true;
    try {
      const response = await api.importPaths(paths, options);
      $("#pathModal").hidden = true;
      this.updateBodyLock();
      if (response.session) this.setSession(response.session, { silent: true });
      const skipped = response.skipped || [];
      if (skipped.length) toast(`跳过 ${skipped.length} 张重复或不支持的图片。`, "info", 5000);
      if (response.job && response.job.total > 0) {
        await this.trackJob(response.job, { title: "识别纸张边缘" });
      } else {
        toast("没有导入新的图片。", "info");
      }
    } catch (error) {
      toast(error.message || "导入失败", "error", 7000);
    } finally {
      $("#pathStart").disabled = false;
    }
  }

  _recoverPending() {
    if (state.recovered || !state.session) return;
    state.recovered = true;
    const pending = this.images.filter((item) => item.status === "pending").map((item) => item.id);
    const active = state.session.jobs || [];
    if (!pending.length || active.length) return;
    api.batch(pending, "recompute").then((response) => {
      if (response.session) this.setSession(response.session, { silent: true });
      this.trackJob(response.job, { title: "继续识别纸张边缘" });
    }).catch(() => { /* user can retry manually */ });
  }

  // ----------------------------------------------------------------
  // Batch / single actions

  async runBatch(action) {
    const ids = Array.from(state.selected);
    if (!ids.length) { toast("请先选择图片。", "info"); return; }
    if (action === "delete") {
      const ok = await this.confirm({
        title: "删除选中的图片？",
        message: `将删除 ${ids.length} 张图片及其裁切结果。源图片副本也会从项目目录移除，此操作不可撤销。`,
        okLabel: "删除",
        danger: true,
      });
      if (!ok) return;
      try {
        const response = await api.batch(ids, "delete");
        state.selected.clear();
        if (response.session) this.setSession(response.session, { silent: true });
        else await this.loadSession();
        toast(`已删除 ${ids.length} 张图片`, "success");
      } catch (error) {
        toast(error.message || "删除失败", "error");
      }
      return;
    }

    if (action === "recompute") {
      const manual = this.images.filter((item) => state.selected.has(item.id) && item.manual).length;
      if (manual) {
        const ok = await this.confirm({
          title: "重新识别会覆盖手动修改",
          message: `选中的图片中有 ${manual} 张做过手动修正。重新识别会清除这些补边和顶点调整，确定继续吗？`,
          okLabel: "重新识别",
          danger: true,
        });
        if (!ok) return;
      }
      await this._startBatch(ids, "recompute", 0, "重新识别纸张边缘");
      return;
    }

    if (action === "deskew") {
      await this._startBatch(ids.filter((id) => {
        const record = this.imageById(id);
        return record && record.status === "ready";
      }), "deskew", 0, "自动拉直");
      return;
    }

    const rotateValue = action === "rotate-left" ? -90 : action === "rotate-right" ? 90 : 180;
    const readyIds = ids.filter((id) => {
      const record = this.imageById(id);
      return record && record.status === "ready";
    });
    if (!readyIds.length) { toast("选中的图片尚未识别完成。", "info"); return; }
    await this._startBatch(readyIds, "rotate", rotateValue, "旋转图片");
  }

  async _startBatch(ids, action, value, title) {
    if (!ids.length) { toast("没有可处理的图片。", "info"); return; }
    try {
      const response = await api.batch(ids, action, value);
      if (response.session) this.setSession(response.session, { silent: true });
      await this.trackJob(response.job, { title });
    } catch (error) {
      toast(error.message || `${title}失败`, "error");
    }
  }

  async rotateOne(id, delta) {
    const record = this.imageById(id);
    if (!record || record.status !== "ready") return;
    const rotation = ((Number(record.rotation || 0) + delta) % 360 + 360) % 360;
    try {
      const detail = await api.patchImage(id, { rotation });
      this.onImageUpdated(detail);
    } catch (error) {
      toast(error.message || "旋转失败", "error");
    }
  }

  async deleteOne(id) {
    const record = this.imageById(id);
    if (!record) return;
    const ok = await this.confirm({
      title: "删除这张图片？",
      message: `将删除「${record.name}」及其裁切结果，此操作不可撤销。`,
      okLabel: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      const response = await api.deleteImage(id);
      state.selected.delete(id);
      if (response.session) this.setSession(response.session, { silent: true });
      else await this.loadSession();
    } catch (error) {
      toast(error.message || "删除失败", "error");
    }
  }

  async sortImages() {
    try {
      const response = await api.sort();
      if (response.session) this.setSession(response.session, { silent: true });
      toast("已按文件名排序", "success");
    } catch (error) {
      toast(error.message || "排序失败", "error");
    }
  }

  async clearProject() {
    if (!this.images.length) { toast("当前项目已经是空的。", "info"); return; }
    const ok = await this.confirm({
      title: "清空当前项目？",
      message: `将删除项目中的 ${this.images.length} 张图片和全部裁切缓存。导出目录中的文件不会被删除。`,
      okLabel: "清空",
      danger: true,
    });
    if (!ok) return;
    try {
      const response = await api.clearAll();
      state.selected.clear();
      if (response.session) this.setSession(response.session, { silent: true });
      else await this.loadSession();
      toast("项目已清空", "success");
    } catch (error) {
      toast(error.message || "清空失败", "error");
    }
  }

  // ----------------------------------------------------------------
  // Export / lightbox

  openExport(kind = null) {
    if (!this.readyRecords().length) {
      toast("还没有可导出的图片，请先完成识别。", "info");
      return;
    }
    this.exporter.open(kind);
  }

  _lightboxRecords() { return this.readyRecords(); }

  openLightbox(id = null) {
    const list = this._lightboxRecords();
    if (!list.length) { toast("还没有可查看的裁切结果。", "info"); return; }
    let index = id ? list.findIndex((item) => item.id === id) : 0;
    if (index < 0) index = 0;
    state.lightbox = list;
    state.lightboxIndex = index;
    $("#lightbox").hidden = false;
    this.updateBodyLock();
    this._renderLightbox();
  }

  closeLightbox() {
    $("#lightbox").hidden = true;
    state.lightbox = null;
    state.lightboxIndex = -1;
    this.updateBodyLock();
  }

  _renderLightbox() {
    const list = state.lightbox;
    if (!list || !list.length) { this.closeLightbox(); return; }
    if (state.lightboxIndex < 0) state.lightboxIndex = 0;
    if (state.lightboxIndex >= list.length) state.lightboxIndex = list.length - 1;
    const record = list[state.lightboxIndex];
    const image = $("#lightboxImage");
    image.src = record.large_url || record.preview_url || "";
    image.alt = record.name || "";
    $("#lightboxName").textContent = record.name || "";
    $("#lightboxCounter").textContent = `${state.lightboxIndex + 1} / ${list.length}`;
  }

  stepLightbox(delta) {
    const list = state.lightbox;
    if (!list || !list.length) return;
    state.lightboxIndex = (state.lightboxIndex + delta + list.length) % list.length;
    this._renderLightbox();
  }

  updateBodyLock() {
    const any = $$(".modal").some((modal) => !modal.hidden)
      || ($("#lightbox") && !$("#lightbox").hidden);
    document.body.classList.toggle("modal-open", any);
  }

  async confirm(options) {
    const promise = confirmDialog(options);
    this.updateBodyLock();
    const result = await promise;
    this.updateBodyLock();
    return result;
  }

  // ----------------------------------------------------------------
  // Events

  _bind() {
    // Toolbar
    $("#selectAll").addEventListener("change", (event) => this._selectAll(event.target.checked));
    $$("[data-batch]").forEach((button) => {
      button.addEventListener("click", () => this.runBatch(button.dataset.batch));
    });
    $("#sortBtn").addEventListener("click", () => this.sortImages());
    $("#batchSettingsBtn").addEventListener("click", () => this.batchSettings.open());
    $("#exportBtn").addEventListener("click", (event) => {
      event.stopPropagation();
      this._toggleMenu("exportMenu");
    });
    $$("#exportMenu [data-export-kind]").forEach((button) => {
      button.addEventListener("click", () => {
        this._closeMenus();
        this.openExport(button.dataset.exportKind);
      });
    });
    $("#homeBtn").addEventListener("click", () => this.showHome());
    $("#homeSave").addEventListener("click", () => this.saveProjectNow());
    $("#homeNew").addEventListener("click", () => this.openNewProjectModal());
    $("#homeImport").addEventListener("click", () => {
      this.hideHome();
      $("#fileInput").click();
    });
    $("#projectGrid").addEventListener("click", (event) => {
      const card = event.target.closest("[data-project-id]");
      if (card) this.openProject(card.dataset.projectId);
    });
    $("#newProjectCreate").addEventListener("click", () => this.createProject());
    $("#newProjectName").addEventListener("keydown", (event) => {
      if (event.key === "Enter") this.createProject();
    });
    $$('[data-close="new-project"]').forEach((node) => {
      node.addEventListener("click", () => this.closeNewProjectModal());
    });
    $("#browseBtn").addEventListener("click", () => this.openLightbox());
    $("#emptyPick").addEventListener("click", () => $("#fileInput").click());
    $("#emptyFolder").addEventListener("click", () => $("#folderInput").click());

    // Grid delegation
    const grid = $("#grid");
    grid.addEventListener("click", (event) => this._onGridClick(event));
    grid.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-check]");
      if (!checkbox) return;
      const card = checkbox.closest(".card");
      if (!card) return;
      this._toggleSelect(card.dataset.id, checkbox.checked);
    });
    grid.addEventListener("error", (event) => {
      const image = event.target;
      if (!(image instanceof HTMLImageElement)) return;
      const fallback = image.dataset.fallback;
      if (fallback && image.src !== fallback) {
        image.dataset.fallback = "";
        image.src = fallback;
        return;
      }
      const thumb = image.closest(".card-thumb");
      if (thumb) {
        image.style.display = "none";
        if (!thumb.querySelector(".thumb-missing")) {
          const node = document.createElement("div");
          node.className = "thumb-missing";
          node.innerHTML = `${icon("image")}<span>预览不可用</span>`;
          thumb.appendChild(node);
        }
      }
    }, true);

    // File pickers
    $("#fileInput").addEventListener("change", (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      this.uploadFiles(files);
    });
    $("#folderInput").addEventListener("change", (event) => {
      const files = Array.from(event.target.files || []);
      event.target.value = "";
      this.uploadFiles(files);
    });

    // Menus
    $("#importBtn").addEventListener("click", (event) => {
      event.stopPropagation();
      this._toggleMenu("importMenu");
    });
    $("#menuBtn").addEventListener("click", (event) => {
      event.stopPropagation();
      this._toggleMenu("mainMenu");
    });
    $$("#importMenu [data-import]").forEach((button) => {
      button.addEventListener("click", () => {
        this._closeMenus();
        const action = button.dataset.import;
        if (action === "files") $("#fileInput").click();
        else if (action === "folder") $("#folderInput").click();
        else this.openPathModal();
      });
    });
    $$("#mainMenu [data-menu]").forEach((button) => {
      button.addEventListener("click", () => {
        this._closeMenus();
        const action = button.dataset.menu;
        if (action === "paths") this.openPathModal();
        else if (action === "settings") this.batchSettings.open();
        else if (action === "open-exports") this.openExportsFolder();
        else if (action === "save-project") this.saveProjectNow();
        else if (action === "home") this.showHome();
        else if (action === "new-project") this.openNewProjectModal();
        else if (action === "clear") this.clearProject();
        else if (action === "about") this.openAbout();
      });
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".menu-anchor")) this._closeMenus();
    });

    // Path modal
    $("#pathStart").addEventListener("click", () => this.importPaths());
    $$('[data-close="path"]').forEach((node) => node.addEventListener("click", () => this.closePathModal()));
    $$('[data-close="about"]').forEach((node) => node.addEventListener("click", () => this.closeAbout()));

    // Project name
    const projectInput = $("#projectName");
    const saveProject = async () => {
      if (state.savingProject) return;
      const name = projectInput.value.trim() || "未命名项目";
      if (name === (state.session?.project?.name || "")) return;
      state.savingProject = true;
      try {
        const response = await api.setProject(name);
        if (state.session) state.session.project = response.project;
        projectInput.value = response.project?.name || name;
        document.title = `${projectInput.value} · 纸边 PaperTrim`;
      } catch (error) {
        toast(error.message || "项目名称保存失败", "error");
      } finally {
        state.savingProject = false;
      }
    };
    projectInput.addEventListener("change", saveProject);
    projectInput.addEventListener("blur", () => {
      if (projectInput.value.trim() !== (state.session?.project?.name || "")) saveProject();
    });
    projectInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") projectInput.blur();
    });

    // Lightbox
    $("#lightboxClose").addEventListener("click", () => this.closeLightbox());
    $("#lightboxPrev").addEventListener("click", () => this.stepLightbox(-1));
    $("#lightboxNext").addEventListener("click", () => this.stepLightbox(1));
    $("#lightbox").addEventListener("click", (event) => {
      if (event.target === $("#lightbox")) this.closeLightbox();
    });
    $("#lightboxImage").addEventListener("error", () => toast("大图加载失败。", "error"));

    // Drag & drop
    let dragDepth = 0;
    const overlay = $("#dropOverlay");
    window.addEventListener("dragenter", (event) => {
      if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
      event.preventDefault();
      dragDepth += 1;
      overlay.hidden = false;
    });
    window.addEventListener("dragover", (event) => {
      if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files")) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }
    });
    window.addEventListener("dragleave", (event) => {
      if (!event.dataTransfer || !Array.from(event.dataTransfer.types || []).includes("Files")) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) overlay.hidden = true;
    });
    window.addEventListener("drop", async (event) => {
      if (!event.dataTransfer) return;
      event.preventDefault();
      dragDepth = 0;
      overlay.hidden = true;
      const files = await this._collectDroppedFiles(event.dataTransfer);
      if (files.length) this.uploadFiles(files);
    });

    // Keyboard
    document.addEventListener("keydown", (event) => this._onKeyDown(event));
    window.addEventListener("hashchange", () => this._handleHash());

    // Modal backdrop for confirm is handled by confirmDialog.
  }

  _onGridClick(event) {
    const card = event.target.closest(".card");
    if (!card) return;
    const id = card.dataset.id;
    const actionButton = event.target.closest("[data-act]");
    if (actionButton) {
      event.preventDefault();
      event.stopPropagation();
      const action = actionButton.dataset.act;
      if (action === "preview") this.openLightbox(id);
      else if (action === "left") this.rotateOne(id, -90);
      else if (action === "right") this.rotateOne(id, 90);
      return;
    }
    if (event.target.closest(".card-check")) return;
    const index = this.images.findIndex((item) => item.id === id);
    if (event.shiftKey) {
      event.preventDefault();
      this._selectRange(index);
      return;
    }
    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      this._toggleSelect(id);
      return;
    }
    if (event.target.closest("[data-open]")) {
      this.openEditor(id);
    }
  }

  _onKeyDown(event) {
    const target = event.target;
    const typing = target && (
      target.tagName === "INPUT" || target.tagName === "TEXTAREA"
      || target.tagName === "SELECT" || target.isContentEditable
    );
    if (typing) return;
    if (event.key === "Escape") {
      this._closeMenus();
      if (!$("#pathModal").hidden) { this.closePathModal(); return; }
      if (!$("#aboutModal").hidden) { this.closeAbout(); return; }
      if (!$("#globalPreviewModal").hidden) { this.batchSettings.closeGlobalPreview(); return; }
      if (!$("#batchModal").hidden) { this.batchSettings.close(); return; }
      if (!$("#newProjectModal").hidden) { this.closeNewProjectModal(); return; }
      if (!$("#homeScreen").hidden) { this.hideHome(); return; }
      if (state.lightbox) { this.closeLightbox(); return; }
      return;
    }
    if (state.lightbox) {
      if (event.key === "ArrowLeft") { event.preventDefault(); this.stepLightbox(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); this.stepLightbox(1); }
      return;
    }
    if (!$("#globalPreviewModal").hidden) {
      if (event.key === "ArrowLeft") { event.preventDefault(); this.batchSettings.stepGlobalPreview(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); this.batchSettings.stepGlobalPreview(1); }
      return;
    }
    if (state.editorOpen) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
      event.preventDefault();
      this._selectAll(true);
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      if (state.selected.size) {
        event.preventDefault();
        this.runBatch("delete");
      }
      return;
    }
    if (event.key.toLowerCase() === "r") {
      if (!state.selected.size) return;
      event.preventDefault();
      this.runBatch(event.shiftKey ? "rotate-left" : "rotate-right");
      return;
    }
    if (event.key.toLowerCase() === "e") {
      event.preventDefault();
      this.openExport();
      return;
    }
    if (event.key.toLowerCase() === "b") {
      event.preventDefault();
      this.openLightbox();
      return;
    }
    if (event.key === "?" || event.key === "/") {
      // Leave browser find/help alone.
    }
  }

  _toggleMenu(id) {
    const target = $(`#${id}`);
    const open = target.hidden;
    this._closeMenus();
    target.hidden = !open;
  }

  _closeMenus() {
    $("#importMenu").hidden = true;
    $("#exportMenu").hidden = true;
    $("#mainMenu").hidden = true;
  }

  openPathModal() {
    $("#pathInput").value = "";
    $("#pathModal").hidden = false;
    this.updateBodyLock();
    setTimeout(() => $("#pathInput").focus(), 30);
  }

  closePathModal() {
    $("#pathModal").hidden = true;
    this.updateBodyLock();
  }

  openAbout() {
    const performance = state.session?.performance || {};
    const bits = [];
    if (performance.threads) bits.push(`${performance.threads} 线程`);
    if (performance.cpu_count) bits.push(`${performance.cpu_count} 核`);
    if (performance.opencl) bits.push("OpenCL 已启用");
    $("#aboutPerformance").textContent = bits.length ? `性能：${bits.join(" · ")}` : "性能：自动";
    $("#aboutModal").hidden = false;
    this.updateBodyLock();
  }

  async showHome() {
    this._closeMenus();
    $("#homeScreen").hidden = false;
    this.updateBodyLock();
    await this.refreshProjects();
  }

  async refreshProjects() {
    try {
      const data = await api.listProjects();
      this._renderProjects(data.projects || []);
      const updated = state.session?.project?.updated_at;
      $("#homeSavedAt").textContent = updated ? `上次保存：${new Date(updated).toLocaleString()}` : "";
    } catch (error) {
      $("#projectGrid").innerHTML = "";
      toast(error.message || "项目列表加载失败", "error");
    }
  }

  _renderProjects(projects) {
    const grid = $("#projectGrid");
    grid.innerHTML = "";
    for (const project of projects) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = `project-card${project.current ? " current" : ""}`;
      card.dataset.projectId = project.id;
      const cover = document.createElement("div");
      cover.className = "project-cover";
      if (project.cover_url) {
        const image = document.createElement("img");
        image.src = `${project.cover_url}?t=${encodeURIComponent(project.updated_at || "")}`;
        image.alt = project.name || "";
        image.loading = "lazy";
        cover.appendChild(image);
      } else {
        const empty = document.createElement("span");
        empty.className = "project-empty";
        empty.textContent = "还没有预览";
        cover.appendChild(empty);
      }
      const name = document.createElement("div");
      name.className = "project-card-name";
      if (project.current) {
        const dot = document.createElement("span");
        dot.className = "current-dot";
        name.appendChild(dot);
      }
      name.appendChild(document.createTextNode(project.name || "未命名项目"));
      const meta = document.createElement("div");
      meta.className = "project-card-meta";
      const when = project.updated_at ? new Date(project.updated_at).toLocaleString() : "";
      meta.textContent = `${project.total || 0} 张 · ${project.ready || 0} 已识别${when ? ` · ${when}` : ""}`;
      card.appendChild(cover);
      card.appendChild(name);
      card.appendChild(meta);
      grid.appendChild(card);
    }
  }

  async openProject(projectId) {
    if (!projectId) return;
    try {
      const response = await api.openProject(projectId);
      if (response.session) this.setSession(response.session, { silent: true });
      state.selected.clear();
      this.hideHome();
      toast("已打开项目", "success");
    } catch (error) {
      toast(error.message || "打开项目失败", "error");
    }
  }

  openNewProjectModal() {
    $("#newProjectModal").hidden = false;
    $("#newProjectName").value = "未命名项目";
    this.updateBodyLock();
    setTimeout(() => {
      $("#newProjectName").focus();
      $("#newProjectName").select();
    }, 30);
  }

  closeNewProjectModal() {
    $("#newProjectModal").hidden = true;
    this.updateBodyLock();
  }

  async createProject() {
    const name = $("#newProjectName").value.trim() || "未命名项目";
    const button = $("#newProjectCreate");
    button.disabled = true;
    try {
      const response = await api.newProject(name);
      if (response.session) this.setSession(response.session, { silent: true });
      state.selected.clear();
      this.closeNewProjectModal();
      this.hideHome();
      toast("已新建项目", "success");
    } catch (error) {
      toast(error.message || "新建项目失败", "error");
    } finally {
      button.disabled = false;
    }
  }

  hideHome() {
    $("#homeScreen").hidden = true;
    this.updateBodyLock();
  }

  async saveProjectNow() {
    try {
      await api.saveProject();
      if (state.session) await this.loadSession({ silent: true });
      if (!$("#homeScreen").hidden) await this.showHome();
      toast("项目已保存", "success");
    } catch (error) {
      toast(error.message || "项目保存失败", "error");
    }
  }

  closeAbout() {
    $("#aboutModal").hidden = true;
    this.updateBodyLock();
  }

  async openExportsFolder() {
    const dir = state.session?.defaults?.export_dir;
    if (!dir) return;
    try { await api.openPath(dir); }
    catch (error) { toast(error.message || "无法打开导出目录", "error"); }
  }

  async _collectDroppedFiles(dataTransfer) {
    const items = Array.from(dataTransfer.items || []);
    const canWalk = items.length && items.some((item) => typeof item.webkitGetAsEntry === "function");
    if (!canWalk) return Array.from(dataTransfer.files || []);
    // Entry handles must be captured synchronously: after the first await the
    // DataTransfer is no longer readable in Chromium, which used to leave only
    // the first dropped file visible.
    const entries = [];
    for (const item of items) {
      try {
        const entry = item.webkitGetAsEntry();
        if (entry) entries.push(entry);
      } catch { /* ignore unreadable item */ }
    }
    if (!entries.length) return Array.from(dataTransfer.files || []);
    const output = [];
    const walkEntry = async (entry) => {
      if (output.length >= 1000) return;
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        output.push(file);
        return;
      }
      if (entry.isDirectory) {
        const reader = entry.createReader();
        while (true) {
          const children = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!children.length) break;
          for (const child of children) await walkEntry(child);
        }
      }
    };
    for (const entry of entries) await walkEntry(entry);
    return output;
  }

  async _handleHash() {
    const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (!hash) return;
    history.replaceState(null, "", location.pathname);
    if (hash.startsWith("edit=")) {
      const id = hash.slice(5);
      if (this.imageById(id)) this.openEditor(id);
    } else if (hash.startsWith("browse=")) {
      const id = hash.slice(7);
      if (this.imageById(id)) this.openLightbox(id);
    }
  }
}

const app = new App();
window.papertrim = app;
app.init();
