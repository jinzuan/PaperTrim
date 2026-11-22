import { api } from "./api.js";
import {
  $, $$, copyText, esc, formatBytes, icon, toast,
} from "./ui.js";

export class ExportDialog {
  constructor(app) {
    this.app = app;
    this.kind = "folder";
    this.scope = "all";
    this.pathTouched = false;
    this.busy = false;
    this.job = null;
    this.pollTimer = 0;
    this.runToken = 0;
    this._bind();
  }

  get modal() { return $("#exportModal"); }

  _bind() {
    $("#exportClose").addEventListener("click", () => this.close());
    $("#exportCancel").addEventListener("click", () => this.close());
    $('[data-close="export"]').addEventListener("click", () => this.close());
    $$("#exportModal .export-tabs button").forEach((button) => {
      button.addEventListener("click", () => this.selectKind(button.dataset.kind));
    });
    $$('input[name="exportScope"]').forEach((input) => {
      input.addEventListener("change", () => {
        if (input.checked) this.scope = input.value;
      });
    });
    $("#resetPath").addEventListener("click", () => {
      this.pathTouched = false;
      this.loadDefaultPath();
    });
    $("#exportPath").addEventListener("input", () => { this.pathTouched = true; });
    $("#exportStart").addEventListener("click", () => this.start());
    $("#exportModal").addEventListener("change", (event) => {
      const target = event.target;
      if (target.matches("[data-opt]")) this._syncOptionUi(target);
    });
    $("#exportModal").addEventListener("input", (event) => {
      const target = event.target;
      if (target.matches('input[type="range"][data-opt]')) this._syncQuality(target);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.modal.hidden && !this.busy) this.close();
    });
  }

  async open(kind = null) {
    if (["folder", "pdf", "long", "zip"].includes(kind)) this.kind = kind;
    const ready = this.app.readyRecords();
    const selected = this.app.selectedReadyRecords();
    $("#scopeAllCount").textContent = String(ready.length);
    $("#scopeSelCount").textContent = String(selected.length);
    const selectedRadio = $('input[name="exportScope"][value="selected"]');
    selectedRadio.disabled = selected.length === 0;
    if (this.scope === "selected" && selected.length === 0) this.scope = "all";
    if (this.scope === "selected" && selected.length === 0) this.scope = "all";
    $$('input[name="exportScope"]').forEach((input) => {
      input.checked = input.value === this.scope;
    });
    this.modal.hidden = false;
    this.app.updateBodyLock();
    this._resetResult();
    this.busy = false;
    this.runToken++;
    clearTimeout(this.pollTimer);
    $("#exportCancel").textContent = "取消";
    await this.selectKind(this.kind, { keepPath: false });
    $("#exportStart").disabled = ready.length === 0;
    $("#exportSubtitle").textContent = ready.length
      ? `共 ${ready.length} 张已识别，选择导出方式`
      : "还没有可导出的图片";
  }

  close() {
    if (this.busy) {
      toast("导出还在进行，请等进度完成。", "info", 2600);
      return;
    }
    this.modal.hidden = true;
    this.busy = false;
    this.runToken++;
    clearTimeout(this.pollTimer);
    this.app.updateBodyLock();
  }

  async selectKind(kind, { keepPath = true } = {}) {
    if (!["folder", "pdf", "long", "zip"].includes(kind)) return;
    this.kind = kind;
    $$("#exportModal .export-tabs button").forEach((button) => {
      button.classList.toggle("active", button.dataset.kind === kind);
    });
    $$("#exportModal .export-options").forEach((panel) => {
      panel.hidden = panel.dataset.panel !== kind;
    });
    const subtitles = {
      folder: "每张裁切结果保存为单独文件",
      pdf: "按顺序合成一份多页 PDF",
      long: "把所有结果拼成一张长图",
      zip: "打包成 ZIP，可指定图片格式",
    };
    $("#exportSubtitle").textContent = subtitles[kind] || "";
    if (!keepPath || !this.pathTouched) {
      this.pathTouched = false;
      await this.loadDefaultPath();
    }
    const panel = this._activePanel();
    if (panel) this._syncFormatUi(panel);
    this._syncLongLabels();
  }

  async loadDefaultPath() {
    try {
      const data = await api.exportDefault(this.kind);
      $("#exportPath").value = data.path || "";
      this.pathTouched = false;
    } catch (error) {
      toast(error.message || "无法获取默认路径", "error");
    }
  }

  _activePanel() {
    return $(`#exportModal .export-options[data-panel="${this.kind}"]`);
  }

  _syncQuality(target) {
    const field = target.closest(".field");
    const output = field ? field.querySelector("output") : null;
    if (output) output.textContent = target.value;
  }

  _syncOptionUi(target) {
    if (target.matches('select[data-opt="direction"]')) this._syncLongLabels();
    if (target.matches('select[data-opt="format"]')) {
      const panel = target.closest(".export-options");
      if (panel) this._syncFormatUi(panel);
    }
  }

  _syncFormatUi(panel) {
    const format = panel.querySelector('[data-opt="format"]')?.value || "png";
    const qualityField = panel.querySelector("[data-quality-field]");
    if (qualityField) qualityField.hidden = format === "png";
    const pngOnly = ["png_optimize", "png_quantize"];
    for (const key of pngOnly) {
      const input = panel.querySelector(`[data-opt="${key}"]`);
      if (input) input.closest(".field").hidden = format !== "png";
    }
    const background = panel.querySelector('[data-opt="background"]');
    if (background && format === "jpg" && background.value === "transparent") {
      background.value = "white";
    }
  }

  _syncLongLabels() {
    const panel = $('#exportModal .export-options[data-panel="long"]');
    if (!panel) return;
    const direction = panel.querySelector('[data-opt="direction"]')?.value || "vertical";
    const label = panel.querySelector('[data-opt="target_size"]')?.closest(".field")?.querySelector("span");
    if (label) label.textContent = direction === "horizontal" ? "统一高度" : "统一宽度";
    const options = panel.querySelectorAll('[data-opt="target_size"] option');
    options.forEach((option) => {
      option.textContent = option.textContent.replace(/^(宽|高)/, direction === "horizontal" ? "高" : "宽");
    });
  }

  _collectOptions() {
    const panel = this._activePanel();
    const options = {};
    if (!panel) return options;
    panel.querySelectorAll("[data-opt]").forEach((input) => {
      const key = input.dataset.opt;
      if (input.type === "checkbox") {
        options[key] = input.checked;
      } else if (input.type === "range" || input.type === "number") {
        options[key] = Number(input.value);
      } else {
        options[key] = input.value;
      }
    });
    for (const key of ["quality", "dpi", "margin_mm", "target_size", "gap", "start", "max_side"]) {
      if (options[key] != null && options[key] !== "") {
        const numeric = Number(options[key]);
        if (Number.isFinite(numeric)) options[key] = numeric;
      }
    }
    if (options.format && options.format !== "png" && options.format !== "webp"
        && options.background === "transparent") {
      options.background = "white";
    }
    if (this.kind === "pdf" && options.background === "transparent") {
      options.background = "white";
    }
    options.path = $("#exportPath").value.trim();
    return options;
  }

  async start() {
    if (this.busy) return;
    const ready = this.app.readyRecords();
    const selected = this.app.selectedReadyRecords();
    if (this.scope === "selected" && selected.length === 0) {
      toast("还没有选中任何已识别的图片。", "info");
      return;
    }
    if (!ready.length) {
      toast("还没有可导出的图片。", "info");
      return;
    }
    this.busy = true;
    this.runToken++;
    const token = this.runToken;
    this._resetResult();
    $("#exportStart").disabled = true;
    $("#exportCancel").disabled = true;
    $("#exportCancel").textContent = "导出中…";
    $("#exportProgress").hidden = false;
    $("#exportProgress").className = "export-progress-panel preparing";
    $("#exportBar").style.width = "0";
    $("#exportProgressPercent").textContent = "0%";
    $("#exportProgressCount").textContent = "";
    $("#exportProgressTitle").textContent = "正在准备导出";
    $("#exportProgressText").textContent = "正在准备…";
    this._setProgressValue(0);

    const payload = {
      kind: this.kind,
      all: this.scope === "all",
      ids: Array.from(this.app.state.selected),
      options: this._collectOptions(),
    };
    try {
      const started = await api.startExport(payload);
      this.job = started.job;
      $("#exportProgress").className = "export-progress-panel";
      $("#exportProgressTitle").textContent = this._progressTitle(started.job);
      this._poll(token);
    } catch (error) {
      this._finishError(error.message || "导出失败", token);
    }
  }

  _progressTitle(job) {
    if (job.status === "error") return "导出失败";
    if (job.status === "done") return "导出完成";
    return {
      folder: "正在导出图片",
      pdf: "正在生成 PDF",
      long: "正在拼接长图",
      zip: "正在打包 ZIP",
    }[this.kind] || "正在导出";
  }

  _setProgressValue(percent) {
    const track = $("#exportProgress .progress-track");
    if (track) track.setAttribute("aria-valuenow", String(percent));
  }

  _updateProgress(job) {
    const total = Math.max(1, Number(job.total) || 1);
    const done = Math.max(0, Number(job.done) || 0);
    const match = String(job.message || "").match(/(\d+)\s*\/\s*(\d+)/);
    const messageDone = match ? Math.max(0, Number(match[1])) : done;
    const messageTotal = match ? Math.max(1, Number(match[2])) : total;
    const percent = job.status === "done"
      ? 100
      : Math.min(99, Math.round((messageDone / messageTotal) * 100));
    const panel = $("#exportProgress");
    panel.hidden = false;
    panel.className = "export-progress-panel";
    if (job.status === "done") panel.classList.add("done");
    if (job.status === "error") panel.classList.add("error");
    $("#exportBar").style.width = `${percent}%`;
    $("#exportProgressPercent").textContent = `${percent}%`;
    $("#exportProgressCount").textContent = job.status === "running"
      ? (match ? `${match[1]} / ${match[2]}` : `${done} / ${total}`)
      : "";
    $("#exportProgressTitle").textContent = this._progressTitle(job);
    $("#exportProgressText").textContent = job.message || "正在导出…";
    $("#exportProgressHint").textContent = job.status === "running"
      ? "请保持窗口打开" : (job.status === "error" ? "导出未完成" : "可以关闭窗口了");
    this._setProgressValue(percent);
  }

  _poll(token) {
    if (token !== this.runToken || !this.job) return;
    this.pollTimer = setTimeout(async () => {
      if (token !== this.runToken) return;
      try {
        const job = await api.getJob(this.job.id);
        if (token !== this.runToken) return;
        this.job = job;
        this._updateProgress(job);
        if (job.status === "running") {
          this._poll(token);
          return;
        }
        if (job.status === "done") this._finishSuccess(job, token);
        else this._finishError(job.error || "导出失败", token);
      } catch (error) {
        this._finishError(error.message || "无法获取导出进度", token);
      }
    }, 420);
  }

  _finishSuccess(job, token) {
    if (token !== this.runToken) return;
    this.busy = false;
    this.job = null;
    $("#exportStart").disabled = false;
    $("#exportCancel").disabled = false;
    $("#exportCancel").textContent = "关闭";
    this._updateProgress(job);
    $("#exportProgressText").textContent = "文件已写入磁盘";
    $("#exportProgressCount").textContent = "";
    $("#exportProgressHint").textContent = "可以关闭窗口了";
    const result = job.result || {};
    const path = result.path || "";
    const count = result.count ?? 0;
    const details = [];
    if (result.kind === "pdf") details.push(`${result.pages || count} 页`);
    if (result.kind === "long" && result.width) details.push(`${result.width} × ${result.height} px`);
    if (result.kind === "folder" || result.kind === "zip") details.push(`${count} 个文件`);
    if (result.size_bytes) details.push(formatBytes(result.size_bytes));
    const box = $("#exportResult");
    box.hidden = false;
    box.className = "export-result";
    box.innerHTML = `
      ${icon("check")}
      <div>
        <strong>导出完成</strong>
        <div class="path">${esc(path)}</div>
        <div>${esc(details.join(" · "))}</div>
      </div>
      <div class="result-actions">
        <button class="btn small ghost" data-result="open">${icon("folder-open")}<span>打开位置</span></button>
        <button class="btn small ghost" data-result="copy">复制路径</button>
      </div>`;
    box.querySelector('[data-result="open"]').onclick = () => {
      const select = !["folder"].includes(result.kind);
      api.openPath(path, select).catch((error) => toast(error.message, "error"));
    };
    box.querySelector('[data-result="copy"]').onclick = async () => {
      if (await copyText(path)) toast("路径已复制", "success");
    };
    toast(`导出完成：${path}`, "success", 6500);
  }

  _finishError(message, token) {
    if (token !== this.runToken) return;
    this.busy = false;
    this.job = null;
    $("#exportStart").disabled = false;
    $("#exportCancel").disabled = false;
    $("#exportCancel").textContent = "关闭";
    const panel = $("#exportProgress");
    panel.hidden = false;
    panel.className = "export-progress-panel error";
    $("#exportProgressTitle").textContent = "导出失败";
    $("#exportProgressPercent").textContent = "!";
    $("#exportProgressCount").textContent = "";
    $("#exportProgressText").textContent = message;
    $("#exportProgressHint").textContent = "导出未完成";
    $("#exportBar").style.width = "100%";
    this._setProgressValue(100);
    const box = $("#exportResult");
    box.hidden = false;
    box.className = "export-result error";
    box.innerHTML = `${icon("warning")}<div><strong>导出失败</strong><div>${esc(message)}</div></div>`;
    toast(message, "error", 6000);
  }

  _resetResult() {
    const box = $("#exportResult");
    box.hidden = true;
    box.className = "export-result";
    box.innerHTML = "";
    const panel = $("#exportProgress");
    panel.hidden = true;
    panel.className = "export-progress-panel";
    $("#exportBar").style.width = "0";
    $("#exportProgressPercent").textContent = "0%";
    $("#exportProgressCount").textContent = "";
    $("#exportProgressTitle").textContent = "正在导出";
    $("#exportProgressText").textContent = "正在准备…";
    $("#exportProgressHint").textContent = "请保持窗口打开";
    $("#exportCancel").disabled = false;
    $("#exportCancel").textContent = "取消";
    this._setProgressValue(0);
  }
}
