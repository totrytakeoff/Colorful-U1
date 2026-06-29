const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue;

const BASE = location.pathname.startsWith("/multiace/") ? "/multiace" : "";
const API = `${BASE}/api`;
const SCREEN = "/screen";
const GCODE_PREVIEW_BYTES = 16 * 1024 * 1024;
const GCODE_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024;
const DEFAULT_MATERIAL_OPTIONS = [
  "PLA", "PLA+", "PETG", "PETG-HF", "ABS", "ASA",
  "TPU", "PA", "PA-CF", "PC", "PVA",
];

function clone(value) {
  return JSON.parse(JSON.stringify(value ?? null));
}

function nowId() {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}

function validHex(color) {
  return /^#[0-9a-f]{6}$/i.test(String(color || ""));
}

function sourceKindLabel(kind) {
  if (kind === "native_feeder") return "Native";
  if (kind === "ace_slot") return "ACE";
  return kind || "Source";
}

function sourceSortKey(source) {
  if (!source) return "z";
  if (source.kind === "native_feeder") {
    return `a-${String(source.head ?? 99).padStart(2, "0")}`;
  }
  return `b-${String(source.ace ?? 99).padStart(2, "0")}-${String(source.slot ?? 99).padStart(2, "0")}`;
}

createApp({
  components: {
    "modal-panel": {
      template: "#modal-panel-template",
      props: { title: { type: String, required: true } },
      emits: ["close"],
    },
  },
  setup() {
    const storedTab = localStorage.getItem("colorful-u1.tab");
    const tab = ref(["console", "config", "upload"].includes(storedTab) ? storedTab : "console");
    const configTab = ref(localStorage.getItem("colorful-u1.configTab") || "device");
    watch(tab, (value) => localStorage.setItem("colorful-u1.tab", value));
    watch(configTab, (value) => localStorage.setItem("colorful-u1.configTab", value));

    const busy = reactive({
      refresh: false,
      preview: false,
      remap: false,
      validate: false,
      print: false,
      saveGraph: false,
      config: false,
      operation: false,
    });
    const globalError = ref("");
    const toast = ref(null);
    let toastTimer = 0;
    function setToast(message, kind = "ok") {
      toast.value = { message, kind };
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toast.value = null; }, 4500);
    }

    const version = reactive({ web: "", printer: {} });
    const state = reactive({
      printer_state: "",
      active_device: null,
      display_index_base: 0,
      aces: [],
      toolheads: [],
      error: "",
    });
    const sourceGraph = ref({ version: 1, heads: {}, sources: {}, edges: [], profiles: {} });
    const sourceGraphMeta = reactive({ hash: "", source: "", path: "", errors: [], warnings: [] });
    const sourceState = ref({ version: 1, source_graph_hash: "", heads: {}, meta: {} });
    const materialOptions = ref(DEFAULT_MATERIAL_OPTIONS.slice());
    const graphJson = ref("");
    const selectedHead = ref("head:0");
    const sourceFilter = ref("all");
    const sourceExecutionFields = [
      { key: "preload_length_mm", label: "Preload", min: 0, max: 3000, step: 1 },
      { key: "push_to_junction_length_mm", label: "To 4-way", min: 0, max: 3000, step: 1 },
      { key: "load_to_toolhead_length_mm", label: "To head", min: 0, max: 3000, step: 1 },
      { key: "unload_to_junction_length_mm", label: "Retract", min: 0, max: 3000, step: 1 },
      { key: "full_unload_length_mm", label: "Full unload", min: 0, max: 3000, step: 1 },
      { key: "feed_speed_mm_s", label: "Feed mm/s", min: 1, max: 120, step: 1 },
      { key: "retract_speed_mm_s", label: "Retract mm/s", min: 1, max: 120, step: 1 },
    ];

    async function api(path, options = {}) {
      const init = { ...options, headers: { ...(options.headers || {}) } };
      if (init.body && !(init.body instanceof FormData) && typeof init.body !== "string") {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(init.body);
      }
      const response = await fetch(`${API}${path}`, init);
      const text = await response.text();
      let data = {};
      if (text) {
        try { data = JSON.parse(text); }
        catch (_) { data = { detail: text }; }
      }
      if (!response.ok) {
        const detail = data.detail || data.message || `${response.status} ${response.statusText}`;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    }

    function applyState(payload) {
      state.printer_state = payload.printer_state || "";
      state.active_device = payload.active_device ?? null;
      state.display_index_base = Number(payload.display_index_base || 0);
      state.aces = Array.isArray(payload.aces) ? payload.aces : [];
      state.toolheads = Array.isArray(payload.toolheads) ? payload.toolheads : [];
      state.error = payload.error || "";
      for (const ace of state.aces) {
        if (!dryerCfg[ace.idx]) dryerCfg[ace.idx] = { temp: 50, duration: 240 };
      }
    }

    function applySourceGraph(payload) {
      sourceGraph.value = clone(payload.graph || {});
      const meta = payload.meta || {};
      sourceGraphMeta.hash = meta.hash || "";
      sourceGraphMeta.source = meta.source || "";
      sourceGraphMeta.path = meta.path || "";
      sourceGraphMeta.errors = Array.isArray(meta.errors) ? meta.errors : [];
      sourceGraphMeta.warnings = Array.isArray(meta.warnings) ? meta.warnings : [];
      if (!sourceGraph.value.heads?.[selectedHead.value]) {
        selectedHead.value = Object.keys(sourceGraph.value.heads || {})[0] || "head:0";
      }
      syncGraphJson();
    }

    async function loadVersion() {
      try {
        const payload = await api("/version");
        version.web = payload.web || "";
        version.printer = payload.printer || {};
      } catch (_) {}
    }

    async function reloadState() { applyState(await api("/state")); }
    async function reloadSourceGraph() { applySourceGraph(await api("/source-graph")); }
    async function reloadSourceState() { sourceState.value = await api("/source-state"); }
    async function loadMaterials() {
      try {
        const payload = await api("/materials");
        const materials = Array.isArray(payload.materials) ? payload.materials : [];
        materialOptions.value = materials.length ? materials : DEFAULT_MATERIAL_OPTIONS.slice();
      } catch (_) {
        materialOptions.value = DEFAULT_MATERIAL_OPTIONS.slice();
      }
    }

    async function refreshAll() {
      busy.refresh = true;
      globalError.value = "";
      try {
        await Promise.all([
          reloadState(),
          reloadSourceGraph(),
          reloadSourceState(),
          reloadOperation(),
          screenProbe(),
          reloadSnapshots(),
        ]);
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.refresh = false;
      }
    }

    function setTab(value) {
      tab.value = value;
      if (value === "config" && !config.loaded) loadConfig();
      if (value === "upload") nextTick(drawGcodePreview);
    }

    const versionLabel = computed(() => version.web ? `v${version.web}` : "source graph UI");
    const printerName = computed(() => version.printer?.device_name || "");
    const connectionState = computed(() => state.error || globalError.value ? "bad" : "ok");
    const connectionText = computed(() => state.error ? "Moonraker error" : (globalError.value ? "接口异常" : "Connected"));
    const aces = computed(() => state.aces || []);

    function displayIndex(index) {
      if (index == null || index === "") return "-";
      const match = String(index).match(/-?\d+$/);
      const value = match ? Number(match[0]) : Number(index);
      return Number.isFinite(value) ? value : "-";
    }
    function displayAce(index) { return index == null ? "-" : `ACE ${displayIndex(index)}`; }
    function shortHash(hash) { return hash ? hash.replace(/^sha256:/, "").slice(0, 12) : "-"; }
    function swatchStyle(color) { return { background: validHex(color) ? color : "#d9dbe1" }; }
    function ringStyle(color) { return { "--ring": validHex(color) ? color : "#d9dbe1" }; }
    function pretty(value) { return JSON.stringify(value, null, 2); }
    function formatBytes(value) {
      const size = Number(value || 0);
      if (!Number.isFinite(size) || size <= 0) return "0 B";
      const units = ["B", "KB", "MB", "GB"];
      let n = size;
      let i = 0;
      while (n >= 1024 && i < units.length - 1) {
        n /= 1024;
        i += 1;
      }
      return `${n >= 10 || i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
    }
    function formatHistoryTime(value) {
      const ts = Number(value || 0);
      if (!Number.isFinite(ts) || ts <= 0) return "-";
      return new Date(ts * 1000).toLocaleString();
    }
    function historySummary(entry) {
      const summary = entry?.summary || {};
      const tools = Array.isArray(summary.used_tools) ? summary.used_tools.length : 0;
      const events = Number(summary.route_events || 0);
      const swaps = Number(summary.active_ace_swaps || 0);
      return `${tools} tools · ${events} events · ${swaps} swaps`;
    }
    function dryerLabel(ace) {
      const dryer = ace?.dryer || {};
      return dryer.status && dryer.status !== "stop" ? dryer.status : "idle";
    }

    function liveSourceInfo(sourceId) {
      const aceMatch = /^ace:(\d+):(\d+)$/.exec(String(sourceId || ""));
      if (aceMatch) {
        const aceIdx = Number(aceMatch[1]);
        const slotIdx = Number(aceMatch[2]);
        const ace = (state.aces || []).find((item) => Number(item.idx) === aceIdx);
        const slot = (ace?.slots || []).find((item) => Number(item.idx) === slotIdx);
        if (!slot) return {};
        const slotState = slot.state || "";
        const ready = slotState !== "empty";
        return {
          material: slot.material || "",
          brand: slot.brand || "",
          subtype: slot.sku || slot.subtype || "",
          color: slot.color || "",
          ready,
          ready_reason: ready ? "ready" : "slot empty",
          state: slotState,
          status_details: { slot_state: slotState },
          rfid: slot.rfid_data || null,
        };
      }
      const nativeMatch = /^native:(\d+)$/.exec(String(sourceId || ""));
      if (nativeMatch) {
        const headIdx = Number(nativeMatch[1]);
        const head = (state.toolheads || []).find((item) => Number(item.idx) === headIdx);
        if (!head) return {};
        const detected = !!head.filament_detected;
        const inAcePath = !!head.filament_in_ace;
        const inNativePath = detected && !inAcePath;
        const atExtruder = !!head.filament_at_extruder;
        const channelError = head.channel_error || "";
        const channelState = head.channel_state || "";
        const channelOk = !channelError || channelError === "ok";
        const sourceStateOk = channelState === "load_finish" || channelState === "preload_finish";
        const ready = inNativePath && channelOk && sourceStateOk;
        const reasons = [];
        if (!inNativePath) reasons.push("native slot empty");
        if (!channelOk) reasons.push(`进料通道错误: ${channelError}`);
        if (inNativePath && !sourceStateOk) reasons.push(`通道状态: ${channelState || "unknown"}`);
        return {
          material: inNativePath ? (head.material || "") : "",
          brand: inNativePath ? (head.brand || "") : "",
          subtype: inNativePath ? (head.sku || head.subtype || "") : "",
          color: inNativePath ? (head.color || "") : "",
          ready,
          ready_reason: ready ? "ready" : reasons.join("; "),
          state: channelState,
          status_details: {
            filament_detected: detected,
            filament_in_ace: inAcePath,
            filament_in_native_path: inNativePath,
            filament_at_extruder: atExtruder,
            channel_error: channelError,
            channel_state: channelState,
          },
        };
      }
      return {};
    }

    const graphHeads = computed(() => {
      const heads = sourceGraph.value.heads || {};
      return Object.entries(heads)
        .map(([id, head]) => ({ id, ...head, label: head.label || `T${displayIndex(head.index)}` }))
        .sort((a, b) => Number(a.index) - Number(b.index));
    });

    const graphSources = computed(() => {
      const sources = sourceGraph.value.sources || {};
      const edges = sourceGraph.value.edges || [];
      return Object.entries(sources)
        .map(([id, source]) => {
          const live = liveSourceInfo(id);
          const heads = edges
            .filter((edge) => edge && edge.source === id && edge.enabled !== false)
            .map((edge) => edge.head);
          return {
            id,
            ...source,
            material: live.material || source.material || "",
            brand: live.brand || source.brand || "",
            subtype: live.subtype || source.subtype || "",
            color: live.color || source.color || "",
            ready: live.ready ?? source.ready,
            ready_reason: live.ready_reason || source.ready_reason || "",
            status_details: live.status_details || source.status_details || {},
            state: live.state || source.state || "",
            rfid: live.rfid || null,
            label: source.label || id,
            kindLabel: sourceKindLabel(source.kind),
            heads,
          };
        })
        .sort((a, b) => sourceSortKey(a).localeCompare(sourceSortKey(b)));
    });

    const selectedHeadLabel = computed(() => {
      const head = graphHeads.value.find((item) => item.id === selectedHead.value);
      return head ? `${head.label} · ${head.id}` : selectedHead.value;
    });
    const edgesForSelectedHead = computed(() => {
      return (sourceGraph.value.edges || []).filter((edge) => edge.head === selectedHead.value);
    });
    const runtimeHeadMap = computed(() => sourceState.value.heads || {});
    const materialBySource = computed(() => {
      const result = {};
      for (const source of graphSources.value) result[source.id] = source;
      return result;
    });
    const headCards = computed(() => {
      return graphHeads.value.map((head) => {
        const runtime = runtimeHeadMap.value[head.id] || {};
        const current = runtime.current_source || "";
        const material = materialBySource.value[current] || {};
        const sources = graphSources.value.filter((source) => source.heads.includes(head.id));
        const loadedKnown = !!current && runtime.source_confidence === "known";
        const materialLabel = material.material || (loadedKnown ? "未设置耗材" : "未装载");
        return {
          ...head,
          label: head.label || `T${displayIndex(head.index)}`,
          current_source: current,
          confidence: runtime.source_confidence || "unknown",
          sensor_filament: !!runtime.sensor_filament,
          last_error: runtime.last_error || "",
          sources,
          material,
          display_material: materialLabel,
        };
      });
    });
    const sourceAlerts = computed(() => {
      const alerts = [];
      for (const head of headCards.value) {
        if (["unknown", "stale", "failed", "exhausted"].includes(head.confidence)) {
          alerts.push(`${head.label}: ${head.confidence}`);
        }
      }
      return alerts;
    });
    const filteredSources = computed(() => {
      const filter = sourceFilter.value;
      return graphSources.value.filter((source) => {
        if (filter === "all") return true;
        if (filter === "native") return source.kind === "native_feeder";
        if (filter === "ace") return source.kind === "ace_slot";
        if (filter === "ready") return !!source.ready;
        if (filter === "error") return !source.ready || source.state === "error";
        return true;
      });
    });

    function sourceOptionsForHead(headId) {
      return graphSources.value.filter((source) => source.heads.includes(headId));
    }

    function edgeIndex(sourceId, headId) {
      return (sourceGraph.value.edges || []).findIndex((edge) => edge.source === sourceId && edge.head === headId);
    }
    function ensureEdge(sourceId, headId) {
      if (!Array.isArray(sourceGraph.value.edges)) sourceGraph.value.edges = [];
      let idx = edgeIndex(sourceId, headId);
      if (idx < 0) {
        sourceGraph.value.edges.push({
          source: sourceId,
          head: headId,
          enabled: false,
          priority: 100,
          constraints: {},
        });
        idx = sourceGraph.value.edges.length - 1;
      }
      return sourceGraph.value.edges[idx];
    }
    function edgeEnabled(sourceId, headId) {
      const idx = edgeIndex(sourceId, headId);
      return idx >= 0 && sourceGraph.value.edges[idx].enabled !== false;
    }
    function edgePriority(sourceId, headId) {
      const idx = edgeIndex(sourceId, headId);
      return idx >= 0 ? Number(sourceGraph.value.edges[idx].priority ?? 100) : 100;
    }
    function sourceExecutionValue(sourceId, key) {
      const source = (sourceGraph.value.sources || {})[sourceId] || {};
      const execution = source.execution || {};
      const value = execution[key];
      return value == null ? "" : value;
    }
    function setSourceExecutionValue(sourceId, key, value) {
      const source = (sourceGraph.value.sources || {})[sourceId];
      if (!source) return;
      if (!source.execution || typeof source.execution !== "object") source.execution = {};
      if (value === "" || value == null) {
        delete source.execution[key];
      } else {
        source.execution[key] = Number(value);
      }
      syncGraphJson();
    }
    function setEdgeEnabled(sourceId, headId, enabled) {
      ensureEdge(sourceId, headId).enabled = !!enabled;
      syncGraphJson();
    }
    function setEdgePriority(sourceId, headId, priority) {
      ensureEdge(sourceId, headId).priority = Number(priority || 100);
      syncGraphJson();
    }
    function boundHeadsForSource(sourceId, exceptHead = "") {
      return (sourceGraph.value.edges || [])
        .filter((edge) => edge.source === sourceId && edge.head !== exceptHead && edge.enabled !== false)
        .map((edge) => edge.head);
    }
    function bindingLabel(sourceId, headId) {
      const heads = boundHeadsForSource(sourceId, headId);
      if (!heads.length) return "";
      return ` · 已绑定 ${heads.map((id) => headLabelById(id)).join(", ")}`;
    }
    function headLabelById(headId) {
      const head = graphHeads.value.find((item) => item.id === headId);
      return head?.label || headId;
    }
    function setHeadSourceBinding(sourceId, headId, eventOrChecked) {
      const checked = typeof eventOrChecked === "boolean" ? eventOrChecked : !!eventOrChecked?.target?.checked;
      const rollback = () => {
        if (eventOrChecked?.target) eventOrChecked.target.checked = edgeEnabled(sourceId, headId);
      };
      if (!checked) {
        setEdgeEnabled(sourceId, headId, false);
        return;
      }
      const oldHeads = boundHeadsForSource(sourceId, headId);
      if (oldHeads.length) {
        const oldLabels = oldHeads.map((id) => headLabelById(id)).join(", ");
        const nextLabel = headLabelById(headId);
        const ok = window.confirm(`${sourceId} 已绑定到 ${oldLabels}。是否解除旧绑定并改绑定到 ${nextLabel}？`);
        if (!ok) {
          rollback();
          syncGraphJson();
          return;
        }
        for (const oldHead of oldHeads) ensureEdge(sourceId, oldHead).enabled = false;
      }
      ensureEdge(sourceId, headId).enabled = true;
      syncGraphJson();
    }
    function syncGraphJson() { graphJson.value = pretty(sourceGraph.value); }
    function applyGraphJson() {
      try {
        sourceGraph.value = JSON.parse(graphJson.value);
        setToast("source_graph JSON 已应用到表单，尚未保存");
      } catch (error) {
        globalError.value = `JSON 解析失败: ${error.message || error}`;
      }
    }
    async function saveSourceGraph() {
      busy.saveGraph = true;
      globalError.value = "";
      try {
        const payload = await api("/source-graph", { method: "POST", body: { graph: sourceGraph.value } });
        sourceGraphMeta.hash = payload.meta?.hash || "";
        sourceGraphMeta.source = payload.meta?.source || "";
        sourceGraphMeta.path = payload.meta?.path || "";
        sourceGraphMeta.errors = payload.meta?.errors || [];
        sourceGraphMeta.warnings = payload.meta?.warnings || [];
        await reloadSourceGraph();
        await reloadSourceState();
        syncGraphJson();
        setToast("Source Graph 已保存，旧 route plan 已失效");
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.saveGraph = false;
      }
    }

    const currentOperation = ref({ active: false });
    const operationBusy = computed(() => {
      const status = currentOperation.value?.status || "";
      return busy.operation || currentOperation.value?.active || status === "queued" || status === "running";
    });
    const operationLabel = computed(() => {
      const op = currentOperation.value || {};
      if (!op.id || (!op.active && op.status !== "error")) return "idle";
      const parts = [op.status || "unknown", op.kind || "operation"];
      if (op.head) parts.push(op.head);
      if (op.source) parts.push(op.source);
      return parts.join(" · ");
    });
    async function reloadOperation() {
      try {
        const payload = await api("/operation/current");
        currentOperation.value = payload.operation || { active: false };
      } catch (_) {}
    }
    async function pollOperation(id) {
      const started = Date.now();
      while (Date.now() - started < 20 * 60 * 1000) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        await reloadOperation();
        const op = currentOperation.value || {};
        if (id && op.id && op.id !== id && op.active) {
          throw new Error(`操作状态切换到 ${op.id}`);
        }
        if (!op.active && op.status) {
          if (op.status === "error") throw new Error(op.error || "硬件操作失败");
          return op;
        }
      }
      throw new Error("硬件操作超时");
    }
    async function runOperation(path, body, label) {
      if (operationBusy.value) {
        globalError.value = `已有硬件操作正在执行：${operationLabel.value}`;
        return null;
      }
      busy.operation = true;
      globalError.value = "";
      try {
        const payload = await api(path, { method: "POST", body });
        const op = payload.operation || {};
        currentOperation.value = op;
        if (op.active || op.status === "queued" || op.status === "running") {
          await pollOperation(op.id);
        }
        await Promise.allSettled([reloadState(), reloadSourceState(), reloadOperation()]);
        setToast(`${label} 完成`);
        return payload;
      } catch (error) {
        globalError.value = error.message || String(error);
        setToast(globalError.value, "error");
        await Promise.allSettled([reloadOperation(), reloadState(), reloadSourceState()]);
        return null;
      } finally {
        busy.operation = false;
      }
    }
    async function confirmUnloadAll() {
      if (!window.confirm("确认将所有工具头执行退料？该动作会移动送料系统。")) return;
      await runOperation("/operation/unload-all", { execute: true }, "全部退料");
    }

    const dryerCfg = reactive({});
    async function dryStart(aceIdx) {
      const cfg = dryerCfg[aceIdx] || { temp: 50, duration: 240 };
      await runOperation("/operation/ace/dry-start", {
        ace: aceIdx,
        temp: cfg.temp,
        duration: cfg.duration,
        execute: true,
      }, `ACE ${displayIndex(aceIdx)} 烘干启动`);
    }
    async function dryStop(aceIdx) {
      await runOperation("/operation/ace/dry-stop", {
        ace: aceIdx,
        execute: true,
      }, `ACE ${displayIndex(aceIdx)} 烘干停止`);
    }

    const headPanel = reactive({
      open: false,
      head: "head:0",
      targetSource: "",
      plan: null,
      status: "",
    });
    const activeHeadCard = computed(() => {
      return headCards.value.find((item) => item.id === headPanel.head)
        || headCards.value[0]
        || { label: "-", material: {}, confidence: "unknown", sources: [] };
    });
    function allowedSourcesForHead(headId) {
      return graphSources.value.filter((source) => source.heads.includes(headId));
    }
    function headBlocksLoad(head) {
      return ["unknown", "stale", "failed", "exhausted"].includes(head?.confidence);
    }
    function headBlocksUnload(head) {
      return ["unknown", "stale", "failed"].includes(head?.confidence);
    }
    function openHeadPanel(headId) {
      headPanel.head = headId || graphHeads.value[0]?.id || "head:0";
      headPanel.targetSource = allowedSourcesForHead(headPanel.head)[0]?.id || "";
      headPanel.plan = null;
      headPanel.status = "";
      headPanel.open = true;
    }
    function closeHeadPanel() {
      headPanel.open = false;
      headPanel.plan = null;
      headPanel.status = "";
    }
    async function runHeadLoadNow() {
      if (!headPanel.head || !headPanel.targetSource) return;
      const head = headCards.value.find((item) => item.id === headPanel.head);
      if (headBlocksLoad(head)) {
        globalError.value = `${head?.label || headPanel.head} 当前状态为 ${head?.confidence}，需要先恢复后再装载`;
        return;
      }
      headPanel.status = "执行装载...";
      const source = graphSources.value.find((item) => item.id === headPanel.targetSource);
      const payload = await runOperation("/operation/head/load", {
        head: headPanel.head,
        source: headPanel.targetSource,
        execute: true,
      }, `${headLabelById(headPanel.head)} 装载 ${source?.label || headPanel.targetSource}`);
      headPanel.status = payload ? "装载完成" : "";
    }
    async function runHeadUnloadNow(headId) {
      const head = headCards.value.find((item) => item.id === headId);
      if (!head?.current_source || head.confidence === "empty") {
        globalError.value = `${head?.label || headId} 当前没有已知已装载 source`;
        return;
      }
      if (headBlocksUnload(head)) {
        globalError.value = `${head?.label || headId} 当前状态为 ${head?.confidence}，不能自动退料，请先恢复`;
        return;
      }
      if (headPanel.open && headPanel.head === headId) headPanel.status = "执行退料...";
      const payload = await runOperation("/operation/head/unload", {
        head: headId,
        execute: true,
      }, `${head?.label || headId} 退料`);
      if (headPanel.open && headPanel.head === headId) headPanel.status = payload ? "退料完成" : "";
    }
    async function runHeadRecoverNow(headId) {
      const head = headCards.value.find((item) => item.id === headId);
      if (!head) return;
      if (head.confidence === "stale" && head.current_source) {
        const payload = await runOperation("/operation/head/recover", {
          head: headId,
          execute: true,
        }, `${head.label || headId} 恢复`);
        if (headPanel.open && headPanel.head === headId) headPanel.status = payload ? "恢复完成" : "";
        return;
      }
      if (head.confidence === "exhausted" && head.current_source) {
        return runHeadUnloadNow(headId);
      }
      globalError.value = `${head.label || headId} 当前状态为 ${head.confidence}，请先按提示处理物理料路后刷新状态`;
    }
    async function runSourceFullUnloadNow(sourceId) {
      const source = graphSources.value.find((item) => item.id === sourceId);
      if (!source) return;
      const loadedHead = headCards.value.find((head) => head.current_source === sourceId);
      if (loadedHead) {
        globalError.value = `${source.label} 当前装载在 ${loadedHead.label}，请先从工具头执行退料`;
        return;
      }
      if (source.ready === false) {
        globalError.value = `${source.label} 当前不可用：${source.ready_reason || "source not ready"}`;
        return;
      }
      await runOperation("/source/full-unload", {
        source: sourceId,
        execute: true,
      }, `${source.label} 完全退料`);
    }
    function humanPlanSummary(plan) {
      if (!plan) return "";
      if (plan.error) return plan.error;
      const target = plan.target || plan.event?.target || {};
      const source = target.source || plan.event?.source || headPanel.targetSource;
      const head = target.head_id || plan.event?.head || headPanel.head;
      const action = plan.event?.action || plan.event?.profile_action || plan.target?.kind || "action";
      const commands = plan.commands || plan.event?.commands || [];
      const commandCount = commands.length;
      if (action === "select_loaded") {
        return `${head} 已经装载 ${source}，不需要移动耗材。`;
      }
      if (action === "load") {
        return `将 ${source} 装载到 ${head}。预计会执行 ${commandCount} 个硬件动作。`;
      }
      if (action === "swap") {
        return `将 ${head} 切换到 ${source}。如当前头内有料，会按后端计划退料/换料。`;
      }
      if (action === "unload") {
        return `从 ${head} 退出现有耗材 ${source}。请确认料路没有堵塞。`;
      }
      return `准备对 ${head} 使用 ${source}，将执行 ${commandCount} 个动作。`;
    }
    const recovery = reactive({ open: false, head: "", message: "" });
    function openRecovery(headId) {
      const head = headCards.value.find((item) => item.id === headId);
      recovery.head = headId;
      if (head?.confidence === "stale" && head.current_source) {
        recovery.message = `${head?.label || headId} 当前状态为 stale，说明传感器已空但旧映射还残留。应先执行恢复清映射，再按需要重新装载。`;
      } else if (head?.confidence === "exhausted" && head.current_source) {
        recovery.message = `${head?.label || headId} 当前 source 标记为 exhausted，但工具头仍检测到耗材。非打印状态下应先执行退料恢复，把工具头内余料退出后再继续。`;
      } else {
        recovery.message = `${head?.label || headId} 当前状态为 ${head?.confidence || "unknown"}。自动装载已阻止，请检查物理耗材路径，必要时手动清理后再恢复。`;
      }
      recovery.open = true;
    }

    const materialEditor = reactive({
      open: false,
      source: "",
      kind: "",
      ace: null,
      slot: null,
      head: null,
      material: "",
      brand: "",
      subtype: "",
      color: "#ffffff",
      rfid: null,
    });
    function openMaterialEditor(sourceId) {
      const source = graphSources.value.find((item) => item.id === sourceId);
      if (!source) return;
      materialEditor.open = true;
      materialEditor.source = source.id;
      materialEditor.kind = source.kind;
      materialEditor.ace = source.ace ?? null;
      materialEditor.slot = source.slot ?? null;
      materialEditor.head = source.head ?? null;
      materialEditor.material = source.material || "";
      materialEditor.brand = source.brand || "";
      materialEditor.subtype = source.subtype || "";
      materialEditor.color = validHex(source.color) ? source.color : "#ffffff";
      materialEditor.rfid = source.rfid || null;
    }
    function closeMaterialEditor() { materialEditor.open = false; }
    function readMaterialFromRfid() {
      const rfid = materialEditor.rfid || {};
      if (rfid.material) materialEditor.material = rfid.material;
      if (rfid.brand) materialEditor.brand = rfid.brand;
      if (rfid.sku) materialEditor.subtype = rfid.sku;
      if (validHex(rfid.color)) materialEditor.color = rfid.color;
    }
    async function saveMaterialEditor() {
      const body = {
        material: materialEditor.material || "",
        brand: materialEditor.brand || "",
        subtype: materialEditor.subtype || "",
        color: materialEditor.color || "",
      };
      try {
        if (materialEditor.kind === "ace_slot") {
          await api("/slot-override", {
            method: "POST",
            body: { ace: materialEditor.ace, slot: materialEditor.slot, ...body },
          });
        } else {
          await api("/native-override", {
            method: "POST",
            body: { head: materialEditor.head, ...body },
          });
        }
        setToast("耗材信息已保存");
        closeMaterialEditor();
        Promise.allSettled([
          reloadState(),
          reloadSourceState(),
          reloadSourceGraph(),
        ]);
      } catch (error) {
        globalError.value = error.message || String(error);
      }
    }

    const snapshots = ref([]);
    const selectedSnapshot = ref("");
    async function reloadSnapshots() {
      try {
        const payload = await api("/snapshots");
        snapshots.value = payload.snapshots || [];
      } catch (_) {}
    }
    async function saveSnapshot() {
      const name = window.prompt("Preset name");
      if (!name) return;
      try {
        await api("/snapshots", { method: "POST", body: { name, description: "" } });
        await reloadSnapshots();
        selectedSnapshot.value = name;
        setToast("Loadout preset saved");
      } catch (error) {
        globalError.value = error.message || String(error);
      }
    }
    async function applySnapshot() {
      if (!selectedSnapshot.value) return;
      try {
        const plan = await api(`/snapshots/${encodeURIComponent(selectedSnapshot.value)}/apply`, { method: "POST" });
        if (plan.errors?.length) {
          globalError.value = plan.errors.map((item) => item.message).join("; ");
          return;
        }
        for (const proposal of plan.override_proposals || []) {
          await api("/slot-override", { method: "POST", body: proposal });
        }
        if ((plan.actions || []).length) {
          setToast("预设耗材已应用；硬件进退料动作请在对应工具头/source 上手动执行。", "warn");
        } else {
          setToast("Loadout preset applied");
        }
      } catch (error) {
        globalError.value = error.message || String(error);
      }
    }
    async function deleteSnapshot() {
      if (!selectedSnapshot.value) return;
      if (!window.confirm(`Delete preset ${selectedSnapshot.value}?`)) return;
      try {
        await api(`/snapshots/${encodeURIComponent(selectedSnapshot.value)}`, { method: "DELETE" });
        selectedSnapshot.value = "";
        await reloadSnapshots();
      } catch (error) {
        globalError.value = error.message || String(error);
      }
    }

    const screenAvailable = ref(false);
    const screenCanvas = ref(null);
    const floatScreenCanvas = ref(null);
    const screenPopout = ref(false);
    const screenFps = ref(0);
    const screenEtag = ref("");
    let screenTimer = 0;
    let frameCount = 0;
    let lastFpsTs = performance.now();
    let screenBusy = false;
    async function screenProbe() {
      try {
        const payload = await api("/screen-available");
        screenAvailable.value = !!payload.available;
      } catch (_) {
        screenAvailable.value = false;
      }
    }
    function liveScreenCanvases() {
      return [screenCanvas.value, floatScreenCanvas.value].filter(Boolean);
    }
    async function pollScreen() {
      if (screenBusy || !screenAvailable.value) return;
      const canvases = liveScreenCanvases();
      if (!canvases.length) return;
      screenBusy = true;
      try {
        const headers = {};
        if (screenEtag.value) headers["If-None-Match"] = `"${screenEtag.value}"`;
        const response = await fetch(`${SCREEN}/snapshot`, { headers, cache: "no-store" });
        if (response.ok && response.status !== 304) {
          screenEtag.value = (response.headers.get("ETag") || "").replace(/"/g, "");
          const image = await createImageBitmap(await response.blob());
          for (const canvas of canvases) {
            if (canvas.width !== image.width || canvas.height !== image.height) {
              canvas.width = image.width;
              canvas.height = image.height;
            }
            canvas.getContext("2d").drawImage(image, 0, 0);
          }
          frameCount += 1;
          const now = performance.now();
          if (now - lastFpsTs >= 1000) {
            screenFps.value = frameCount * 1000 / (now - lastFpsTs);
            frameCount = 0;
            lastFpsTs = now;
          }
        }
      } catch (_) {
      } finally {
        screenBusy = false;
      }
    }
    function startScreenPolling() {
      clearInterval(screenTimer);
      screenTimer = setInterval(pollScreen, 250);
    }
    function screenCoords(ev) {
      const canvas = ev.currentTarget;
      const rect = canvas.getBoundingClientRect();
      return {
        x: Math.round((ev.clientX - rect.left) * canvas.width / rect.width),
        y: Math.round((ev.clientY - rect.top) * canvas.height / rect.height),
      };
    }
    async function sendTouch(action, x, y) {
      try { await fetch(`${SCREEN}/touch?a=${action}&x=${x}&y=${y}`, { method: "POST" }); } catch (_) {}
    }
    function screenDown(ev) {
      ev.currentTarget?.setPointerCapture?.(ev.pointerId);
      const { x, y } = screenCoords(ev);
      sendTouch("down", x, y);
    }
    function screenMove(ev) {
      if (ev.buttons === 0) return;
      const { x, y } = screenCoords(ev);
      sendTouch("move", x, y);
    }
    function screenUp(ev) {
      const { x, y } = screenCoords(ev);
      sendTouch("up", x, y);
    }
    function toggleScreenPopout() {
      screenPopout.value = !screenPopout.value;
      nextTick(pollScreen);
    }

    const camera = reactive({
      url: localStorage.getItem("colorful-u1.camera.url") || "",
      mode: localStorage.getItem("colorful-u1.camera.mode") || "image",
      editing: !localStorage.getItem("colorful-u1.camera.url"),
      nonce: Date.now(),
    });
    const cameraUrl = computed(() => {
      if (!camera.url) return "";
      if (camera.mode !== "image") return camera.url;
      return camera.url.includes("?") ? `${camera.url}&_=${camera.nonce}` : `${camera.url}?_=${camera.nonce}`;
    });
    function saveCameraConfig() {
      localStorage.setItem("colorful-u1.camera.url", camera.url || "");
      localStorage.setItem("colorful-u1.camera.mode", camera.mode || "image");
      camera.editing = false;
      camera.nonce = Date.now();
      setToast("Camera config saved");
    }

    const config = reactive({
      loaded: false,
      path: "",
      content: "",
      params: {},
      perAceParams: {},
      loadError: "",
      log: "",
      restartKlipper: false,
    });
    const configForm = reactive({
      ace_device_count: 1,
      feed_speed: 80,
      retract_speed: 80,
      load_length: 2100,
      retract_length: 1950,
      swap_retract_length: "",
      swap_purge_length: "",
      dryer_temp: "",
      dryer_duration: "",
      load_retry: "",
      extrusion_retry: "",
      unload_retry: "",
      state_debug: false,
      usb_debug: false,
      fa_debug: false,
      perAce: [],
    });
    function makePerAceEntry() {
      return {
        dryer_temp: "",
        dryer_duration: "",
        feed_speed: "",
        retract_speed: "",
        load_length: "",
        retract_length: "",
        swap_retract_length: "",
        perSlot: Array.from({ length: 4 }, () => ({
          load_length: "",
          retract_length: "",
          swap_retract_length: "",
        })),
      };
    }
    function ensurePerAceLength() {
      const count = Math.max(0, Math.min(8, Number(configForm.ace_device_count || 0)));
      while (configForm.perAce.length < count) configForm.perAce.push(makePerAceEntry());
      while (configForm.perAce.length > count) configForm.perAce.pop();
    }
    watch(() => configForm.ace_device_count, ensurePerAceLength, { immediate: true });
    function paramsToForm(params = {}, perAce = {}) {
      const num = (key, fallback = configForm[key]) => params[key] != null && params[key] !== "" ? Number(params[key]) : fallback;
      const numOrEmpty = (value) => value != null && value !== "" ? Number(value) : "";
      const bool = (key) => String(params[key] ?? "false") === "true";
      configForm.ace_device_count = num("ace_device_count", 1);
      ensurePerAceLength();
      configForm.feed_speed = num("feed_speed", 80);
      configForm.retract_speed = num("retract_speed", 80);
      configForm.load_length = num("load_length", 2100);
      configForm.retract_length = num("retract_length", 1950);
      configForm.swap_retract_length = numOrEmpty(params.swap_retract_length);
      configForm.swap_purge_length = numOrEmpty(params.swap_purge_length);
      configForm.dryer_temp = numOrEmpty(params.dryer_temp);
      configForm.dryer_duration = numOrEmpty(params.dryer_duration);
      configForm.load_retry = numOrEmpty(params.load_retry);
      configForm.extrusion_retry = numOrEmpty(params.extrusion_retry);
      configForm.unload_retry = numOrEmpty(params.unload_retry);
      configForm.state_debug = bool("state_debug");
      configForm.usb_debug = bool("usb_debug");
      configForm.fa_debug = bool("fa_debug");
      for (let i = 0; i < configForm.perAce.length; i++) {
        const item = configForm.perAce[i];
        const sec = perAce[i] || perAce[String(i)] || {};
        item.dryer_temp = numOrEmpty(params[`dryer_temp_${i}`]);
        item.dryer_duration = numOrEmpty(params[`dryer_duration_${i}`]);
        item.feed_speed = numOrEmpty(sec.feed_speed);
        item.retract_speed = numOrEmpty(sec.retract_speed);
        item.load_length = numOrEmpty(sec.load_length);
        item.retract_length = numOrEmpty(sec.retract_length);
        item.swap_retract_length = numOrEmpty(sec.swap_retract_length);
        for (let s = 0; s < 4; s++) {
          item.perSlot[s].load_length = numOrEmpty(sec[`load_length_${s}`]);
          item.perSlot[s].retract_length = numOrEmpty(sec[`retract_length_${s}`]);
          item.perSlot[s].swap_retract_length = numOrEmpty(sec[`swap_retract_length_${s}`]);
        }
      }
    }
    async function loadConfig() {
      config.loadError = "";
      try {
        const payload = await api("/config");
        config.loaded = true;
        config.path = payload.path || "";
        config.content = payload.content || "";
        config.params = payload.params || {};
        config.perAceParams = payload.per_ace_params || {};
        paramsToForm(config.params, config.perAceParams);
      } catch (error) {
        config.loadError = error.message || String(error);
      }
    }
    function renderConfigContent(content) {
      const main = {
        ace_device_count: configForm.ace_device_count,
        feed_speed: configForm.feed_speed,
        retract_speed: configForm.retract_speed,
        load_length: configForm.load_length,
        retract_length: configForm.retract_length,
        swap_retract_length: configForm.swap_retract_length,
        swap_purge_length: configForm.swap_purge_length,
        dryer_temp: configForm.dryer_temp,
        dryer_duration: configForm.dryer_duration,
        load_retry: configForm.load_retry,
        extrusion_retry: configForm.extrusion_retry,
        unload_retry: configForm.unload_retry,
        state_debug: configForm.state_debug ? "true" : "false",
        usb_debug: configForm.usb_debug ? "true" : "false",
        fa_debug: configForm.fa_debug ? "true" : "false",
      };
      for (let i = 0; i < configForm.perAce.length; i++) {
        main[`dryer_temp_${i}`] = configForm.perAce[i].dryer_temp;
        main[`dryer_duration_${i}`] = configForm.perAce[i].dryer_duration;
      }
      const perAce = {};
      for (let i = 0; i < configForm.perAce.length; i++) {
        const item = configForm.perAce[i];
        perAce[i] = {
          feed_speed: item.feed_speed,
          retract_speed: item.retract_speed,
          load_length: item.load_length,
          retract_length: item.retract_length,
          swap_retract_length: item.swap_retract_length,
        };
        for (let s = 0; s < 4; s++) {
          perAce[i][`load_length_${s}`] = item.perSlot[s].load_length;
          perAce[i][`retract_length_${s}`] = item.perSlot[s].retract_length;
          perAce[i][`swap_retract_length_${s}`] = item.perSlot[s].swap_retract_length;
        }
      }
      return patchAceConfig(content || "[ace]\n", main, perAce);
    }
    function patchAceConfig(content, mainValues, perAceValues) {
      const lines = String(content || "").split("\n");
      const sections = [{ label: null, header: "", lines: [] }];
      const keyRegex = /^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/;
      const sectionRegex = /^\s*\[(.+?)\]\s*$/;
      const obsolete = new Set(["head0_mode", "head1_mode", "head2_mode", "head3_mode", "ace0_head", "ace1_head", "ace2_head", "ace3_head", "ace4_head", "ace5_head", "ace6_head", "ace7_head", "ace_route_mode", "ace_primary_head", "print_mode"]);
      for (const raw of lines) {
        const sm = raw.match(sectionRegex);
        if (sm) {
          const label = sm[1].trim();
          sections.push({ label, header: raw, lines: [] });
          continue;
        }
        sections[sections.length - 1].lines.push(raw);
      }

      function aceIndex(label) {
        if (!label || !label.startsWith("ace ")) return null;
        const value = Number(label.split(/\s+/, 2)[1]);
        return Number.isInteger(value) ? String(value) : null;
      }

      function updateLines(rawLines, values, removeObsolete = false) {
        const seen = new Set();
        const out = [];
        for (const raw of rawLines) {
          const km = raw.match(keyRegex);
          if (km) {
            const key = km[1];
            if (removeObsolete && obsolete.has(key)) continue;
            if (key in values) {
              seen.add(key);
              const value = values[key];
              if (value !== "" && value != null) out.push(`${key}: ${value}`);
              continue;
            }
          }
          out.push(raw);
        }
        for (const [key, value] of Object.entries(values)) {
          if (!seen.has(key) && value !== "" && value != null) out.push(`${key}: ${value}`);
        }
        return out;
      }

      let hasMain = false;
      const existingAce = new Set();
      for (const section of sections) {
        if (section.label === "ace") {
          hasMain = true;
          section.lines = updateLines(section.lines, mainValues, true);
          continue;
        }
        const idx = aceIndex(section.label);
        if (idx != null) {
          existingAce.add(idx);
          if (perAceValues[idx]) {
            section.lines = updateLines(section.lines, perAceValues[idx], false);
          }
        }
      }
      if (!hasMain) {
        sections.push({
          label: "ace",
          header: "[ace]",
          lines: updateLines([], mainValues, true),
        });
      }
      for (const [idx, values] of Object.entries(perAceValues)) {
        if (!existingAce.has(String(idx))) {
          sections.push({
            label: `ace ${idx}`,
            header: `[ace ${idx}]`,
            lines: updateLines([], values, false),
          });
        }
      }
      const rendered = [];
      for (const section of sections) {
        if (section.header) {
          if (rendered.length && rendered[rendered.length - 1] !== "") rendered.push("");
          rendered.push(section.header);
        }
        rendered.push(...section.lines);
      }
      return rendered.join("\n").replace(/\n{3,}/g, "\n\n");
    }
    async function saveConfig(restartKlipper = false) {
      if (!config.loaded) await loadConfig();
      busy.config = true;
      config.log = "Saving...";
      try {
        const content = renderConfigContent(config.content);
        const payload = await api("/config", {
          method: "PUT",
          body: { content, restart_klipper: !!restartKlipper },
        });
        config.content = content;
        config.log = `Saved ${payload.path || config.path}${restartKlipper ? " · Klipper restart requested" : ""}`;
      } catch (error) {
        config.log = "";
        globalError.value = error.message || String(error);
      } finally {
        busy.config = false;
      }
    }
    async function saveRawConfig() {
      busy.config = true;
      try {
        const payload = await api("/config", {
          method: "PUT",
          body: { content: config.content, restart_klipper: !!config.restartKlipper },
        });
        config.log = `Saved ${payload.path || config.path}`;
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.config = false;
      }
    }
    const debugState = reactive({ enabled: false, busy: false });
    const updateState = reactive({ current: "", latest: "", statusText: "", canApply: false, busy: "", log: "" });
    async function refreshDebugState() {
      try {
        const payload = await api("/debug-mode");
        debugState.enabled = !!payload.enabled;
      } catch (_) {}
    }
    async function debugEnable() {
      debugState.busy = true;
      try {
        const payload = await api("/debug-mode/enable", { method: "POST" });
        debugState.enabled = !!payload.enabled;
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        debugState.busy = false;
      }
    }
    async function debugDisable() {
      if (!window.confirm("Disable persistent debug mode?")) return;
      debugState.busy = true;
      try {
        const payload = await api("/debug-mode/disable", { method: "POST" });
        debugState.enabled = !!payload.enabled;
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        debugState.busy = false;
      }
    }
    function parseUpdate(payload) {
      updateState.log = payload.stdout || "";
      for (const line of payload.status_lines || []) {
        const cur = line.match(/current=(\S+)/);
        const latest = line.match(/latest=(\S+)|to=(\S+)/);
        if (cur) updateState.current = cur[1];
        if (latest) updateState.latest = latest[1] || latest[2];
        if (line.startsWith("update_available")) updateState.canApply = true;
        if (line.startsWith("up_to_date") || line.startsWith("done")) updateState.canApply = false;
        updateState.statusText = line;
      }
    }
    async function updateCheck() {
      updateState.busy = "check";
      try { parseUpdate(await api("/update/check")); }
      catch (error) { globalError.value = error.message || String(error); }
      finally { updateState.busy = ""; }
    }
    async function updateApply() {
      if (!window.confirm("Apply Colorful-U1 update?")) return;
      updateState.busy = "apply";
      try { parseUpdate(await api("/update/apply", { method: "POST" })); }
      catch (error) { globalError.value = error.message || String(error); }
      finally { updateState.busy = ""; }
    }
    function profileCommandPreview(profile) {
      const lines = [];
      for (const key of ["load", "unload", "swap"]) {
        const action = profile?.[key];
        if (action?.command) lines.push(`${key}: ${action.command}`);
      }
      return lines.join("\n") || "no command";
    }

    const fileInput = ref(null);
    const gcodeCanvas = ref(null);
    const gcodePreview = reactive({
      name: "",
      bounds: null,
      layers: 0,
      toolChanges: 0,
      segments: [],
    });
    const gcodeBoundsLabel = computed(() => {
      const b = gcodePreview.bounds;
      if (!b) return "-";
      return `X ${b.minX.toFixed(1)}-${b.maxX.toFixed(1)}, Y ${b.minY.toFixed(1)}-${b.maxY.toFixed(1)}, Z ${b.minZ.toFixed(2)}-${b.maxZ.toFixed(2)}`;
    });
    const preflight = reactive({
      report: null,
      validation: null,
      job: null,
      previewJob: null,
      historyEntry: null,
    });
    const preflightHistory = reactive({ entries: [], loading: false, error: "" });
    const manualTargets = reactive({});
    const selectedTool = ref("");
    function applyPreflightReport(report) {
      preflight.report = report;
      selectedTool.value = report.slicer_colors?.[0] ? String(report.slicer_colors[0].t) : "";
      const targets = report.tool_targets || {};
      for (const tool of report.slicer_colors || []) {
        manualTargets[String(tool.t)] = targetKey(targets[String(tool.t)] || {});
      }
    }
    function resetPreflightReport() {
      preflight.report = null;
      preflight.validation = null;
      preflight.job = null;
      preflight.previewJob = null;
      preflight.historyEntry = null;
      for (const key of Object.keys(manualTargets)) delete manualTargets[key];
    }
    function fileClientMtime(file) {
      const value = Number(file?.lastModified || 0);
      return Number.isFinite(value) && value > 0 ? value : null;
    }
    async function loadRoutePlanHistory() {
      preflightHistory.loading = true;
      preflightHistory.error = "";
      try {
        const payload = await api("/route-plan/history");
        preflightHistory.entries = Array.isArray(payload.entries) ? payload.entries : [];
      } catch (error) {
        preflightHistory.error = error.message || String(error);
      } finally {
        preflightHistory.loading = false;
      }
    }
    async function matchRoutePlanHistory(file) {
      const params = new URLSearchParams({
        filename: file.name,
        size: String(file.size),
      });
      const mtime = fileClientMtime(file);
      if (mtime != null) params.set("client_mtime", String(mtime));
      return api(`/route-plan/history/match?${params.toString()}`);
    }
    async function restoreRoutePlanHistory(entry, { quiet = false } = {}) {
      if (!entry?.token) return false;
      const report = await api(`/route-plan/history/report?token=${encodeURIComponent(entry.token)}`);
      applyPreflightReport(report);
      preflight.historyEntry = entry;
      preflight.previewJob = {
        kind: "route_preview_history",
        stage: "history",
        percent: 100,
        done: true,
        error: null,
        filename: report.filename,
        token: report.token,
      };
      if ((report.resolve_errors || []).length) {
        preflight.validation = {
          ok: false,
          errors: ["耗材映射未完成，请先为未映射的 slicer tool 选择 source。"],
        };
      } else {
        await autoValidateRoutePlan();
      }
      if (!quiet) setToast("已从历史恢复分析，未重复上传");
      return true;
    }
    async function restoreRoutePlanHistoryFromClick(entry) {
      if (!entry?.token || busy.preview || busy.print) return;
      busy.preview = true;
      globalError.value = "";
      try {
        await restoreRoutePlanHistory(entry);
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.preview = false;
      }
    }
    async function handleFile(file) {
      if (!file) return;
      busy.preview = true;
      globalError.value = "";
      resetPreflightReport();
      try {
        const text = await file.slice(0, GCODE_PREVIEW_BYTES).text();
        parseGcodePreview(text, file.name);
        await nextTick(drawGcodePreview);
        try {
          const matched = await matchRoutePlanHistory(file);
          if (matched?.matched && matched.entry?.available) {
            if (matched.entry.match_confidence === "exact") {
              try {
                await restoreRoutePlanHistory(matched.entry, { quiet: true });
                setToast("已从历史恢复分析（文件信息完全匹配），未重复上传");
                await loadRoutePlanHistory();
                return;
              } catch (restoreError) {
                preflightHistory.error = restoreError.message || String(restoreError);
              }
            }
            preflight.previewJob = {
              kind: "route_preview_history_candidate",
              stage: "history candidate",
              percent: 100,
              done: true,
              error: null,
              filename: file.name,
              token: matched.entry.token,
            };
            await loadRoutePlanHistory();
            setToast("找到同名同大小历史；如确认是同一文件，可从历史中恢复，避免重新上传分析");
            return;
          }
        } catch (historyError) {
          preflightHistory.error = historyError.message || String(historyError);
        }
        const started = await uploadRoutePlanPreview(file);
        const report = await pollRoutePlanPreview(started.job_id);
        applyPreflightReport(report);
        if ((report.resolve_errors || []).length) {
          preflight.validation = {
            ok: false,
            errors: ["耗材映射未完成，请先为未映射的 slicer tool 选择 source。"],
          };
        } else {
          await autoValidateRoutePlan();
        }
        await loadRoutePlanHistory();
        setToast("G-code 已分析");
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.preview = false;
      }
    }
    async function uploadRoutePlanPreview(file) {
      const started = await api("/route-plan/preview/upload/start", {
        method: "POST",
        body: {
          filename: file.name,
          size: file.size,
          client_mtime: fileClientMtime(file),
        },
      });
      let offset = Number(started.offset || 0);
      preflight.previewJob = {
        kind: "route_preview_upload",
        stage: "upload",
        percent: file.size ? Math.round((offset / file.size) * 1000) / 10 : 0,
        done: false,
        error: null,
        filename: file.name,
        token: started.token,
      };
      while (offset < file.size) {
        const end = Math.min(file.size, offset + GCODE_UPLOAD_CHUNK_BYTES);
        const form = new FormData();
        form.append("file", file.slice(offset, end), file.name);
        const chunk = await api(
          `/route-plan/preview/upload/chunk?token=${encodeURIComponent(started.token)}&offset=${offset}`,
          { method: "POST", body: form },
        );
        offset = Number(chunk.offset || end);
        preflight.previewJob = {
          ...preflight.previewJob,
          stage: "upload",
          percent: file.size ? Math.round((offset / file.size) * 1000) / 10 : 0,
        };
      }
      const job = await api("/route-plan/preview/upload/commit", {
        method: "POST",
        body: { token: started.token },
      });
      preflight.previewJob = job;
      return job;
    }
    async function pollRoutePlanPreview(jobId) {
      if (!jobId) throw new Error("preview job missing");
      while (true) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const job = await api(`/route-plan/preview/status?job_id=${encodeURIComponent(jobId)}`);
        preflight.previewJob = job;
        if (!job.done) continue;
        if (job.error) throw new Error(job.error);
        if (!job.result) throw new Error("preview completed without result");
        return job.result;
      }
    }
    function handleFileInput(event) {
      handleFile(event.target.files?.[0]);
      event.target.value = "";
    }
    function handleDrop(event) { handleFile(event.dataTransfer?.files?.[0]); }
    function parseGcodePreview(text, name) {
      gcodePreview.name = name || "";
      gcodePreview.bounds = null;
      gcodePreview.layers = 0;
      gcodePreview.toolChanges = 0;
      gcodePreview.segments = [];
      let x = 0, y = 0, z = 0, tool = 0;
      const bounds = { minX: Infinity, minY: Infinity, minZ: Infinity, maxX: -Infinity, maxY: -Infinity, maxZ: -Infinity };
      const zSeen = new Set();
      for (const raw of text.split(/\r?\n/)) {
        const line = raw.split(";", 1)[0].trim();
        if (!line) continue;
        const tm = line.match(/^T(\d+)/i);
        if (tm) {
          tool = Number(tm[1]);
          gcodePreview.toolChanges += 1;
          continue;
        }
        if (!/^G0?1\b/i.test(line)) continue;
        const nx = /(?:^|\s)X(-?\d+(?:\.\d+)?)/i.exec(line);
        const ny = /(?:^|\s)Y(-?\d+(?:\.\d+)?)/i.exec(line);
        const nz = /(?:^|\s)Z(-?\d+(?:\.\d+)?)/i.exec(line);
        const nextX = nx ? Number(nx[1]) : x;
        const nextY = ny ? Number(ny[1]) : y;
        const nextZ = nz ? Number(nz[1]) : z;
        if (nx || ny) {
          if (gcodePreview.segments.length < 2500) gcodePreview.segments.push({ x1: x, y1: y, x2: nextX, y2: nextY, z: nextZ, tool });
          bounds.minX = Math.min(bounds.minX, nextX);
          bounds.minY = Math.min(bounds.minY, nextY);
          bounds.minZ = Math.min(bounds.minZ, nextZ);
          bounds.maxX = Math.max(bounds.maxX, nextX);
          bounds.maxY = Math.max(bounds.maxY, nextY);
          bounds.maxZ = Math.max(bounds.maxZ, nextZ);
        }
        if (nz) zSeen.add(nextZ.toFixed(3));
        x = nextX; y = nextY; z = nextZ;
      }
      gcodePreview.layers = zSeen.size;
      gcodePreview.bounds = Number.isFinite(bounds.minX) ? bounds : null;
    }
    function drawGcodePreview() {
      const canvas = gcodeCanvas.value;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(320, Math.round(rect.width * dpr));
      canvas.height = Math.max(220, Math.round(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = canvas.width / dpr;
      const h = canvas.height / dpr;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "#f6f7f9";
      ctx.fillRect(0, 0, w, h);
      const b = gcodePreview.bounds;
      if (!b || !gcodePreview.segments.length) {
        ctx.fillStyle = "#8a8f98";
        ctx.font = "14px system-ui";
        ctx.fillText("上传 G-code 后显示路径预览", 24, 36);
        return;
      }
      const pad = 24;
      const sx = (w - pad * 2) / Math.max(1, b.maxX - b.minX);
      const sy = (h - pad * 2) / Math.max(1, b.maxY - b.minY);
      const scale = Math.min(sx, sy);
      const colors = ["#0071e3", "#ff3b30", "#34c759", "#ffcc00", "#af52de", "#ff9500"];
      ctx.lineWidth = 1.2;
      for (const seg of gcodePreview.segments) {
        ctx.strokeStyle = colors[seg.tool % colors.length];
        ctx.beginPath();
        ctx.moveTo(pad + (seg.x1 - b.minX) * scale, h - pad - (seg.y1 - b.minY) * scale);
        ctx.lineTo(pad + (seg.x2 - b.minX) * scale, h - pad - (seg.y2 - b.minY) * scale);
        ctx.stroke();
      }
    }
    window.addEventListener("resize", () => { if (tab.value === "upload") drawGcodePreview(); });

    const usedTools = computed(() => [...(preflight.report?.slicer_colors || [])].sort((a, b) => Number(a.t) - Number(b.t)));
    const preflightTargets = computed(() => {
      const byKey = new Map();
      const add = (target) => {
        const key = target.key || targetKey(target);
        if (!key) return;
        const previous = byKey.get(key) || {};
        const merged = {
          ...previous,
          ...target,
          key,
        };
        merged.label = formatTarget(merged);
        merged.color = merged.color || merged.target?.color || "";
        merged.material = merged.material || merged.target?.material || "";
        byKey.set(key, merged);
      };
      for (const item of preflight.report?.resolve_candidates || []) add(item);
      for (const item of preflight.report?.configured_loadout || []) add(item);
      for (const item of preflight.report?.live_loadout || []) add(item);
      const rows = [...byKey.values()];
      return rows.sort((a, b) => a.label.localeCompare(b.label));
    });
    const preflightErrors = computed(() => {
      const errors = [];
      for (const item of preflight.report?.resolve_errors || []) {
        const tool = item.t == null ? "" : String(item.t);
        if (tool && targetForKey(manualTargets[tool])) continue;
        errors.push(item.message || JSON.stringify(item));
      }
      for (const event of preflight.report?.route_plan?.events || []) {
        if (event.action === "unmapped" || !event.source || !event.head) {
          const tool = event.slicer_tool == null ? "unknown" : `T${displayIndex(event.slicer_tool)}`;
          errors.push(`${tool}: 未映射到可用 source`);
        }
      }
      return Array.from(new Set(errors));
    });
    const manualMappingTools = computed(() => {
      const tools = new Set();
      for (const tool of usedTools.value) {
        if (!targetForKey(manualTargets[String(tool.t)])) tools.add(String(tool.t));
      }
      return [...tools].sort((a, b) => Number(a) - Number(b));
    });
    const preflightStatus = computed(() => {
      if (!preflight.report) return { label: "idle", detail: "等待上传 G-code", ok: false };
      if (preflight.report.route_plan && preflight.validation?.ok === true && !preflightErrors.value.length) {
        return { label: "ready", detail: "映射和 route plan 校验已通过", ok: true };
      }
      if (manualMappingTools.value.length) {
        return {
          label: "manual mapping",
          detail: `需要手动映射 ${manualMappingTools.value.map((t) => `T${displayIndex(t)}`).join(", ")}`,
          ok: false,
        };
      }
      if (!preflight.report.route_plan) {
        return { label: "route plan missing", detail: "请应用映射以生成 route plan", ok: false };
      }
      if (preflight.validation && !preflight.validation.ok) {
        return { label: "blocked", detail: "route plan 校验未通过", ok: false };
      }
      return { label: "pending", detail: "等待校验", ok: false };
    });
    const mappingCheckLabel = computed(() => {
      if (!preflight.report) return "idle";
      if (manualMappingTools.value.length) return preflightStatus.value.detail;
      if (!preflight.report.route_plan) return "待应用";
      if (preflightErrors.value.length) return `${preflightErrors.value.length} issue(s)`;
      return preflightStatus.value.ok ? "ok" : "待校验";
    });
    const canPrintRoutePlan = computed(() => {
      return !!preflight.report?.token && preflight.validation?.ok === true && preflightErrors.value.length === 0 && sourceAlerts.value.length === 0;
    });
    const canValidateRoutePlan = computed(() => {
      return !!preflight.report?.token && !!preflight.report?.route_plan && !manualMappingTools.value.length;
    });
    const canApplyMapping = computed(() => {
      return !!preflight.report?.token && !manualMappingTools.value.length && !preflightStatus.value.ok;
    });
    const swapSummary = computed(() => {
      const stats = preflight.report?.route_plan?.stats || preflight.report?.source_map?.swap_stats || {};
      const swaps = stats.active_ace_swaps ?? 0;
      const min = stats.estimated_swap_seconds_min ?? 0;
      const max = stats.estimated_swap_seconds_max ?? 0;
      return `${swaps} swaps · ${Math.round(min / 60)}-${Math.round(max / 60)} min`;
    });
    const routePlanSummary = computed(() => {
      const plan = preflight.report?.route_plan;
      if (!plan) return "no route plan";
      return pretty({
        version: plan.version,
        source_graph_hash: plan.source_graph_hash,
        used_tools: plan.used_tools,
        events: (plan.events || []).length,
        resources: plan.resources || {},
        execution: plan.execution || {},
      });
    });
    function targetKey(target) {
      if (!target || typeof target !== "object") return "";
      if (target.key) return String(target.key);
      if (target.edge?.source && target.edge?.head) return `${target.edge.source}->${target.edge.head}`;
      if (target.source && target.head_id) return `${target.source}->${target.head_id}`;
      if (target.source && target.head != null) return `${target.source}->head:${Number(target.head)}`;
      return "";
    }
    function targetForKey(key) {
      const found = preflightTargets.value.find((item) => item.key === key);
      if (!found) return null;
      return {
        key: found.key,
        source: found.source,
        head: found.head,
        head_id: found.head_id,
        edge: found.edge || { source: found.source, head: found.head_id || `head:${found.head}` },
        kind: found.kind,
        ace: found.ace,
        slot: found.slot,
        module: found.module,
        channel: found.channel,
        execution_profile: found.execution_profile,
        ready: found.ready,
        ready_reason: found.ready_reason,
        status_details: found.status_details,
      };
    }
    function sourceCardLabel(target) {
      if (!target) return "未映射";
      const kind = target.kind || target.edge?.source?.split(":")?.[0] || "";
      const material = target.material ? ` · ${target.material}` : "";
      if (kind === "native" || kind === "native_feeder" || String(target.source || "").startsWith("native:")) {
        const headValue = target.head ?? target.head_id ?? target.edge?.head ?? "";
        return `Native Slot ${displayIndex(headValue)}${material}`;
      }
      if (kind === "ace" || kind === "ace_slot" || String(target.source || "").startsWith("ace:")) {
        const ace = target.ace ?? String(target.source || "").split(":")[1];
        const slot = target.slot ?? String(target.source || "").split(":")[2];
        return `ACE ${displayIndex(ace)} S${displayIndex(slot)}${material}`;
      }
      return target.label || target.key || target.source || "Source";
    }
    function targetRouteLabel(target) {
      if (!target) return "";
      const headValue = target.head ?? target.head_id ?? target.edge?.head ?? "";
      const route = headValue === "" ? "" : `走 T${displayIndex(headValue)}`;
      const key = target.key || targetKey(target);
      return [route, key].filter(Boolean).join(" · ");
    }
    function sourceStatusLabel(target) {
      if (!target) return "";
      if (target.ready === false) {
        return `不可用 · ${target.ready_reason || "source not ready"}`;
      }
      if (target.ready == null) {
        return `未知 · ${target.ready_reason || target.state || "no runtime state"}`;
      }
      return `可用 · ${target.ready_reason || target.state || "ready"}`;
    }
    function sourceReadyLabel(source) {
      if (!source || source.ready == null) return "未知";
      return source.ready ? "可用" : "不可用";
    }
    function sourceStatusClass(source) {
      if (!source || source.ready == null) return "unknown";
      return source.ready ? "ok" : "failed";
    }
    function headConfidenceLabel(head) {
      const confidence = head?.confidence || "unknown";
      const labels = {
        known: "已装载",
        empty: "未装载",
        unknown: "未知来源",
        stale: "状态过期",
        failed: "异常",
        exhausted: "耗尽",
      };
      return labels[confidence] || confidence;
    }
    function yesNo(value) {
      if (value == null) return "-";
      return value ? "yes" : "no";
    }
    function sourceRuntimeDetail(source) {
      if (!source) return "";
      const details = source.status_details || {};
      if (source.kind === "native_feeder" || String(source.id || "").startsWith("native:")) {
        return [
          `detect ${yesNo(details.filament_detected)}`,
          `extruder ${yesNo(details.filament_at_extruder)}`,
          `channel ${details.channel_state || source.state || "-"}`,
          details.channel_error && details.channel_error !== "ok" ? `error ${details.channel_error}` : "",
        ].filter(Boolean).join(" · ");
      }
      if (source.kind === "ace_slot" || String(source.id || "").startsWith("ace:")) {
        return `slot ${details.slot_state || source.state || "-"}`;
      }
      return source.state || "";
    }
    function formatTarget(target) {
      return sourceCardLabel(target);
    }
    function mappingLabel(tool) {
      const key = manualTargets[String(tool)];
      const target = preflightTargets.value.find((item) => item.key === key);
      return target ? target.label : "未映射";
    }
    function mappingCommands(tool) {
      const target = targetForKey(manualTargets[String(tool)]);
      if (!target) return [];
      if (target.kind === "native") return [`T${target.head}`];
      if (target.kind === "ace") return [`T${target.head}`, `ACE_SWAP_HEAD HEAD=${target.head} ACE=${target.ace} SLOT=${target.slot}`];
      return [];
    }
    function assignSelectedTool(key) {
      if (!selectedTool.value) return;
      const target = preflightTargets.value.find((item) => item.key === key);
      if (target && target.ready === false) {
        globalError.value = "该 source 当前不可用，请先装载耗材或修正 source 配置。";
        return;
      }
      manualTargets[selectedTool.value] = key;
      if (preflight.validation?.ok === true) {
        preflight.validation = {
          ok: false,
          errors: ["耗材映射已变更，请点击“应用映射”重新生成 route plan。"],
        };
      }
    }
    function targetCardForTool(tool) {
      const key = manualTargets[String(tool)];
      return preflightTargets.value.find((item) => item.key === key) || null;
    }
    async function remapRoutePlan() {
      if (!preflight.report?.token) return;
      const missing = usedTools.value
        .map((tool) => String(tool.t))
        .filter((tool) => !targetForKey(manualTargets[tool]));
      if (missing.length) {
        preflight.validation = {
          ok: false,
          errors: [`需要手动映射 ${missing.map((tool) => `T${displayIndex(tool)}`).join(", ")}`],
        };
        globalError.value = "";
        return;
      }
      busy.remap = true;
      globalError.value = "";
      try {
        const toolTargets = {};
        for (const tool of usedTools.value) {
          const target = targetForKey(manualTargets[String(tool.t)]);
          if (target) toolTargets[String(tool.t)] = target;
        }
        const payload = await api("/route-plan/remap", {
          method: "POST",
          body: { token: preflight.report.token, tool_targets: toolTargets },
        });
        preflight.report = { ...preflight.report, ...payload, resolve_errors: [] };
        const targets = preflight.report.tool_targets || {};
        for (const tool of usedTools.value) {
          manualTargets[String(tool.t)] = targetKey(targets[String(tool.t)] || toolTargets[String(tool.t)] || {});
        }
        await autoValidateRoutePlan();
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.remap = false;
      }
    }
    async function autoValidateRoutePlan() {
      if (!preflight.report?.token) return;
      if ((preflight.report?.resolve_errors || []).length || !preflight.report?.route_plan) {
        preflight.validation = {
          ok: false,
          errors: ["耗材映射未完成，请先完成映射并应用。"],
        };
        return;
      }
      busy.validate = true;
      try {
        preflight.validation = await api(`/preflight/route-plan/validate?token=${encodeURIComponent(preflight.report.token)}`);
      } catch (error) {
        preflight.validation = { ok: false, errors: [error.message || String(error)] };
      } finally {
        busy.validate = false;
      }
    }
    function checkClass(ok) { return ok ? "check-card ok" : "check-card bad"; }
    async function printRoutePlan() {
      if (!canPrintRoutePlan.value) {
        globalError.value = "Route plan 尚未通过自动校验，或仍存在 source state/mapping 问题。";
        return;
      }
      busy.print = true;
      try {
        const job = await api("/route-plan/print", {
          method: "POST",
          body: { token: preflight.report.token },
        });
        preflight.job = job;
        pollPrintJob(job.job_id);
      } catch (error) {
        globalError.value = error.message || String(error);
        busy.print = false;
      }
    }
    async function pollPrintJob(jobId) {
      let failures = 0;
      try {
        while (jobId) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          let job = null;
          try {
            job = await api(`/preflight/print/status?job_id=${encodeURIComponent(jobId)}`);
            failures = 0;
          } catch (pollError) {
            failures += 1;
            preflight.job = {
              ...(preflight.job || {}),
              job_id: jobId,
              stage: preflight.job?.stage || "status_retry",
              error: `状态连接重试中 (${failures}/5): ${pollError.message || pollError}`,
              done: false,
            };
            if (failures < 5) continue;
            throw pollError;
          }
          preflight.job = job;
          if (job.done) {
            if (job.error) globalError.value = job.error;
            else setToast("打印任务已发送到 Moonraker");
            await loadRoutePlanHistory();
            break;
          }
        }
      } catch (error) {
        globalError.value = error.message || String(error);
      } finally {
        busy.print = false;
      }
    }

    onMounted(async () => {
      await loadVersion();
      await loadMaterials();
      await refreshAll();
      await Promise.allSettled([loadConfig(), refreshDebugState()]);
      await loadRoutePlanHistory();
      startScreenPolling();
      setInterval(() => {
        if (!busy.refresh) Promise.allSettled([reloadState(), reloadSourceState(), reloadOperation()]);
      }, 3000);
      setInterval(() => { if (camera.mode === "image") camera.nonce = Date.now(); }, 5000);
    });

    return {
      tab, configTab, setTab,
      busy, globalError, toast,
      versionLabel, printerName, state, sourceGraph, sourceGraphMeta, sourceState,
      graphJson, selectedHead, sourceFilter,
      connectionState, connectionText,
      aces, graphHeads, graphSources, selectedHeadLabel, edgesForSelectedHead,
      headCards, sourceAlerts, filteredSources,
      refreshAll, reloadSourceGraph, displayIndex, displayAce, shortHash,
      swatchStyle, ringStyle, dryerLabel, pretty,
      formatBytes, formatHistoryTime, historySummary,
      sourceOptionsForHead, edgeEnabled, edgePriority, setEdgeEnabled, setEdgePriority,
      setHeadSourceBinding, bindingLabel,
      sourceExecutionValue, setSourceExecutionValue,
      sourceExecutionFields,
      syncGraphJson, applyGraphJson, saveSourceGraph,
      currentOperation, operationBusy, operationLabel, reloadOperation,
      confirmUnloadAll,
      dryerCfg, dryStart, dryStop,
      headPanel, activeHeadCard, allowedSourcesForHead,
      openHeadPanel, closeHeadPanel, runHeadLoadNow, runHeadUnloadNow, runSourceFullUnloadNow,
      humanPlanSummary,
      recovery, openRecovery,
      runHeadRecoverNow,
      materialEditor, materialOptions,
      openMaterialEditor, closeMaterialEditor, readMaterialFromRfid, saveMaterialEditor,
      snapshots, selectedSnapshot, saveSnapshot, applySnapshot, deleteSnapshot,
      screenAvailable, screenCanvas, floatScreenCanvas, screenPopout, screenFps,
      screenDown, screenMove, screenUp, toggleScreenPopout,
      camera, cameraUrl, saveCameraConfig,
      config, configForm, loadConfig, saveConfig, saveRawConfig,
      debugState, updateState, debugEnable, debugDisable, updateCheck, updateApply,
      profileCommandPreview,
      fileInput, gcodeCanvas, gcodePreview, gcodeBoundsLabel,
      preflight, preflightHistory, manualTargets, selectedTool, usedTools, preflightTargets,
      preflightErrors, manualMappingTools, preflightStatus, mappingCheckLabel,
      canValidateRoutePlan, canApplyMapping,
      canPrintRoutePlan, swapSummary, routePlanSummary,
      handleFileInput, handleDrop, autoValidateRoutePlan, remapRoutePlan,
      loadRoutePlanHistory, restoreRoutePlanHistoryFromClick,
      checkClass, mappingLabel, mappingCommands, assignSelectedTool,
      targetCardForTool, sourceCardLabel, sourceStatusLabel,
      sourceReadyLabel, sourceStatusClass, headConfidenceLabel,
      sourceRuntimeDetail,
      targetRouteLabel, printRoutePlan,
    };
  },
}).mount("#app");
