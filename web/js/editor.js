import { api } from "./api.js";
import {
  $, $$, clamp, formatBytes, icon, loadImage, showWarnings, toast,
} from "./ui.js";

const clone = (value) => JSON.parse(JSON.stringify(value));

export class Editor {
  constructor(app) {
    this.app = app;
    this.detail = null;
    this.image = null;
    this.sourceW = 1;
    this.sourceH = 1;
    this.resultMode = false;
    this.resultImage = null;
    this.resultUrl = null;
    this.resultToken = 0;
    this.resultTimer = 0;
    this.resultPending = false;
    this.polygon = [];
    this.strokes = [];
    this.quad = [];
    this.paperRect = [];
    this.quadDirty = false;
    this.outputDirty = false;
    this.curveDirty = false;
    this.outputTimer = 0;
    this.curveTimer = 0;
    this.outputMode = "cutout";
    this.mode = "vertex";
    this.scale = 0.3;
    this.fitScale = 0.3;
    this.minScale = 0.05;
    this.maxScale = 1;
    this.selected = -1;
    this.selectedCorner = -1;
    this.history = [];
    this.drag = null;
    this.brushStroke = null;
    this.pointer = null;
    this.regions = [];
    this.regionDrag = null;
    this.outline = [];
    this.regionBusy = false;
    this.resultLayout = [];
    this.regionPalette = ["#e05a3d", "#3d8fe0", "#2fa36b", "#a35ae0", "#e0a53d", "#3dbfc8", "#d1557f"];
    this.saving = false;
    this.pendingSave = false;
    this.dirty = false;
    this.editVersion = 0;
    this.saveTimer = 0;
    this.loadToken = 0;
    this.userZoomed = false;
    this.busy = false;
    this.openFlag = false;
    this._bind();
  }

  get els() {
    return {
      modal: $("#editorModal"),
      name: $("#editorName"),
      sub: $("#editorSub"),
      canvas: $("#editorCanvas"),
      wrap: $("#canvasWrap"),
      busy: $("#canvasBusy"),
      hint: $("#canvasHint"),
      brush: $("#brushControls"),
      brushSize: $("#brushSize"),
      brushLabel: $("#brushSizeLabel"),
      zoomLabel: $("#zoomLabel"),
      preview: $("#resultPreview"),
      previewEmpty: $("#previewEmpty"),
      resultMeta: $("#resultMeta"),
      warnings: $("#editorWarnings"),
      undo: $("#undoBtn"),
      fine: $("#fineAngle"),
      fineLabel: $("#fineAngleLabel"),
      alignEdge: $("#alignEdge"),
      expand: $("#expandRange"),
      expandLabel: $("#expandLabel"),
      feather: $("#featherRange"),
      featherLabel: $("#featherLabel"),
      simplify: $("#simplifyRange"),
      simplifyLabel: $("#simplifyLabel"),
      thresholdAuto: $("#thresholdAuto"),
      threshold: $("#thresholdRange"),
      thresholdLabel: $("#thresholdLabel"),
      invert: $("#invertCheck"),
      detectHoles: $("#detectHoles"),
      outputCutout: $('[data-output-mode="cutout"]'),
      outputRect: $('[data-output-mode="rect"]'),
      outputRectFields: $("#outputRectFields"),
      outputRectification: $("#outputRectification"),
      outputFill: $("#outputFill"),
      outputColorRow: $("#outputColorRow"),
      outputColor: $("#outputColor"),
      outputColorLabel: $("#outputColorLabel"),
      outputPadding: $("#outputPadding"),
      outputAspect: $("#outputAspect"),
      outputAspectRow: $("#outputAspectRow"),
      outputAspectW: $("#outputAspectW"),
      outputAspectH: $("#outputAspectH"),
      outputEdge: $("#outputEdge"),
      outputEdgeMargin: $("#outputEdgeMargin"),
      outputEdgeMarginLabel: $("#outputEdgeMarginLabel"),
      outputEnhance: $("#outputEnhance"),
      outputWhiten: $("#outputWhiten"),
      outputWhitenLabel: $("#outputWhitenLabel"),
      outputPurity: $("#outputPurity"),
      outputPurityLabel: $("#outputPurityLabel"),
      outputDesaturate: $("#outputDesaturate"),
      outputDesaturateLabel: $("#outputDesaturateLabel"),
      outputMonoBoost: $("#outputMonoBoost"),
      outputQuadStatus: $("#outputQuadStatus"),
      outputQuadAuto: $("#outputQuadAuto"),
      outputQuadEdit: $("#outputQuadEdit"),
      pointCount: $("#pointCount"),
      pointDelta: $("#pointDelta"),
      pointsAdd: $("#pointsAdd"),
      pointsRemove: $("#pointsRemove"),
      curveEnabled: $("#curveEnabled"),
      curveAmount: $("#curveAmount"),
      curveAmountLabel: $("#curveAmountLabel"),
      regionList: $("#regionList"),
      regionAuto: $("#regionAuto"),
      regionRedetect: $("#regionRedetect"),
      regionOutline: $("#regionOutline"),
    };
  }

  _bind() {
    const e = this.els;
    $("#editorClose").addEventListener("click", () => this.close());
    $("#editorDone").addEventListener("click", () => this.close());
    $('[data-close="editor"]').addEventListener("click", () => this.close());
    $("#editorRotateLeft").addEventListener("click", () => this.rotate(-90));
    $("#editorRotateRight").addEventListener("click", () => this.rotate(90));
    $("#sideRotateLeft").addEventListener("click", () => this.rotate(-90));
    $("#sideRotateRight").addEventListener("click", () => this.rotate(90));
    $("#sideDeskew").addEventListener("click", () => this.deskew());
    $("#sideResetAngle").addEventListener("click", () => this.patch({ fine_angle: 0 }));
    $("#editorRecompute").addEventListener("click", () => this.recompute());
    $("#applyParams").addEventListener("click", () => this.recompute());
    $("#previewRefresh").addEventListener("click", () => this._updatePreview(true));

    $$(".modes button").forEach((button) => {
      button.addEventListener("click", () => this.setMode(button.dataset.mode));
    });
    $("#zoomIn").addEventListener("click", () => this.zoomBy(1.25));
    $("#zoomOut").addEventListener("click", () => this.zoomBy(1 / 1.25));
    $("#zoomFit").addEventListener("click", () => this.fit());
    $$("#editorViewToggle button").forEach((button) => {
      button.addEventListener("click", () => this.setView(button.dataset.view));
    });
    e.undo.addEventListener("click", () => this.undo());

    e.canvas.addEventListener("pointerdown", (event) => this.onPointerDown(event));
    e.canvas.addEventListener("pointermove", (event) => this.onPointerMove(event));
    e.canvas.addEventListener("pointerup", (event) => this.onPointerUp(event));
    e.canvas.addEventListener("pointercancel", (event) => this.onPointerUp(event));
    e.canvas.addEventListener("pointerleave", () => {
      this.pointer = null;
      if (this.mode !== "pan") this.draw();
    });
    e.canvas.addEventListener("contextmenu", (event) => this.onContextMenu(event));
    e.canvas.addEventListener("dblclick", (event) => {
      if (this.mode !== "outline") return;
      event.preventDefault();
      if (this.outline.length >= 3) this.commitOutline();
    });
    e.canvas.addEventListener("wheel", (event) => this.onWheel(event), { passive: false });

    e.brushSize.addEventListener("input", () => {
      e.brushLabel.textContent = `${e.brushSize.value} px`;
      this.draw();
    });
    e.fine.addEventListener("input", () => {
      e.fineLabel.textContent = `${Number(e.fine.value).toFixed(1)}°`;
    });
    e.fine.addEventListener("change", () => this.patch({ fine_angle: Number(e.fine.value) }));
    e.alignEdge.addEventListener("change", () => this.changeAlignEdge(e.alignEdge.value));
    e.expand.addEventListener("input", () => this._updateEdgeLabels());
    e.expand.addEventListener("change", () => this._saveParams());
    e.feather.addEventListener("input", () => this._updateEdgeLabels());
    e.feather.addEventListener("change", () => this._saveParams());
    e.simplify.addEventListener("input", () => {
      e.simplifyLabel.textContent = `${Number(e.simplify.value).toFixed(1)} px`;
    });
    e.simplify.addEventListener("change", () => {
      e.simplifyLabel.textContent = `${Number(e.simplify.value).toFixed(1)} px`;
    });
    $$('.output-modes button').forEach((button) => {
      button.addEventListener("click", () => this.setOutputMode(button.dataset.outputMode));
    });
    [e.outputRectification, e.outputFill, e.outputAspect].forEach((control) => {
      control.addEventListener("change", () => {
        this._updateOutputUi();
        this._saveOutputSoon();
      });
    });
    e.outputColor.addEventListener("input", () => {
      e.outputColorLabel.textContent = e.outputColor.value;
      this._saveOutputSoon();
    });
    e.outputPadding.addEventListener("change", () => this._saveOutputSoon());
    e.outputAspectW.addEventListener("change", () => this._saveOutputSoon());
    e.outputAspectH.addEventListener("change", () => this._saveOutputSoon());
    e.outputEdge.addEventListener("change", () => this._saveOutputSoon());
    e.outputEdgeMargin.addEventListener("input", () => {
      e.outputEdgeMarginLabel.textContent = `${e.outputEdgeMargin.value} px`;
      this._saveOutputSoon();
    });
    e.outputEnhance.addEventListener("change", () => {
      this._syncEnhanceUi();
      this._saveOutputSoon();
    });
    e.outputWhiten.addEventListener("input", () => {
      e.outputWhitenLabel.textContent = e.outputWhiten.value;
      this._saveOutputSoon();
    });
    e.outputPurity.addEventListener("input", () => {
      e.outputPurityLabel.textContent = e.outputPurity.value;
      this._saveOutputSoon();
    });
    e.outputDesaturate.addEventListener("input", () => {
      e.outputDesaturateLabel.textContent = `${e.outputDesaturate.value}%`;
      this._syncEnhanceUi();
      this._saveOutputSoon();
    });
    e.outputMonoBoost.addEventListener("change", () => this._saveOutputSoon());
    e.outputQuadAuto.addEventListener("click", () => this.autoQuad());
    e.outputQuadEdit.addEventListener("click", () => this.setMode("quad"));
    e.pointsAdd.addEventListener("click", () => this.adjustPointCount(Math.abs(Number(e.pointDelta.value) || 50)));
    e.pointsRemove.addEventListener("click", () => this.adjustPointCount(-Math.abs(Number(e.pointDelta.value) || 50)));
    e.curveEnabled.addEventListener("change", () => {
      e.curveAmount.disabled = !e.curveEnabled.checked;
      this._saveCurveSoon();
      this.draw();
    });
    e.curveAmount.addEventListener("input", () => {
      e.curveAmountLabel.textContent = Number(e.curveAmount.value).toFixed(2);
      this.draw();
    });
    e.curveAmount.addEventListener("change", () => this._saveCurveSoon());
    e.regionRedetect.addEventListener("click", () => this.redetectRegions());
    e.regionOutline.addEventListener("click", () => this.setMode("outline"));
    e.regionAuto.addEventListener("change", () => this.toggleRegionAuto());
    e.regionList.addEventListener("click", (event) => this._onRegionListClick(event));
    $$(".edge-quick button").forEach((button) => {
      button.addEventListener("click", () => {
        const value = Number(button.dataset.edge) || 0;
        e.expand.value = String(value);
        this._updateEdgeLabels();
        this._saveParams();
      });
    });

    e.thresholdAuto.addEventListener("change", () => {
      e.threshold.disabled = e.thresholdAuto.checked;
      if (e.thresholdAuto.checked) {
        e.thresholdLabel.textContent = "自动";
      } else {
        e.thresholdLabel.textContent = e.threshold.value;
      }
    });
    e.threshold.addEventListener("input", () => {
      if (!e.thresholdAuto.checked) e.thresholdLabel.textContent = e.threshold.value;
    });
    e.detectHoles.addEventListener("change", () => {
      toast("内部扣洞在点“应用参数并重新识别”后生效。", "info", 3000);
    });

    $("#resultPreview").addEventListener("error", () => {
      this.els.previewEmpty.hidden = false;
      this.els.previewEmpty.textContent = "预览加载失败";
    });

    window.addEventListener("resize", () => {
      if (!this.openFlag) return;
      if (!this.userZoomed) this.fit();
      else this._applyScale();
    });

    document.addEventListener("keydown", (event) => this.onKeyDown(event));
  }

