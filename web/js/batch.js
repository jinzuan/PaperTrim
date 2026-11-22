import { api } from "./api.js";
import { $, $$, clamp, toast } from "./ui.js";

function normalizeOutput(raw = {}) {
  const output = {
    mode: raw.mode === "rect" ? "rect" : "cutout",
    rectification: ["auto", "rotate", "quad"].includes(raw.rectification) ? raw.rectification : "auto",
    fill: ["paper", "color", "white", "transparent", "original"].includes(raw.fill) ? raw.fill : "paper",
    color: /^#[0-9a-f]{6}$/i.test(String(raw.color || "")) ? String(raw.color).toLowerCase() : "#ffffff",
    padding: Math.max(0, Math.min(1000, Number(raw.padding) || 0)),
    aspect: raw.aspect === "custom" ? "custom" : "paper",
    aspect_w: Math.max(1, Number(raw.aspect_w) || 210),
    aspect_h: Math.max(1, Number(raw.aspect_h) || 297),
    edge: raw.edge === "actual" ? "actual" : "fit",
    edge_margin: Math.max(-60, Math.min(60, Number(raw.edge_margin) || 0)),
    enhance: Boolean(raw.enhance),
    whiten: Math.max(0, Math.min(100, Number(raw.whiten ?? 35))),
    purity: Math.max(0, Math.min(100, Number(raw.purity ?? 35))),
    desaturate: Math.max(0, Math.min(100, Number(raw.desaturate ?? 0))),
    mono_boost: Boolean(raw.mono_boost),
  };
  return output;
}

export class BatchSettings {
  constructor(app) {
    this.app = app;
    this.previewItems = [];
    this.previewObserver = null;
    this.previewQueue = [];
    this.previewActive = 0;
    this.previewTimer = 0;
    this.previewVersion = 0;
    this.previewSize = 680;
    this.globalIndex = -1;
    this.globalUrl = null;
    this.globalToken = 0;
    this._bind();
  }

  get modal() { return $("#batchModal"); }