  async open(id) {
    if (this.openFlag) await this.flush();
    const detail = await api.getImage(id);
    this.detail = detail;
    this.polygon = clone(detail.polygon || []);
    this.strokes = clone(detail.strokes || []);
    this.quad = clone(detail.quad || []);
    this.paperRect = clone(detail.paper_rect || []);
    this.quadDirty = false;
    this.outputDirty = false;
    this.curveDirty = false;
    this.history = [];
    this.selected = -1;
    this.mode = "vertex";
    this.userZoomed = false;
    this.dirty = false;
    this.busy = false;
    this.editVersion++;
    this.image = null;
    this.resultMode = false;
    this.resultImage = null;
    this.resultToken += 1;
    clearTimeout(this.resultTimer);
    if (this.resultUrl) {
      URL.revokeObjectURL(this.resultUrl);
      this.resultUrl = null;
    }
    this._syncView();
    this.openFlag = true;
    this._syncModeButtons();
    this._syncWrapClass();

    this.els.name.textContent = detail.name || "";
    this.els.sub.textContent = `${detail.width} × ${detail.height} px · ${formatBytes(detail.size_bytes)}`;
    this.els.modal.hidden = false;
    this.app.updateBodyLock();
    this._syncControls();
    this._syncOutputUi();
    this.regions = clone(detail.regions || []);
    this._syncRegionPanel();
    this._renderWarnings();
    this._updatePreview();
    this.els.previewEmpty.textContent = "尚无预览";
    this.setBusy(true, "正在载入原图…");

    const token = ++this.loadToken;
    try {
      const image = await loadImage(detail.source_preview_url || detail.source_url);
      if (token !== this.loadToken || !this.openFlag) return;
      this.image = image;
      this.sourceW = Math.max(1, Number(detail.width) || image.naturalWidth);
      this.sourceH = Math.max(1, Number(detail.height) || image.naturalHeight);
      this.setBusy(false);
      requestAnimationFrame(() => {
        if (!this.openFlag) return;
        this.fit();
      });
    } catch (error) {
      this.setBusy(false);
      toast("原图载入失败，请重新导入这张图片。", "error");
      this.draw();
    }
  }

  async close() {
    if (!this.openFlag) return;
    clearTimeout(this.saveTimer);
    this.saveTimer = 0;
    clearTimeout(this.outputTimer);
    clearTimeout(this.curveTimer);
    while (this.saving) await new Promise((resolve) => setTimeout(resolve, 40));
    if (this.outputDirty) await this._saveOutputNow();
    if (this.curveDirty) await this._saveCurveNow();
    if (this.quadDirty && !this.dirty) this.markEdited();
    try {
      if (this.dirty) await this.flush();
    } catch { /* reported by flush */ }
    if (this.dirty) {
      toast("裁切修改尚未保存成功，请检查提示后重试。", "error");
      return;
    }
    this.openFlag = false;
    this.loadToken++;
    clearTimeout(this.resultTimer);
    this.resultToken += 1;
    if (this.resultUrl) {
      URL.revokeObjectURL(this.resultUrl);
      this.resultUrl = null;
    }
    const e = this.els;
    e.modal.hidden = true;
    this.image = null;
    this.detail = null;
    this.polygon = [];
    this.strokes = [];
    this.history = [];
    this.setBusy(false);
    e.preview.removeAttribute("src");
    this.app.updateBodyLock();
    this.app.onEditorClosed();
  }

  setBusy(value, message = "正在重新识别…") {
    this.busy = Boolean(value);
    const e = this.els;
    e.busy.hidden = !this.busy;
    if (this.busy) {
      const span = e.busy.querySelector("span:not(.spinner)");
      if (span) span.textContent = message;
    }
  }

  setMode(mode) {
    if (!["vertex", "quad", "add", "erase", "pan", "region", "outline"].includes(mode)) return;
    if (this.resultMode) this.setView("source");
    this.mode = mode;
    this.selected = -1;
    this.selectedCorner = -1;
    this.regionDrag = null;
    if (mode !== "outline") this.outline = [];
    if (mode === "quad" && this.quad.length !== 4) {
      this.quad = clone(this.detail?.detection?.quad || []);
    }
    this._syncModeButtons();
    const hints = {
      vertex: "拖动顶点修改边缘；点击边线添加顶点；右键删除顶点",
      quad: "拖动纸张的四个角；用于完整矩形的透视校正",
      add: "按住左键涂抹，把漏掉的纸边补回来",
      erase: "按住左键涂抹，擦掉多余的背景",
      pan: "拖动画面平移；滚轮缩放",
      region: "纸片模式：单击纸片/背景 = 重新识别该处；右键 = 弃选/恢复；拖动一块到相邻的另一块 = 合并",
      outline: "手绘模式：依次点击外轮廓，双击或回车闭合；Esc 取消",
    };
    this.els.hint.textContent = hints[mode] || "";
    this.els.brush.hidden = !["add", "erase"].includes(mode);
    const canvas = this.els.canvas;
    canvas.classList.toggle("mode-pan", mode === "pan");
    canvas.classList.toggle("mode-add", mode === "add");
    canvas.classList.toggle("mode-erase", mode === "erase");
    canvas.classList.toggle("mode-region", mode === "region");
    canvas.classList.toggle("mode-outline", mode === "outline");
    this.draw();
  }