  _bind() {
    $$("#batchModal [data-close='batch']").forEach((node) => {
      node.addEventListener("click", () => this.close());
    });
    $$('input[name="batchMode"]').forEach((input) => {
      input.addEventListener("change", () => this.updateUi());
    });
    $$("#batchModal select, #batchModal input").forEach((input) => {
      input.addEventListener("change", () => this.updateUi());
    });
    $("#batchApply").addEventListener("click", () => this.apply());
    $("#batchEnhance").addEventListener("change", () => this._syncEnhanceUi());
    $("#batchDesaturate").addEventListener("input", () => {
      $("#batchDesaturateLabel").textContent = `${$("#batchDesaturate").value}%`;
      this._syncEnhanceUi();
    });
    $("#batchMonoBoost").addEventListener("change", () => this._syncPresetUi());
    $$("#batchModal [data-edge-preset]").forEach((button) => {
      button.addEventListener("click", () => this.applyEdgePreset(button.dataset.edgePreset));
    });
    $$("#batchModal [data-enhance-preset]").forEach((button) => {
      button.addEventListener("click", () => this.applyEnhancePreset(button.dataset.enhancePreset));
    });
    $("#batchModal").addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.close();
    });
    $("#batchModal").addEventListener("input", (event) => {
      if (event.target.id !== "previewSize") this._schedulePreview();
    });
    $("#batchModal").addEventListener("change", (event) => {
      if (event.target.id !== "previewSize") this._schedulePreview();
    });
    $("#previewSize").addEventListener("input", () => this._applyPreviewSize());
    $("#globalPreviewClose").addEventListener("click", () => this.closeGlobalPreview());
    $$('[data-close="global-preview"]').forEach((node) => {
      node.addEventListener("click", () => this.closeGlobalPreview());
    });
    $("#globalPreviewPrev").addEventListener("click", () => this.stepGlobalPreview(-1));
    $("#globalPreviewNext").addEventListener("click", () => this.stepGlobalPreview(1));
    $$('input[name="batchScope"]').forEach((input) => {
      input.addEventListener("change", () => this._renderPreviewList());
    });
  }

  async open() {
    const ready = this.app.readyRecords();
    if (!ready.length) {
      toast("还没有可设置的图片，请先完成识别。", "info");
      return;
    }
    const selected = this.app.selectedReadyRecords();
    $("#batchAllCount").textContent = String(ready.length);
    $("#batchSelCount").textContent = String(selected.length);
    const selectedRadio = $('input[name="batchScope"][value="selected"]');
    selectedRadio.disabled = selected.length === 0;
    const scope = selected.length ? "selected" : "all";
    $$('input[name="batchScope"]').forEach((input) => { input.checked = input.value === scope; });

    const defaults = normalizeOutput(this.app.state.session?.defaults?.output || {});
    this.setOutput(defaults);
    try {
      const saved = Number(localStorage.getItem("papertrim.previewSize"));
      if (Number.isFinite(saved) && saved >= 280 && saved <= 820) this.previewSize = saved;
    } catch { /* storage unavailable */ }
    this._applyPreviewSize();
    $("#batchAlign").value = "";
    $("#batchEdge").value = "";
    $("#batchEnhance").checked = Boolean(defaults.enhance);
    $("#batchWhiten").value = String(defaults.whiten);
    $("#batchPurity").value = String(defaults.purity);
    $("#batchDesaturate").value = String(defaults.desaturate);
    $("#batchDesaturateLabel").textContent = `${defaults.desaturate}%`;
    $("#batchMonoBoost").checked = Boolean(defaults.mono_boost);
    this._syncEnhanceUi();
    $("#batchRemember").checked = true;
    this.modal.hidden = false;
    this.app.updateBodyLock();
    this.updateUi();
    this._renderPreviewList();
  }

  _scopeRecords() {
    const scope = $('input[name="batchScope"]:checked')?.value || "all";
    const selected = this.app.selectedReadyRecords();
    if (scope === "selected" && selected.length) return selected;
    return this.app.readyRecords();
  }

  _applyPreviewSize() {
    const input = $("#previewSize");
    const value = clamp(Number(input.value) || 680, 280, 820);
    this.previewSize = value;
    input.value = String(value);
    $("#previewSizeLabel").textContent = `${value}px`;
    document.querySelector(".batch-dialog").style.setProperty("--batch-preview-size", `${value}px`);
    try { localStorage.setItem("papertrim.previewSize", String(value)); } catch { /* storage unavailable */ }
  }

  _renderPreviewList() {
    this._teardownPreview();
    const records = this._scopeRecords();
    $("#batchPreviewName").textContent = `${records.length} 张`;
    const list = $("#batchPreviewList");
    list.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "preview-empty";
      empty.textContent = "当前范围还没有已识别的图片。";
      list.appendChild(empty);
      return;
    }
    this.previewItems = [];
    records.forEach((record, index) => {
      const item = {
        record, dirty: true, visible: false, queued: false, rendering: false, url: null,
        el: null, img: null,
      };
      const el = document.createElement("div");
      el.className = "preview-item";
      const head = document.createElement("div");
      head.className = "preview-item-head";
      const name = document.createElement("span");
      name.textContent = record.name || `图片 ${index + 1}`;
      const order = document.createElement("em");
      order.textContent = String(index + 1).padStart(2, "0");
      head.appendChild(name);
      head.appendChild(order);
      const media = document.createElement("div");
      media.className = "preview-item-media";
      const image = document.createElement("img");
      image.src = record.thumb_url || record.source_url || "";
      image.alt = record.name || "";
      image.loading = "lazy";
      media.appendChild(image);
      media.addEventListener("click", () => this.openGlobalPreview(item));
      el.appendChild(head);
      el.appendChild(media);
      list.appendChild(el);
      item.el = el;
      item.img = image;
      this.previewItems.push(item);
    });
    this.previewObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const item = this.previewItems.find((candidate) => candidate.el === entry.target);
        if (!item) continue;
        item.visible = entry.isIntersecting;
        if (item.visible && item.dirty) this._enqueuePreview(item);
      }
    }, { root: list, rootMargin: "0px" });
    this.previewItems.forEach((item) => this.previewObserver.observe(item.el));
    this.previewVersion += 1;
    this._enqueueVisiblePreviews();
  }

  _teardownPreview() {
    clearTimeout(this.previewTimer);
    this.previewVersion += 1;
    if (this.previewObserver) {
      this.previewObserver.disconnect();
      this.previewObserver = null;
    }
    this.previewQueue = [];
    for (const item of this.previewItems) {
      if (item.url) URL.revokeObjectURL(item.url);
    }
    this.previewItems = [];
    const list = $("#batchPreviewList");
    if (list) list.innerHTML = "";
  }

  _schedulePreview(delay = 220) {
    for (const item of this.previewItems) {
      item.dirty = true;
      item.error = false;
    }
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => {
      this.previewVersion += 1;
      for (const item of this.previewItems) {
        item.dirty = true;
        item.error = false;
      }
      this._enqueueVisiblePreviews();
    }, delay);
  }

  _enqueueVisiblePreviews() {
    for (const item of this.previewItems) {
      if (item.visible && item.dirty) this._enqueuePreview(item);
    }
  }

  _enqueuePreview(item) {
    if (item.rendering || item.queued || !item.dirty || !item.visible) return;
    item.queued = true;
    this.previewQueue.push(item);
    this._pumpPreviewQueue();
  }

  _pumpPreviewQueue() {
    if (this.previewActive >= 2) return;
    const item = this.previewQueue.shift();
    if (!item) return;
    item.queued = false;
    if (!this.previewItems.includes(item) || !item.visible || !item.dirty) {
      this._pumpPreviewQueue();
      return;
    }
    item.rendering = true;
    this.previewActive += 1;
    this._renderPreviewItem(item).finally(() => {
      this.previewActive = Math.max(0, this.previewActive - 1);
      this._pumpPreviewQueue();
    });
  }

  async _renderPreviewItem(item) {
    const version = this.previewVersion;
    item.el.classList.add("loading");
    try {
      const blob = await api.renderPreview(item.record.id, this.getOutput());
      if (!this.previewItems.includes(item) || version !== this.previewVersion) {
        item.dirty = true;
        return;
      }
      const previous = item.url;
      item.url = URL.createObjectURL(blob);
      item.img.src = item.url;
      if (previous && previous !== item.url) URL.revokeObjectURL(previous);
      item.dirty = false;
      item.error = false;
      item.el.classList.remove("error");
    } catch (error) {
      if (this.previewItems.includes(item) && version === this.previewVersion) {
        item.el.classList.add("error");
        item.error = true;
      }
      item.dirty = true;
    } finally {
      item.el.classList.remove("loading");
      item.rendering = false;
      if (item.dirty && item.visible && !item.error && this.previewItems.includes(item)) {
        this._enqueuePreview(item);
      }
    }
  }

  openGlobalPreview(item) {
    const index = this.previewItems.indexOf(item);
    if (index < 0) return;
    this.globalIndex = index;
    $("#globalPreviewModal").hidden = false;
    this.app.updateBodyLock();
    this._loadGlobalPreview();
  }

  closeGlobalPreview() {
    this.globalToken += 1;
    if (this.globalUrl) {
      URL.revokeObjectURL(this.globalUrl);
      this.globalUrl = null;
    }
    $("#globalPreviewImage").removeAttribute("src");
    $("#globalPreviewModal").hidden = true;
    this.app.updateBodyLock();
  }

  stepGlobalPreview(delta) {
    if (this.globalIndex < 0 || this.previewItems.length === 0) return;
    this.globalIndex = (this.globalIndex + delta + this.previewItems.length) % this.previewItems.length;
    this._loadGlobalPreview();
  }

  async _loadGlobalPreview() {
    const item = this.previewItems[this.globalIndex];
    if (!item) return;
    const token = ++this.globalToken;
    $("#globalPreviewName").textContent = item.record.name || "预览";
    $("#globalPreviewMeta").textContent = `${this.globalIndex + 1} / ${this.previewItems.length}`;
    $("#globalPreviewLoading").textContent = "正在生成大图…";
    $("#globalPreviewLoading").hidden = false;
    $("#globalPreviewPrev").hidden = this.previewItems.length < 2;
    $("#globalPreviewNext").hidden = this.previewItems.length < 2;
    try {
      const blob = await api.renderPreview(item.record.id, this.getOutput(), 1800);
      if (token !== this.globalToken) return;
      const previous = this.globalUrl;
      this.globalUrl = URL.createObjectURL(blob);
      $("#globalPreviewImage").src = this.globalUrl;
      if (previous) URL.revokeObjectURL(previous);
      $("#globalPreviewLoading").hidden = true;
    } catch (error) {
      if (token !== this.globalToken) return;
      $("#globalPreviewLoading").textContent = error.message || "预览生成失败";
    }
  }

  close() {
    this.closeGlobalPreview();
    this._teardownPreview();
    this.modal.hidden = true;
    this.app.updateBodyLock();
  }

  setOutput(output) {
    const modeInput = $(`input[name="batchMode"][value="${output.mode}"]`);
    if (modeInput) modeInput.checked = true;
    $("#batchRectification").value = output.rectification;
    $("#batchFill").value = output.fill;
    $("#batchColor").value = output.color;
    $("#batchPadding").value = String(output.padding);
    $("#batchAspect").value = output.aspect;
    $("#batchAspectW").value = String(Math.round(output.aspect_w));
    $("#batchAspectH").value = String(Math.round(output.aspect_h));
    $("#batchEdgeMode").value = output.edge;
    $("#batchEdgeMargin").value = String(output.edge_margin);
    $("#batchEnhance").checked = Boolean(output.enhance);
    $("#batchWhiten").value = String(output.whiten);
    $("#batchPurity").value = String(output.purity);
    $("#batchDesaturate").value = String(output.desaturate);
    $("#batchDesaturateLabel").textContent = `${output.desaturate}%`;
    $("#batchMonoBoost").checked = Boolean(output.mono_boost);
    this._syncEnhanceUi();
    this.updateUi();
  }

  _syncEnhanceUi() {
    const enabled = $("#batchEnhance").checked;
    $("#batchWhiten").disabled = !enabled;
    $("#batchPurity").disabled = !enabled;
    const mono = Number($("#batchDesaturate").value) > 0;
    $("#batchMonoBoost").disabled = !mono;
    if (!mono) $("#batchMonoBoost").checked = false;
    this._syncPresetUi();
  }

  _syncPresetUi() {
    const mode = $('input[name="batchMode"]:checked')?.value || "cutout";
    const edge = $("#batchEdgeMode").value;
    const margin = Number($("#batchEdgeMargin").value) || 0;
    const rectification = $("#batchRectification").value;
    let edgePreset = null;
    if (mode === "rect") {
      if (rectification === "quad") edgePreset = "quad";
      else if (edge === "fit" && margin < -8) edgePreset = "tight";
      else if (edge === "fit") edgePreset = "fit";
      else edgePreset = "actual";
    }
    $$("#batchModal [data-edge-preset]").forEach((button) => {
      button.classList.toggle("active", button.dataset.edgePreset === edgePreset);
    });
    const enhance = $("#batchEnhance").checked;
    const whiten = Number($("#batchWhiten").value) || 0;
    const purity = Number($("#batchPurity").value) || 0;
    const desaturate = Number($("#batchDesaturate").value) || 0;
    let enhancePreset = "off";
    if (desaturate >= 99) enhancePreset = "mono";
    else if (enhance) enhancePreset = (whiten >= 55 || purity >= 55) ? "strong" : "soft";
    $$("#batchModal [data-enhance-preset]").forEach((button) => {
      button.classList.toggle("active", button.dataset.enhancePreset === enhancePreset);
    });
  }

  applyEdgePreset(preset) {
    $('input[name="batchMode"][value="rect"]').checked = true;
    if (preset === "quad") {
      $("#batchRectification").value = "quad";
      $("#batchEdgeMode").value = "fit";
      $("#batchEdgeMargin").value = "0";
    } else if (preset === "actual") {
      $("#batchRectification").value = "auto";
      $("#batchEdgeMode").value = "actual";
      $("#batchEdgeMargin").value = "0";
    } else if (preset === "tight") {
      $("#batchRectification").value = "auto";
      $("#batchEdgeMode").value = "fit";
      $("#batchEdgeMargin").value = "-18";
    } else {
      $("#batchRectification").value = "auto";
      $("#batchEdgeMode").value = "fit";
      $("#batchEdgeMargin").value = "0";
    }
    this.updateUi();
    this._schedulePreview();
  }

  applyEnhancePreset(preset) {
    if (preset === "mono") {
      $("#batchDesaturate").value = "100";
      $("#batchDesaturateLabel").textContent = "100%";
      $("#batchEnhance").checked = false;
      $("#batchMonoBoost").checked = false;
    } else if (preset === "strong") {
      $("#batchDesaturate").value = "0";
      $("#batchDesaturateLabel").textContent = "0%";
      $("#batchEnhance").checked = true;
      $("#batchWhiten").value = "72";
      $("#batchPurity").value = "65";
      $("#batchMonoBoost").checked = false;
    } else if (preset === "soft") {
      $("#batchDesaturate").value = "0";
      $("#batchDesaturateLabel").textContent = "0%";
      $("#batchEnhance").checked = true;
      $("#batchWhiten").value = "35";
      $("#batchPurity").value = "35";
      $("#batchMonoBoost").checked = false;
    } else {
      $("#batchDesaturate").value = "0";
      $("#batchDesaturateLabel").textContent = "0%";
      $("#batchEnhance").checked = false;
      $("#batchMonoBoost").checked = false;
    }
    this._syncEnhanceUi();
    this.updateUi();
    this._schedulePreview();
  }

  getOutput() {
    return normalizeOutput({
      mode: $('input[name="batchMode"]:checked')?.value || "cutout",
      rectification: $("#batchRectification").value,
      fill: $("#batchFill").value,
      color: $("#batchColor").value,
      padding: $("#batchPadding").value,
      aspect: $("#batchAspect").value,
      aspect_w: $("#batchAspectW").value,
      aspect_h: $("#batchAspectH").value,
      edge: $("#batchEdgeMode").value,
      edge_margin: $("#batchEdgeMargin").value,
      enhance: $("#batchEnhance").checked,
      whiten: $("#batchWhiten").value,
      purity: $("#batchPurity").value,
      desaturate: $("#batchDesaturate").value,
      mono_boost: $("#batchMonoBoost").checked,
    });
  }

  updateUi() {
    const mode = $('input[name="batchMode"]:checked')?.value || "cutout";
    const fill = $("#batchFill").value;
    const aspect = $("#batchAspect").value;
    const section = $("#batchRectSection");
    section.classList.toggle("disabled", mode !== "rect");
    $("#batchColorField").hidden = fill !== "color";
    $("#batchAspectField").hidden = aspect !== "custom";
    this._syncPresetUi();
  }

  async apply() {
    const scope = $('input[name="batchScope"]:checked')?.value || "all";
    const records = scope === "selected" ? this.app.selectedReadyRecords() : this.app.readyRecords();
    if (!records.length) {
      toast(scope === "selected" ? "还没有选中已识别的图片。" : "还没有可设置的图片。", "info");
      return;
    }
    const output = this.getOutput();
    const edgeText = $("#batchEdge").value.trim();
    const edge = edgeText === "" ? null : clamp(Number(edgeText) || 0, -30, 30);
    const align = $("#batchAlign").value || null;
    const remember = $("#batchRemember").checked;
    $("#batchApply").disabled = true;
    try {
      const payload = { output, align, remember };
      if (edge !== null) payload.edge = edge;
      const response = await api.batch(records.map((record) => record.id), "output", 0, null, payload);
      if (response.session) this.app.setSession(response.session, { silent: true });
      this.close();
      await this.app.trackJob(response.job, { title: "应用输出设置" });
    } catch (error) {
      toast(error.message || "应用输出设置失败", "error");
    } finally {
      $("#batchApply").disabled = false;
    }
  }
}