  _syncModeButtons() {
    $$(".modes button").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === this.mode);
    });
  }

  // ------------------------------------------------------------------
  // Region layer: several pieces on one scan

  _regionColor(index) {
    return this.regionPalette[index % this.regionPalette.length];
  }

  _regionSize(region) {
    const box = region.bbox || [];
    if (box.length === 4) {
      return `${Math.round(Number(box[2]) || 0)}×${Math.round(Number(box[3]) || 0)}`;
    }
    return "";
  }

  _syncRegionPanel() {
    const list = this.els.regionList;
    if (!list) return;
    const regions = this.regions || [];
    this.els.regionAuto.checked = this.detail?.detect_multi !== false;
    list.innerHTML = "";
    if (!regions.length) {
      const empty = document.createElement("div");
      empty.className = "region-empty";
      empty.textContent = "还没有纸片，点「重新判断」或用手绘画框。";
      list.appendChild(empty);
      return;
    }
    regions.forEach((region, index) => {
      const row = document.createElement("div");
      row.className = "region-row";
      row.dataset.region = region.id;
      if (region.enabled === false) row.classList.add("muted");
      row.title = region.enabled === false ? "已弃选，导出时跳过" : "已选中，导出时包含";
      const dot = document.createElement("span");
      dot.className = "region-dot";
      dot.style.background = this._regionColor(index);
      const name = document.createElement("span");
      name.className = "region-name";
      name.textContent = region.label || String(index + 1);
      const size = document.createElement("span");
      size.className = "region-size";
      size.textContent = this._regionSize(region);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "region-btn";
      toggle.dataset.regionAction = "toggle";
      toggle.title = region.enabled === false ? "恢复导出" : "弃选（不导出这块）";
      toggle.innerHTML = icon("eye");
      const rectify = document.createElement("button");
      rectify.type = "button";
      rectify.className = `region-btn${region.rectify ? " on" : ""}`;
      rectify.dataset.regionAction = "rectify";
      rectify.title = "四角拉正：拉伸四边顶点，输出长方形";
      rectify.textContent = "▭";
      const del = document.createElement("button");
      del.type = "button";
      del.className = "region-btn";
      del.dataset.regionAction = "delete";
      del.title = "删除这块纸片";
      del.innerHTML = icon("trash");
      del.disabled = regions.length <= 1;
      row.append(dot, name, size, toggle, rectify, del);
      list.appendChild(row);
    });
  }

  _onRegionListClick(event) {
    const button = event.target.closest("[data-region-action]");
    if (!button) return;
    const row = button.closest("[data-region]");
    if (!row) return;
    const region = (this.regions || []).find((item) => item.id === row.dataset.region);
    if (!region) return;
    const action = button.dataset.regionAction;
    if (action === "toggle") {
      this.updateRegion(region.id, { enabled: region.enabled === false });
    } else if (action === "rectify") {
      this.updateRegion(region.id, { rectify: !region.rectify });
    } else if (action === "delete") {
      this.deleteRegion(region);
    }
  }

  _regionAt(point) {
    const regions = this.regions || [];
    for (let index = regions.length - 1; index >= 0; index -= 1) {
      if (this._pointInPolygon(point, regions[index].polygon || [])) return regions[index];
    }
    return null;
  }

  _pointInPolygon([x, y], polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
      const xi = Number(polygon[i][0]);
      const yi = Number(polygon[i][1]);
      const xj = Number(polygon[j][0]);
      const yj = Number(polygon[j][1]);
      const crosses = (yi > y) !== (yj > y);
      if (crosses && x < ((xj - xi) * (y - yi)) / ((yj - yi) || 1e-9) + xi) inside = !inside;
    }
    return inside;
  }

  async _regionCall(payload, message = "正在处理纸片…") {
    if (!this.detail || this.regionBusy) return null;
    this.regionBusy = true;
    this.setBusy(true, message);
    try {
      const detail = await api.regions(this.detail.id, payload);
      this.dirty = false;
      clearTimeout(this.saveTimer);
      this._applyDetail(detail, { replaceGeometry: true });
      this.app.onImageUpdated(detail);
      this.draw();
      return detail;
    } catch (error) {
      toast(error.message || "纸片操作失败", "error");
      return null;
    } finally {
      this.regionBusy = false;
      this.setBusy(false);
    }
  }

  async updateRegion(regionId, patch) {
    const detail = await this._regionCall({ action: "update", id: regionId, patch },
      "正在更新纸片…");
    if (detail && patch.enabled !== undefined) {
      toast(patch.enabled ? "已恢复导出这块纸片" : "已弃选这块纸片（不导出）", "success");
    }
  }

  async deleteRegion(region) {
    if ((this.regions || []).length <= 1) {
      toast("至少要保留一块纸片；整张不要请删除图片。", "info");
      return;
    }
    const proceed = await this.app.confirm({
      title: `删除纸片 ${region.label || ""}？`,
      message: "只是从这张图里去掉这一块，原图不会删除。",
      okLabel: "删除",
      danger: true,
    });
    if (!proceed) return;
    await this._regionCall({ action: "delete", id: region.id }, "正在删除纸片…");
  }

  async redetectRegions() {
    const detail = await this._regionCall({
      action: "detect",
      multi: this.els.regionAuto.checked,
    }, "正在重新判断纸片…");
    if (detail) {
      toast(`已重新判断：${(detail.regions || []).length} 块纸片`, "success");
      this._applyDetail(detail, { replaceGeometry: true });
      this.draw();
    }
  }

  async toggleRegionAuto() {
    const multi = this.els.regionAuto.checked;
    const detail = await this._regionCall({ action: "detect", multi }, "正在按新设置重新判断…");
    if (detail) {
      toast(multi ? "已开启：自动判断一张图里的多块纸片" : "已关闭：只保留最大的一块纸片", "success");
    }
  }

  async addRegionAt(point) {
    const inside = this._regionAt(point);
    if (!this.detail) return;
    const detail = await this._regionCall({
      action: "add", x: point[0], y: point[1],
    }, "正在重新识别该处…");
    if (!detail) return;
    const created = detail.created_regions || [];
    if (created.length > 1) toast("这里其实有多块纸片，已拆开", "success");
    else if (inside) toast(`已重新识别纸片 ${inside.label || ""}`, "success");
    else toast("已新增一块纸片", "success");
    this.draw();
  }

  async mergeRegions(regionIds) {
    if (regionIds.length < 2) return;
    const detail = await this._regionCall({ action: "merge", ids: regionIds }, "正在合并纸片…");
    if (detail) {
      toast("已合并为一块纸片", "success");
      this.draw();
    }
  }

  async commitOutline() {
    if (this.outline.length < 3) {
      toast("手绘纸片至少需要 3 个点。", "info");
      return;
    }
    const polygon = this.outline.map((point) => [Number(point[0]), Number(point[1])]);
    this.outline = [];
    const detail = await this._regionCall({ action: "manual", polygon }, "正在加入手绘纸片…");
    if (detail) toast("已加入手绘纸片", "success");
    this.draw();
  }

  _syncWrapClass() {
    const wrap = this.els.wrap;
    const canvas = this.els.canvas;
    const width = canvas.offsetWidth || 0;
    const height = canvas.offsetHeight || 0;
    const overflow = width > wrap.clientWidth - 20 || height > wrap.clientHeight - 20;
    wrap.classList.toggle("zoomed", overflow);
  }

  _updateEdgeLabels() {
    const e = this.els;
    const expand = Number(e.expand.value);
    e.expandLabel.textContent = `${expand > 0 ? "+" : ""}${expand} px`;
    e.featherLabel.textContent = `${Number(e.feather.value).toFixed(1)} px`;
    e.simplifyLabel.textContent = `${Number(e.simplify.value).toFixed(1)} px`;
  }

  _syncControls() {
    if (!this.detail) return;
    const e = this.els;
    const params = this.detail.params || {};
    const detection = this.detail.detection || {};
    const fine = Number(this.detail.fine_angle || 0);
    e.fine.value = String(clamp(fine, -45, 45));
    e.fineLabel.textContent = `${fine.toFixed(1)}°`;
    e.alignEdge.value = this.detail.align_edge || "top";
    const expand = Number(params.expand ?? 0);
    const feather = Number(params.feather ?? 0.8);
    const simplify = Number(params.simplify ?? 2);
    e.expand.value = String(clamp(expand, -30, 30));
    e.feather.value = String(clamp(feather, 0, 4));
    e.simplify.value = String(clamp(simplify, 0, 10));
    this._updateEdgeLabels();

    const manualThreshold = params.threshold;
    if (manualThreshold == null) {
      e.thresholdAuto.checked = true;
      e.threshold.disabled = true;
      e.threshold.value = String(clamp(Number(detection.threshold ?? 100), 1, 254));
      e.thresholdLabel.textContent = "自动";
    } else {
      e.thresholdAuto.checked = false;
      e.threshold.disabled = false;
      e.threshold.value = String(clamp(Number(manualThreshold), 1, 254));
      e.thresholdLabel.textContent = e.threshold.value;
    }
    e.invert.checked = Boolean(params.invert);
    e.detectHoles.checked = Boolean(params.detect_holes);
    const curve = this.detail.curve || {};
    e.curveEnabled.checked = Boolean(curve.enabled);
    e.curveAmount.disabled = !e.curveEnabled.checked;
    e.curveAmount.value = String(clamp(Number(curve.amount ?? 0.55), 0, 1));
    e.curveAmountLabel.textContent = Number(e.curveAmount.value).toFixed(2);
    this._syncPointCount();
    e.undo.disabled = this.history.length === 0;
    this.els.sub.textContent = [
      `${this.detail.width} × ${this.detail.height} px`,
      formatBytes(this.detail.size_bytes),
      `旋转 ${this.detail.rotation || 0}°`,
      Math.abs(Number(this.detail.fine_angle) || 0) >= 0.1
        ? `拉直 ${Number(this.detail.fine_angle).toFixed(1)}°` : "",
      this.detail.manual ? "手动修正" : "自动轮廓",
    ].filter(Boolean).join(" · ");
    this._syncOutputUi();
  }

  _syncPointCount() {
    this.els.pointCount.textContent = String(this.polygon.length);
  }

  _syncOutputUi() {
    if (!this.detail) return;
    const e = this.els;
    const output = this.detail.output || {};
    this.outputMode = output.mode === "rect" ? "rect" : "cutout";
    $$('.output-modes button').forEach((button) => {
      button.classList.toggle("active", button.dataset.outputMode === this.outputMode);
    });
    e.outputRectification.value = output.rectification || "auto";
    e.outputFill.value = output.fill || "paper";
    e.outputColor.value = output.color || "#ffffff";
    e.outputColorLabel.textContent = e.outputColor.value;
    e.outputPadding.value = String(Number(output.padding || 0));
    e.outputAspect.value = output.aspect || "paper";
    e.outputAspectW.value = String(Math.round(Number(output.aspect_w || 210)));
    e.outputAspectH.value = String(Math.round(Number(output.aspect_h || 297)));
    e.outputEdge.value = output.edge === "actual" ? "actual" : "fit";
    e.outputEdgeMargin.value = String(Number(output.edge_margin || 0));
    e.outputEdgeMarginLabel.textContent = `${e.outputEdgeMargin.value} px`;
    e.outputRectFields.classList.toggle("disabled", this.outputMode !== "rect");
    e.outputColorRow.hidden = e.outputFill.value !== "color";
    e.outputAspectRow.hidden = e.outputAspect.value !== "custom";
    e.outputEnhance.checked = Boolean(output.enhance);
    e.outputWhiten.value = String(Number(output.whiten ?? 35));
    e.outputPurity.value = String(Number(output.purity ?? 35));
    e.outputDesaturate.value = String(Number(output.desaturate ?? 0));
    e.outputMonoBoost.checked = Boolean(output.mono_boost);
    e.outputWhitenLabel.textContent = e.outputWhiten.value;
    e.outputPurityLabel.textContent = e.outputPurity.value;
    e.outputDesaturateLabel.textContent = `${e.outputDesaturate.value}%`;
    this._syncEnhanceUi();
    const quad = this.quad.length === 4;
    e.outputQuadStatus.textContent = this.detail.quad_manual
      ? "手动四角"
      : (quad ? "自动四角" : "外接框");
    e.outputQuadEdit.disabled = this.outputMode !== "rect";
    e.outputQuadAuto.disabled = !(this.detail.detection?.quad?.length === 4);
  }

  _updateOutputUi() {
    const e = this.els;
    e.outputColorRow.hidden = e.outputFill.value !== "color";
    e.outputAspectRow.hidden = e.outputAspect.value !== "custom";
  }

  _syncEnhanceUi() {
    const e = this.els;
    const enabled = e.outputEnhance.checked;
    e.outputWhiten.disabled = !enabled;
    e.outputPurity.disabled = !enabled;
    const mono = Number(e.outputDesaturate.value) > 0;
    e.outputMonoBoost.disabled = !mono;
    if (!mono) e.outputMonoBoost.checked = false;
  }

  _outputFromControls() {
    const e = this.els;
    return {
      mode: this.outputMode || "cutout",
      rectification: e.outputRectification.value,
      fill: e.outputFill.value,
      color: e.outputColor.value,
      padding: Number(e.outputPadding.value) || 0,
      aspect: e.outputAspect.value,
      aspect_w: Number(e.outputAspectW.value) || 210,
      aspect_h: Number(e.outputAspectH.value) || 297,
      edge: e.outputEdge.value,
      edge_margin: Number(e.outputEdgeMargin.value) || 0,
      enhance: e.outputEnhance.checked,
      whiten: Number(e.outputWhiten.value) || 0,
      purity: Number(e.outputPurity.value) || 0,
      desaturate: Number(e.outputDesaturate.value) || 0,
      mono_boost: e.outputMonoBoost.checked,
    };
  }

  setOutputMode(mode) {
    if (!["cutout", "rect"].includes(mode)) return;
    this.outputMode = mode;
    if (this.detail) {
      this.detail.output = { ...(this.detail.output || {}), mode };
    }
    this._syncOutputUi();
    this._saveOutputSoon();
  }

  _saveOutputSoon() {
    if (!this.detail) return;
    this.outputDirty = true;
    clearTimeout(this.outputTimer);
    this.outputTimer = setTimeout(async () => {
      this.outputDirty = false;
      await this.patch({ output: this._outputFromControls() });
      if (this.resultMode) this._scheduleResultPreview();
    }, 280);
  }

  async _saveOutputNow() {
    clearTimeout(this.outputTimer);
    if (!this.outputDirty) return;
    this.outputDirty = false;
    await this.patch({ output: this._outputFromControls() });
    if (this.resultMode) this._scheduleResultPreview();
  }

  _saveCurveSoon() {
    if (!this.detail) return;
    this.curveDirty = true;
    clearTimeout(this.curveTimer);
    this.curveTimer = setTimeout(async () => {
      this.curveDirty = false;
      await this.patch({
        curve: {
          enabled: this.els.curveEnabled.checked,
          amount: Number(this.els.curveAmount.value),
        },
      });
    }, 280);
  }

  async _saveCurveNow() {
    clearTimeout(this.curveTimer);
    if (!this.curveDirty) return;
    this.curveDirty = false;
    await this.patch({
      curve: {
        enabled: this.els.curveEnabled.checked,
        amount: Number(this.els.curveAmount.value),
      },
    });
  }

  adjustPointCount(delta) {
    if (!this.detail || !this.polygon.length) return;
    const target = Math.max(3, Math.min(20000, this.polygon.length + Math.trunc(delta)));
    if (target === this.polygon.length) return;
    this._pushHistory();
    this.polygon = target > this.polygon.length
      ? this._densifyPolygon(this.polygon, target)
      : this._decimatePolygon(this.polygon, target);
    this.selected = -1;
    this._syncPointCount();
    this.markEdited();
    this.draw();
    this.scheduleSave(180);
  }

  _densifyPolygon(polygon, target) {
    const n = polygon.length;
    const add = target - n;
    if (add <= 0 || n < 2) return clone(polygon);
    const lengths = polygon.map((point, index) => {
      const next = polygon[(index + 1) % n];
      return Math.hypot(next[0] - point[0], next[1] - point[1]);
    });
    const total = lengths.reduce((sum, value) => sum + value, 0) || 1;
    const exact = lengths.map((length) => add * length / total);
    const counts = exact.map(Math.floor);
    let remainder = add - counts.reduce((sum, value) => sum + value, 0);
    const order = exact
      .map((value, index) => ({ index, fraction: value - Math.floor(value) }))
      .sort((a, b) => b.fraction - a.fraction);
    for (let i = 0; i < remainder; i += 1) counts[order[i % n].index] += 1;
    const result = [];
    for (let i = 0; i < n; i += 1) {
      const point = polygon[i];
      const next = polygon[(i + 1) % n];
      result.push([point[0], point[1]]);
      for (let k = 1; k <= counts[i]; k += 1) {
        const t = k / (counts[i] + 1);
        result.push([
          point[0] + (next[0] - point[0]) * t,
          point[1] + (next[1] - point[1]) * t,
        ]);
      }
    }
    return result;
  }

  _decimatePolygon(polygon, target) {
    let points = clone(polygon);
    while (points.length > target && points.length > 3) {
      let remove = 0;
      let smallest = Infinity;
      for (let i = 0; i < points.length; i += 1) {
        const prev = points[(i - 1 + points.length) % points.length];
        const current = points[i];
        const next = points[(i + 1) % points.length];
        const area = Math.abs(
          (current[0] - prev[0]) * (next[1] - prev[1])
          - (current[1] - prev[1]) * (next[0] - prev[0]),
        );
        if (area < smallest) { smallest = area; remove = i; }
      }
      points.splice(remove, 1);
    }
    return points;
  }

  async autoQuad() {
    const quad = this.detail?.detection?.quad;
    if (!quad || quad.length !== 4) {
      toast("没有可用的自动四角，请手动调整。", "info");
      return;
    }
    this.quad = clone(quad);
    this.quadDirty = false;
    this._syncOutputUi();
    this.draw();
    await this.patch({ quad: this.quad, quad_manual: false });
  }

  _renderWarnings() {
    const detail = this.detail;
    if (!detail) return;
    if (detail.status === "error") {
      this.els.warnings.innerHTML = `<div class="warning-item">${icon("warning")}<span>${detail.error || "识别失败"}</span></div>`;
      return;
    }
    const detection = detail.detection || {};
    showWarnings(this.els.warnings, detection.warnings || [], detection.confidence);
  }

  _updatePreview(force = false) {
    const e = this.els;
    const detail = this.detail;
    if (!detail || !detail.preview_url) {
      e.preview.removeAttribute("src");
      e.previewEmpty.hidden = false;
      e.previewEmpty.textContent = detail && detail.status === "error" ? "识别失败" : "尚无预览";
      e.resultMeta.textContent = detail && detail.status === "error" ? (detail.error || "") : "";
      return;
    }
    const url = force ? `${detail.preview_url}?t=${Date.now()}` : detail.preview_url;
    e.previewEmpty.hidden = true;
    e.preview.src = url;
    const detection = detail.detection || {};
    const bits = [
      `${detail.result_width} × ${detail.result_height} px`,
      detection.confidence != null ? `可信度 ${Math.round(detection.confidence * 100)}%` : "",
      detection.threshold != null ? `阈值 ${detection.threshold}` : "",
      detection.elapsed_ms != null ? `${detection.elapsed_ms} ms` : "",
      Math.abs(Number(detail.fine_angle) || 0) >= 0.1
        ? `拉直 ${Number(detail.fine_angle).toFixed(1)}°` : "",
      detail.manual ? "手动修正" : "",
    ].filter(Boolean);
    e.resultMeta.textContent = bits.join(" · ");
  }

  async rotate(delta) {
    if (!this.detail || this.busy) return;
    const rotation = ((Number(this.detail.rotation || 0) + delta) % 360 + 360) % 360;
    await this.patch({ rotation });
  }

  async patch(patch) {
    if (!this.detail || this.busy) return;
    try {
      const detail = await api.patchImage(this.detail.id, patch);
      this._applyDetail(detail, { replaceGeometry: false });
      this.app.onImageUpdated(detail);
    } catch (error) {
      toast(error.message || "保存失败", "error");
    }
  }

  async deskew() {
    if (!this.detail || this.busy) return;
    this.setBusy(true, "正在自动拉直…");
    try {
      const detail = await api.deskew(this.detail.id);
      this._applyDetail(detail, { replaceGeometry: false });
      this._syncControls();
      this.app.onImageUpdated(detail);
      toast(`已按纸张边缘拉直 ${Number(detail.fine_angle).toFixed(1)}°`, "success");
    } catch (error) {
      toast(error.message || "自动拉直失败", "error");
    } finally {
      this.setBusy(false);
    }
  }

  async changeAlignEdge(align) {
    if (!this.detail || this.busy) return;
    const previous = this.detail.align_edge || "top";
    this.setBusy(true, "正在按边重新拉直…");
    try {
      const detail = await api.alignEdge(this.detail.id, align);
      this._applyDetail(detail, { replaceGeometry: false });
      this._syncControls();
      this.app.onImageUpdated(detail);
      const labels = { top: "上边", bottom: "下边", left: "左边", right: "右边", all: "综合" };
      toast(`已按${labels[align] || "综合"}重新拉直 ${Number(detail.fine_angle).toFixed(1)}°`, "success");
    } catch (error) {
      this.els.alignEdge.value = previous;
      toast(error.message || "按边拉直失败", "error");
    } finally {
      this.setBusy(false);
    }
  }

  async recompute() {
    if (!this.detail || this.busy) return;
    if (this.detail.manual || this.strokes.length || this.history.length) {
      const proceed = await this.app.confirm({
        title: "重新识别会覆盖手动修改",
        message: "自动轮廓会替换当前的顶点与补边/擦除笔迹。确定继续吗？",
        okLabel: "重新识别",
        danger: true,
      });
      if (!proceed) return;
    }
    this.setBusy(true, "正在重新识别…");
    clearTimeout(this.saveTimer);
    try {
      const params = this._paramsFromControls();
      const detail = await api.recompute(this.detail.id, params);
      this.history = [];
      this.dirty = false;
      this._applyDetail(detail, { replaceGeometry: true });
      this._syncControls();
      this._renderWarnings();
      this.draw();
      this.app.onImageUpdated(detail);
      toast("已重新识别纸张边缘", "success");
    } catch (error) {
      toast(error.message || "重新识别失败", "error");
      this._renderWarnings();
    } finally {
      this.setBusy(false);
    }
  }

  _paramsFromControls() {
    const e = this.els;
    return {
      threshold: e.thresholdAuto.checked ? null : Number(e.threshold.value),
      invert: e.invert.checked,
      detect_holes: e.detectHoles.checked,
      simplify: Number(e.simplify.value),
      expand: Number(e.expand.value),
      feather: Number(e.feather.value),
    };
  }

  async _saveParams() {
    if (!this.detail || this.busy) return;
    await this.patch({ params: this._paramsFromControls() });
  }

  _applyDetail(detail, { replaceGeometry = false } = {}) {
    if (!detail) return;
    this.detail = detail;
    if (replaceGeometry) {
      this.polygon = clone(detail.polygon || []);
      this.strokes = clone(detail.strokes || []);
      this.quad = clone(detail.quad || []);
      this.paperRect = clone(detail.paper_rect || []);
      this.quadDirty = false;
      this.selected = -1;
      this.selectedCorner = -1;
    } else if (detail.paper_rect) {
      this.paperRect = clone(detail.paper_rect);
    }
    this._renderWarnings();
    this._updatePreview();
    this._syncControls();
    this.regions = clone(detail.regions || []);
    this._syncRegionPanel();
  }

  markEdited() {
    this.dirty = true;
    this.editVersion++;
    if (this.saving) this.pendingSave = true;
  }

  scheduleSave(delay = 450) {
    if (!this.detail) return;
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.flush(), delay);
  }

  async flush() {
    if (!this.detail || !this.dirty) return;
    if (this.saving) { this.pendingSave = true; return; }
    clearTimeout(this.saveTimer);
    this.saveTimer = 0;
    const version = this.editVersion;
    const id = this.detail.id;
    const payload = {
      polygon: this.polygon,
      strokes: this.strokes,
      manual: true,
    };
    if (this.quadDirty && this.quad.length === 4) {
      payload.quad = this.quad;
      payload.quad_manual = true;
    }
    this.saving = true;
    try {
      const detail = await api.patchImage(id, payload);
      this._applyDetail(detail, { replaceGeometry: version === this.editVersion });
      if (version === this.editVersion) this.dirty = false;
      this.quadDirty = false;
      this.app.onImageUpdated(detail);
    } catch (error) {
      toast(error.message || "保存裁切轮廓失败", "error");
    } finally {
      this.saving = false;
      if (this.pendingSave) {
        this.pendingSave = false;
        if (this.dirty) this.scheduleSave(120);
      }
    }
  }

  _pushHistory(snapshot = null) {
    this.history.push(snapshot || {
      polygon: clone(this.polygon),
      strokes: clone(this.strokes),
      quad: clone(this.quad),
    });
    if (this.history.length > 50) this.history.shift();
    this.els.undo.disabled = this.history.length === 0;
  }

  undo() {
    if (!this.history.length || this.busy) return;
    const state = this.history.pop();
    this.polygon = state.polygon;
    this.strokes = state.strokes;
    if (state.quad) this.quad = state.quad;
    this.selected = -1;
    this.selectedCorner = -1;
    this.quadDirty = true;
    this.els.undo.disabled = this.history.length === 0;
    this.markEdited();
    this.draw();
    this.scheduleSave(200);
  }

  _dpr(cssW, cssH) {
    let dpr = Math.min(window.devicePixelRatio || 1, 2);
    const maxPixels = 12_000_000;
    if (cssW * cssH * dpr * dpr > maxPixels) {
      dpr = Math.sqrt(maxPixels / Math.max(1, cssW * cssH));
    }
    return Math.max(1, dpr);
  }

  setView(view) {
    const next = view === "result";
    if (next && !this.image) return;
    this.resultMode = next;
    this._syncView();
    if (next) {
      this._loadResultPreview();
    } else {
      this.fit();
    }
  }

  _syncView() {
    $$("#editorViewToggle button").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === (this.resultMode ? "result" : "source"));
    });
    this.els.wrap.classList.toggle("result-view", this.resultMode);
    if (this.resultMode) {
      this.els.hint.textContent = "结果预览（只读）· 已叠加边缘/顶点指示；切回原始可继续编辑";
    } else {
      this.els.hint.textContent = "拖动顶点修改边缘；点击边线添加顶点；右键删除顶点";
    }
  }

  _scheduleResultPreview(delay = 320) {
    clearTimeout(this.resultTimer);
    if (!this.resultMode) return;
    this.resultTimer = setTimeout(() => this._loadResultPreview(), delay);
  }

  async _loadResultPreview() {
    if (!this.resultMode || !this.detail || !this.image) return;
    const token = ++this.resultToken;
    this.resultPending = true;
    this.setBusy(true, "正在生成结果预览…");
    try {
      const regions = (this.regions || []).filter((region) => region.enabled !== false);
      let image;
      let url;
      if (regions.length > 1) {
        const composite = await this._composeRegionResults(regions, token);
        if (!composite || token !== this.resultToken) return;
        ({ image, url } = composite);
      } else {
        const region = regions[0];
        const blob = await api.renderPreview(
          this.detail.id, this._outputFromControls(), 1800, true, region ? region.id : "",
        );
        if (token !== this.resultToken || !this.resultMode || !this.openFlag) return;
        url = URL.createObjectURL(blob);
        image = await loadImage(url);
        this.resultLayout = [];
      }
      if (token !== this.resultToken || !this.resultMode || !this.openFlag) {
        URL.revokeObjectURL(url);
        return;
      }
      const previous = this.resultUrl;
      this.resultUrl = url;
      this.resultImage = image;
      if (previous) URL.revokeObjectURL(previous);
      this.fit();
    } catch (error) {
      if (token === this.resultToken) toast(error.message || "结果预览生成失败", "error");
    } finally {
      if (token === this.resultToken) {
        this.resultPending = false;
        this.setBusy(false);
      }
    }
  }

  async _composeRegionResults(regions, token) {
    const output = this._outputFromControls();
    const blobs = await Promise.all(regions.map((region) => api.renderPreview(
      this.detail.id, output, 900, false, region.id,
    )));
    if (token !== this.resultToken || !this.openFlag) return null;
    const urls = blobs.map((blob) => URL.createObjectURL(blob));
    const images = await Promise.all(urls.map((url) => loadImage(url)));
    if (token !== this.resultToken || !this.openFlag) {
      urls.forEach((item) => URL.revokeObjectURL(item));
      return null;
    }
    const gap = 16;
    const columns = images.length <= 2 ? images.length : 2;
    const rows = Math.ceil(images.length / columns);
    const cellWidth = Math.round(Math.min(900, 2200 / columns));
    const cellHeights = [];
    for (let row = 0; row < rows; row += 1) {
      let tallest = 1;
      for (let column = 0; column < columns; column += 1) {
        const item = images[row * columns + column];
        if (!item) continue;
        tallest = Math.max(tallest, cellWidth * item.naturalHeight / Math.max(1, item.naturalWidth));
      }
      cellHeights.push(Math.round(tallest));
    }
    const canvasW = columns * cellWidth + gap * (columns - 1);
    const canvasH = cellHeights.reduce((sum, value) => sum + value, 0) + gap * (rows - 1);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, canvasW);
    canvas.height = Math.max(1, canvasH);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const layout = [];
    images.forEach((item, index) => {
      const row = Math.floor(index / columns);
      const column = index % columns;
      const scale = cellWidth / Math.max(1, item.naturalWidth);
      const drawH = Math.round(item.naturalHeight * scale);
      const x = column * (cellWidth + gap);
      const y = cellHeights.slice(0, row).reduce((sum, value) => sum + value, 0) + row * gap;
      ctx.drawImage(item, x, y, cellWidth, drawH);
      layout.push({
        label: regions[index].label || String(index + 1),
        rectify: Boolean(regions[index].rectify),
        x, y, width: cellWidth, height: drawH,
      });
    });
    urls.forEach((item) => URL.revokeObjectURL(item));
    const dataUrl = canvas.toDataURL("image/png");
    const image = await loadImage(dataUrl);
    this.resultLayout = layout;
    return { image, url: dataUrl };
  }

  fit() {
    if (!this.image || !this.openFlag) return;
    const wrap = this.els.wrap;
    const image = this.resultMode && this.resultImage ? this.resultImage : this.image;
    const imageW = this.resultMode && this.resultImage ? image.naturalWidth : this.sourceW;
    const imageH = this.resultMode && this.resultImage ? image.naturalHeight : this.sourceH;
    const availW = Math.max(80, wrap.clientWidth - 56);
    const availH = Math.max(80, wrap.clientHeight - 56);
    const previewScale = image.naturalWidth / imageW;
    let scale = Math.min(availW / imageW, availH / imageH);
    scale = clamp(scale, 0.02, Math.max(0.05, previewScale));
    this.scale = scale;
    this.fitScale = scale;
    this.minScale = scale * 0.5;
    this.maxScale = Math.max(
      scale,
      Math.min(
        Math.max(1, previewScale * 1.15),
        3000 / imageW,
        2200 / imageH,
      ),
    );
    this.userZoomed = false;
    this._applyScale();
    wrap.scrollTo({ left: 0, top: 0 });
  }

  zoomBy(factor) {
    if (!this.image) return;
    const wrap = this.els.wrap;
    const rect = wrap.getBoundingClientRect();
    this._zoomAt(factor, rect.left + rect.width / 2, rect.top + rect.height / 2);
  }

  _zoomAt(factor, clientX, clientY) {
    const wrap = this.els.wrap;
    const rect = wrap.getBoundingClientRect();
    const next = clamp(this.scale * factor, this.minScale, this.maxScale);
    if (Math.abs(next - this.scale) < 1e-6) return;
    const px = clientX - rect.left + wrap.scrollLeft;
    const py = clientY - rect.top + wrap.scrollTop;
    const ix = px / this.scale;
    const iy = py / this.scale;
    this.scale = next;
    this.userZoomed = true;
    this._applyScale();
    wrap.scrollLeft = ix * next - (clientX - rect.left);
    wrap.scrollTop = iy * next - (clientY - rect.top);
    this._syncWrapClass();
  }

  _applyScale() {
    if (!this.image) return;
    this.draw();
    this.els.zoomLabel.textContent = `${Math.round(this.scale * 100)}%`;
    this._syncWrapClass();
  }

  _curveAmount() {
    const enabled = this.els.curveEnabled.checked;
    return enabled ? Number(this.els.curveAmount.value) : 0;
  }

  _tracePath(ctx, points, amount) {
    if (!points.length) return;
    ctx.moveTo(points[0][0], points[0][1]);
    if (!amount) {
      for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i][0], points[i][1]);
      ctx.closePath();
      return;
    }
    const count = points.length;
    for (let i = 0; i < count; i += 1) {
      const p0 = points[(i - 1 + count) % count];
      const p1 = points[i];
      const p2 = points[(i + 1) % count];
      const p3 = points[(i + 2) % count];
      const c1x = p1[0] + (p2[0] - p0[0]) * amount / 6;
      const c1y = p1[1] + (p2[1] - p0[1]) * amount / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) * amount / 6;
      const c2y = p2[1] - (p3[1] - p1[1]) * amount / 6;
      ctx.bezierCurveTo(c1x, c1y, c2x, c2y, p2[0], p2[1]);
    }
    ctx.closePath();
  }

  draw() {
    const canvas = this.els.canvas;
    if (!this.image || !this.openFlag) return;
    this.els.pointCount.textContent = String(this.polygon.length);
    const activeImage = this.resultMode && this.resultImage ? this.resultImage : this.image;
    const imageW = this.resultMode && this.resultImage ? activeImage.naturalWidth : this.sourceW;
    const imageH = this.resultMode && this.resultImage ? activeImage.naturalHeight : this.sourceH;
    const cssW = Math.max(1, Math.round(imageW * this.scale));
    const cssH = Math.max(1, Math.round(imageH * this.scale));
    const dpr = this._dpr(cssW, cssH);
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(activeImage, 0, 0, cssW, cssH);
    if (this.resultMode) {
      this._drawResultLabels(ctx);
      return;
    }

    if (this.mode === "region") {
      this._drawRegionOverlay(ctx, true);
      return;
    }

    const points = this.polygon.map(([x, y]) => [x * this.scale, y * this.scale]);
    const curveAmount = this._curveAmount();
    if (points.length >= 3) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, cssW, cssH);
      this._tracePath(ctx, points, curveAmount);
      ctx.fillStyle = "rgba(18, 20, 17, .46)";
      ctx.fill("evenodd");
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      this._tracePath(ctx, points, curveAmount);
      ctx.lineJoin = "round";
      ctx.lineWidth = 3.4;
      ctx.strokeStyle = "rgba(255, 250, 240, .8)";
      ctx.stroke();
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = "#c65b43";
      ctx.stroke();
      ctx.restore();
    }

    if (this.paperRect.length === 4) {
      const rectPoints = this.paperRect.map(([x, y]) => [x * this.scale, y * this.scale]);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(rectPoints[0][0], rectPoints[0][1]);
      for (let i = 1; i < 4; i += 1) ctx.lineTo(rectPoints[i][0], rectPoints[i][1]);
      ctx.closePath();
      ctx.setLineDash([8, 6]);
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = "rgba(120, 205, 255, .9)";
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.restore();
    }

    const drawStroke = (stroke, active = false) => {
      const pts = stroke.points || [];
      if (!pts.length) return;
      const color = stroke.mode === "erase" ? "214, 74, 62" : "60, 150, 92";
      ctx.save();
      ctx.strokeStyle = `rgba(${color}, ${active ? 0.55 : 0.38})`;
      ctx.fillStyle = `rgba(${color}, ${active ? 0.45 : 0.3})`;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      const radius = Math.max(1, Number(stroke.radius || 24) * this.scale);
      ctx.lineWidth = radius * 2;
      if (pts.length === 1) {
        ctx.beginPath();
        ctx.arc(pts[0][0] * this.scale, pts[0][1] * this.scale, radius, 0, Math.PI * 2);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.moveTo(pts[0][0] * this.scale, pts[0][1] * this.scale);
        for (let i = 1; i < pts.length; i += 1) {
          ctx.lineTo(pts[i][0] * this.scale, pts[i][1] * this.scale);
        }
        ctx.stroke();
      }
      ctx.restore();
    };
    this.strokes.forEach((stroke) => drawStroke(stroke));
    if (this.brushStroke) drawStroke(this.brushStroke, true);

    if (this.mode === "vertex" && points.length) {
      const radius = 3.2;
      points.forEach(([x, y], index) => {
        ctx.beginPath();
        ctx.arc(x, y, index === this.selected ? radius + 2.6 : radius, 0, Math.PI * 2);
        ctx.fillStyle = index === this.selected ? "#fff8ee" : "#c65b43";
        ctx.fill();
        ctx.lineWidth = index === this.selected ? 2.2 : 1.2;
        ctx.strokeStyle = index === this.selected ? "#c65b43" : "rgba(255, 250, 240, .95)";
        ctx.stroke();
      });
    }

    if (this.mode === "quad" && this.quad.length === 4) {
      const quadPoints = this.quad.map(([x, y]) => [x * this.scale, y * this.scale]);
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(quadPoints[0][0], quadPoints[0][1]);
      for (let i = 1; i < 4; i += 1) ctx.lineTo(quadPoints[i][0], quadPoints[i][1]);
      ctx.closePath();
      ctx.lineWidth = 2.2;
      ctx.strokeStyle = "rgba(255, 196, 90, .95)";
      ctx.setLineDash([7, 5]);
      ctx.stroke();
      ctx.setLineDash([]);
      quadPoints.forEach(([x, y], index) => {
        ctx.beginPath();
        ctx.arc(x, y, index === this.selectedCorner ? 9 : 6.5, 0, Math.PI * 2);
        ctx.fillStyle = index === this.selectedCorner ? "#ffe2a8" : "#f0a63c";
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = "rgba(35, 30, 20, .85)";
        ctx.stroke();
      });
      ctx.restore();
    }

    if (["add", "erase"].includes(this.mode) && this.pointer) {
      const [x, y] = this.pointer;
      const radius = Math.max(2, Number(this.els.brushSize.value) * this.scale);
      ctx.save();
      ctx.beginPath();
      ctx.arc(x * this.scale, y * this.scale, radius, 0, Math.PI * 2);
      ctx.strokeStyle = this.mode === "erase" ? "rgba(255, 120, 100, .95)" : "rgba(120, 230, 160, .95)";
      ctx.lineWidth = 1.3;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.restore();
    }

    if ((this.regions || []).length > 1) this._drawRegionOverlay(ctx, false);
    if (this.mode === "outline") this._drawOutlineDraft(ctx);
  }

  _rgba(hex, alpha) {
    const value = String(hex || "#ffffff").replace("#", "");
    const full = value.length === 3
      ? value.split("").map((char) => char + char).join("")
      : value;
    const number = parseInt(full, 16);
    const r = (number >> 16) & 255;
    const g = (number >> 8) & 255;
    const b = number & 255;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  _tracePolygon(ctx, points) {
    if (!points.length) return;
    ctx.beginPath();
    ctx.moveTo(points[0][0], points[0][1]);
    for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i][0], points[i][1]);
    ctx.closePath();
  }

  _drawRegionOverlay(ctx, showAll) {
    const hovered = this.regionDrag && this.regionDrag.moved && this.pointer
      ? this._regionAt(this.pointer) : null;
    (this.regions || []).forEach((region, index) => {
      if (!showAll && index === 0) return;
      const points = (region.polygon || []).map(([x, y]) => [x * this.scale, y * this.scale]);
      if (points.length < 3) return;
      const color = this._regionColor(index);
      const disabled = region.enabled === false;
      ctx.save();
      this._tracePolygon(ctx, points);
      if (showAll) {
        ctx.fillStyle = this._rgba(color, disabled ? 0.05 : 0.14);
        ctx.fill();
      }
      ctx.setLineDash(disabled ? [7, 5] : []);
      ctx.lineWidth = showAll ? 2.6 : 1.9;
      ctx.strokeStyle = disabled ? "rgba(198, 198, 198, .9)" : color;
      ctx.stroke();
      if (hovered && hovered.id === region.id && this.regionDrag.regionId
          && this.regionDrag.regionId !== region.id) {
        ctx.setLineDash([]);
        ctx.lineWidth = 5.5;
        ctx.strokeStyle = "rgba(255, 240, 200, .95)";
        ctx.stroke();
      }
      ctx.setLineDash([]);
      if (showAll || (this.regions || []).length > 1) {
        const xs = points.map((point) => point[0]);
        const ys = points.map((point) => point[1]);
        const left = Math.min(...xs) + 8;
        const top = Math.min(...ys) + 8;
        const label = `${region.label || index + 1}${region.rectify ? " · 方" : ""}`;
        ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
        const width = ctx.measureText(label).width + 18;
        ctx.fillStyle = disabled ? "rgba(96, 96, 96, .86)" : color;
        const radius = 7;
        ctx.beginPath();
        if (typeof ctx.roundRect === "function") ctx.roundRect(left, top, width, 23, radius);
        else ctx.rect(left, top, width, 23);
        ctx.fill();
        ctx.fillStyle = "#fff";
        ctx.textBaseline = "middle";
        ctx.fillText(label, left + 9, top + 12.5);
      }
      ctx.restore();
    });
  }

  _drawOutlineDraft(ctx) {
    const points = (this.outline || []).map(([x, y]) => [x * this.scale, y * this.scale]);
    if (!points.length) return;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(points[0][0], points[0][1]);
    for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i][0], points[i][1]);
    if (this.pointer && this.mode === "outline") {
      ctx.lineTo(this.pointer[0] * this.scale, this.pointer[1] * this.scale);
    }
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#7ad1ff";
    ctx.stroke();
    ctx.setLineDash([]);
    points.forEach(([x, y]) => {
      ctx.beginPath();
      ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = "#7ad1ff";
      ctx.fill();
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = "rgba(20, 40, 55, .8)";
      ctx.stroke();
    });
    if (this.pointer) {
      ctx.font = "500 12px ui-sans-serif, system-ui, sans-serif";
      ctx.fillStyle = "rgba(10, 25, 35, .72)";
      const hint = points.length >= 3 ? "双击或回车闭合" : "继续点击";
      ctx.fillRect(this.pointer[0] * this.scale + 12, this.pointer[1] * this.scale + 12,
        ctx.measureText(hint).width + 14, 20);
      ctx.fillStyle = "#e6f6ff";
      ctx.textBaseline = "middle";
      ctx.fillText(hint, this.pointer[0] * this.scale + 19, this.pointer[1] * this.scale + 22);
    }
    ctx.restore();
  }

  _drawResultLabels(ctx) {
    const layout = this.resultLayout || [];
    if (layout.length < 2) return;
    layout.forEach((cell, index) => {
      const x = cell.x * this.scale + 8;
      const y = cell.y * this.scale + 8;
      const color = this._regionColor(index);
      const label = `${cell.label || index + 1}${cell.rectify ? " · 方" : ""}`;
      ctx.save();
      ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
      const width = ctx.measureText(label).width + 18;
      ctx.fillStyle = color;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") ctx.roundRect(x, y, width, 23, 7);
      else ctx.rect(x, y, width, 23);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.textBaseline = "middle";
      ctx.fillText(label, x + 9, y + 12.5);
      ctx.restore();
    });
  }

  _point(event) {
    const rect = this.els.canvas.getBoundingClientRect();
    return [
      (event.clientX - rect.left) / this.scale,
      (event.clientY - rect.top) / this.scale,
    ];
  }

  _screenPoint(point) {
    return [point[0] * this.scale, point[1] * this.scale];
  }

  _findVertex(sx, sy) {
    let best = -1;
    let bestDist = 11;
    this.polygon.forEach(([x, y], index) => {
      const [px, py] = this._screenPoint([x, y]);
      const distance = Math.hypot(px - sx, py - sy);
      if (distance < bestDist) { best = index; bestDist = distance; }
    });
    return best;
  }

  _findEdge(sx, sy) {
    let best = null;
    let bestDist = 9;
    const count = this.polygon.length;
    for (let i = 0; i < count; i += 1) {
      const a = this._screenPoint(this.polygon[i]);
      const b = this._screenPoint(this.polygon[(i + 1) % count]);
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      const length2 = dx * dx + dy * dy;
      if (length2 < 1) continue;
      let t = ((sx - a[0]) * dx + (sy - a[1]) * dy) / length2;
      t = clamp(t, 0, 1);
      const px = a[0] + t * dx;
      const py = a[1] + t * dy;
      const distance = Math.hypot(px - sx, py - sy);
      if (distance < bestDist) {
        bestDist = distance;
        best = { index: i, t, point: [px / this.scale, py / this.scale] };
      }
    }
    return best;
  }

  _clampPoint([x, y]) {
    return [clamp(x, 0, this.sourceW - 1), clamp(y, 0, this.sourceH - 1)];
  }

  _findCorner(sx, sy) {
    let best = -1;
    let bestDistance = 14;
    this.quad.forEach(([x, y], index) => {
      const distance = Math.hypot(x * this.scale - sx, y * this.scale - sy);
      if (distance < bestDistance) { best = index; bestDistance = distance; }
    });
    return best;
  }

  onPointerDown(event) {
    if (!this.image || this.busy || !this.detail) return;
    if (this.resultMode) return;
    if (event.button === 1 || this.mode === "pan") {
      event.preventDefault();
      this.drag = {
        kind: "pan",
        startX: event.clientX,
        startY: event.clientY,
        left: this.els.wrap.scrollLeft,
        top: this.els.wrap.scrollTop,
      };
      this.els.canvas.classList.add("dragging");
      this.els.canvas.setPointerCapture(event.pointerId);
      return;
    }
    if (event.button !== 0) return;
    const rect = this.els.canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const point = this._clampPoint(this._point(event));

    if (this.mode === "region") {
      event.preventDefault();
      const hit = this._regionAt(point);
      this.regionDrag = {
        regionId: hit ? hit.id : null,
        startX: sx,
        startY: sy,
        moved: false,
      };
      this.pointer = point;
      this.els.canvas.setPointerCapture(event.pointerId);
      this.draw();
      return;
    }

    if (this.mode === "outline") {
      event.preventDefault();
      this.outline.push(point);
      this.pointer = point;
      this.draw();
      return;
    }

    if (this.mode === "quad") {
      const corner = this._findCorner(sx, sy);
      if (corner >= 0) {
        this.selectedCorner = corner;
        this.drag = {
          kind: "quad",
          index: corner,
          snapshot: { polygon: clone(this.polygon), strokes: clone(this.strokes), quad: clone(this.quad) },
          moved: false,
          startX: sx,
          startY: sy,
        };
        this.els.canvas.setPointerCapture(event.pointerId);
        this.draw();
      }
      return;
    }

    if (this.mode === "add" || this.mode === "erase") {
      event.preventDefault();
      this._pushHistory();
      this.brushStroke = {
        mode: this.mode,
        radius: Number(this.els.brushSize.value),
        points: [point],
      };
      this.pointer = point;
      this.els.canvas.setPointerCapture(event.pointerId);
      this.draw();
      return;
    }

    const vertex = this._findVertex(sx, sy);
    if (vertex >= 0) {
      this.selected = vertex;
      this.drag = {
        kind: "vertex",
        index: vertex,
        snapshot: { polygon: clone(this.polygon), strokes: clone(this.strokes) },
        moved: false,
        startX: sx,
        startY: sy,
      };
      this.els.canvas.setPointerCapture(event.pointerId);
      this.draw();
      return;
    }

    const edge = this._findEdge(sx, sy);
    if (edge) {
      this._pushHistory();
      const insertAt = edge.index + 1;
      this.polygon.splice(insertAt, 0, edge.point);
      this.selected = insertAt;
      this.markEdited();
      this.drag = {
        kind: "vertex",
        index: insertAt,
        snapshot: null,
        moved: true,
        startX: sx,
        startY: sy,
      };
      this.els.canvas.setPointerCapture(event.pointerId);
      this.draw();
      return;
    }

    this.selected = -1;
    this.draw();
  }

  onPointerMove(event) {
    if (!this.image || !this.detail) return;
    const rect = this.els.canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const point = this._clampPoint(this._point(event));

    if (this.drag && this.drag.kind === "pan") {
      this.els.wrap.scrollLeft = this.drag.left - (event.clientX - this.drag.startX);
      this.els.wrap.scrollTop = this.drag.top - (event.clientY - this.drag.startY);
      return;
    }

    if (this.regionDrag) {
      this.pointer = point;
      if (Math.hypot(sx - this.regionDrag.startX, sy - this.regionDrag.startY) > 5) {
        this.regionDrag.moved = true;
      }
      this.draw();
      return;
    }

    if (this.mode === "outline") {
      this.pointer = point;
      this.draw();
      return;
    }

    if (this.drag && this.drag.kind === "quad") {
      if (!this.drag.moved && Math.hypot(sx - this.drag.startX, sy - this.drag.startY) > 1.5) {
        this.drag.moved = true;
        if (this.drag.snapshot) this._pushHistory(this.drag.snapshot);
      }
      if (this.drag.moved) {
        this.quad[this.drag.index] = point;
        this.quadDirty = true;
        if (this.detail) this.detail.quad_manual = true;
        this.markEdited();
        this._syncOutputUi();
        this.draw();
      }
      return;
    }

    if (this.drag && this.drag.kind === "vertex") {
      if (!this.drag.moved && Math.hypot(sx - this.drag.startX, sy - this.drag.startY) > 1.5) {
        this.drag.moved = true;
        if (this.drag.snapshot) this._pushHistory(this.drag.snapshot);
      }
      if (this.drag.moved) {
        this.polygon[this.drag.index] = point;
        this.markEdited();
        this.draw();
      }
      return;
    }

    if (this.brushStroke) {
      const pts = this.brushStroke.points;
      const [lx, ly] = pts[pts.length - 1];
      if (Math.hypot((point[0] - lx) * this.scale, (point[1] - ly) * this.scale) > 1.2) {
        pts.push(point);
        this.markEdited();
        this.draw();
      }
      return;
    }

    if (["add", "erase"].includes(this.mode)) {
      this.pointer = point;
      this.draw();
    } else if (this.mode === "vertex") {
      this.pointer = point;
    }
  }

  onPointerUp(event) {
    if (this.drag && this.drag.kind === "pan") {
      this.drag = null;
      this.els.canvas.classList.remove("dragging");
      return;
    }
    if (this.regionDrag) {
      const drag = this.regionDrag;
      this.regionDrag = null;
      const point = this._clampPoint(this._point(event));
      try { this.els.canvas.releasePointerCapture(event.pointerId); } catch { /* noop */ }
      this.draw();
      if (!drag.moved) {
        this.addRegionAt(point);
        return;
      }
      const target = this._regionAt(point);
      if (drag.regionId && target && target.id !== drag.regionId) {
        this.mergeRegions([drag.regionId, target.id]);
      }
      return;
    }
    if (this.mode === "outline") {
      try { this.els.canvas.releasePointerCapture(event.pointerId); } catch { /* noop */ }
      return;
    }
    if (this.drag && this.drag.kind === "quad") {
      const wasMoved = this.drag.moved;
      this.drag = null;
      if (wasMoved) {
        this.markEdited();
        this.scheduleSave(200);
      }
      this.draw();
      return;
    }
    if (this.drag && this.drag.kind === "vertex") {
      const wasMoved = this.drag.moved;
      this.drag = null;
      if (wasMoved) {
        this.markEdited();
        this.scheduleSave();
      }
      this.draw();
      return;
    }
    if (this.brushStroke) {
      const stroke = this.brushStroke;
      this.brushStroke = null;
      if (stroke.points.length) {
        this.strokes.push(stroke);
        this.markEdited();
        this.scheduleSave();
      }
      this.draw();
      return;
    }
    try { this.els.canvas.releasePointerCapture(event.pointerId); } catch { /* noop */ }
  }

  onContextMenu(event) {
    event.preventDefault();
    if (this.mode === "region") {
      const point = this._clampPoint(this._point(event));
      const region = this._regionAt(point);
      if (region) this.updateRegion(region.id, { enabled: region.enabled === false });
      return;
    }
    if (this.mode !== "vertex" || this.busy) return;
    const rect = this.els.canvas.getBoundingClientRect();
    const index = this._findVertex(event.clientX - rect.left, event.clientY - rect.top);
    if (index < 0) return;
    if (this.polygon.length <= 3) {
      toast("至少保留 3 个顶点。", "info");
      return;
    }
    this._pushHistory();
    this.polygon.splice(index, 1);
    this.selected = -1;
    this.markEdited();
    this.draw();
    this.scheduleSave(200);
  }

  onWheel(event) {
    if (!this.image) return;
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * 0.0014);
    this._zoomAt(factor, event.clientX, event.clientY);
  }

  onKeyDown(event) {
    if (!this.openFlag || !this.detail) return;
    const target = event.target;
    const typing = target && (
      target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT"
    );
    if (event.key === "Enter" && this.mode === "outline" && this.outline.length >= 3) {
      event.preventDefault();
      this.commitOutline();
      return;
    }
    if (event.key === "Escape" && this.mode === "outline" && this.outline.length) {
      event.preventDefault();
      this.outline = [];
      this.draw();
      return;
    }
    if (event.key === "Escape") {
      if (this.els.modal.hidden) return;
      event.preventDefault();
      this.close();
      return;
    }
    if (typing) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
      event.preventDefault();
      this.undo();
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      if (this.selected < 0 || this.polygon.length <= 3) {
        if (event.key === "Backspace") event.preventDefault();
        return;
      }
      event.preventDefault();
      this._pushHistory();
      this.polygon.splice(this.selected, 1);
      this.selected = -1;
      this.markEdited();
      this.draw();
      this.scheduleSave(200);
      return;
    }
    if (event.key === "+" || event.key === "=") this.zoomBy(1.2);
    if (event.key === "-" || event.key === "_") this.zoomBy(1 / 1.2);
    if (event.key === "0") this.fit();
  }
}
