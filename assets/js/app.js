import Alpine from "alpinejs";
import * as echarts from "echarts/core";
import {
  BarChart,
  GaugeChart,
  GraphChart,
  HeatmapChart,
  LineChart,
  PieChart,
  SankeyChart,
  ScatterChart,
} from "echarts/charts";
import {
  DatasetComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  GaugeChart,
  GraphChart,
  HeatmapChart,
  LineChart,
  PieChart,
  SankeyChart,
  ScatterChart,
  DatasetComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

window.Alpine = Alpine;
Alpine.start();

const root = document.documentElement;
const body = document.body;
const charts = new Map();

function preferredTheme() {
  return localStorage.getItem("atlas-theme") ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

function applyTheme(theme, persist = true) {
  root.dataset.theme = theme;
  if (persist) localStorage.setItem("atlas-theme", theme);
  document.querySelectorAll("[data-theme-label]").forEach((node) => {
    node.textContent = theme === "dark" ? "切换浅色" : "切换深色";
  });
  document.querySelectorAll("[data-theme-icon]").forEach((node) => {
    node.textContent = theme === "dark" ? "☼" : "◐";
  });
  window.setTimeout(() => refreshCharts(), 20);
}

applyTheme(root.dataset.theme || preferredTheme(), false);

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  button.addEventListener("click", () => applyTheme(root.dataset.theme === "dark" ? "light" : "dark"));
});

function setNav(open) {
  body.classList.toggle("nav-open", open);
  document.querySelectorAll("[data-nav-toggle]").forEach((button) => button.setAttribute("aria-expanded", String(open)));
  const backdrop = document.querySelector("[data-nav-backdrop]");
  if (backdrop) backdrop.hidden = !open;
}

document.querySelectorAll("[data-nav-toggle]").forEach((button) => {
  button.addEventListener("click", () => setNav(!body.classList.contains("nav-open")));
});
document.querySelector("[data-nav-backdrop]")?.addEventListener("click", () => setNav(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setNav(false);
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    document.querySelector("[data-site-search]")?.focus();
  }
});

const etClock = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "America/New_York",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});
function updateClock() {
  document.querySelectorAll("[data-et-clock]").forEach((node) => {
    node.textContent = `${etClock.format(new Date())} ET`;
  });
}
updateClock();
window.setInterval(updateClock, 1000);

function css(name) {
  return getComputedStyle(root).getPropertyValue(name).trim();
}

function parseSpec(element) {
  const sourceId = element.dataset.chartSource;
  if (!sourceId) return null;
  const source = document.getElementById(sourceId);
  if (!source) return null;
  try {
    return JSON.parse(source.textContent || "null");
  } catch (error) {
    console.warn(`Atlas chart data '${sourceId}' is not valid JSON`, error);
    return null;
  }
}

function baseOption() {
  const muted = css("--muted");
  const line = css("--line");
  return {
    animationDuration: 420,
    color: [css("--accent"), css("--cyan"), css("--warning"), css("--negative"), "#7c8df1"],
    textStyle: { color: css("--ink"), fontFamily: css("--sans") },
    tooltip: {
      trigger: "axis",
      backgroundColor: css("--surface"),
      borderColor: css("--line-strong"),
      textStyle: { color: css("--ink"), fontSize: 11 },
      extraCssText: "box-shadow: 0 12px 35px rgba(0,0,0,.18); border-radius: 8px;",
    },
    legend: { top: 0, right: 0, textStyle: { color: muted, fontSize: 10 } },
    grid: { left: 10, right: 14, top: 40, bottom: 15, containLabel: true },
    xAxis: {
      type: "category",
      boundaryGap: false,
      axisLine: { lineStyle: { color: line } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10 },
      splitLine: { lineStyle: { color: line, type: "dashed" } },
    },
  };
}

function rowsToSeries(raw) {
  if (!Array.isArray(raw)) return { labels: [], series: [] };
  if (raw.every((row) => typeof row === "number" || typeof row === "string")) {
    return {
      labels: raw.map((_, index) => index + 1),
      series: [{
        name: "数值",
        data: raw.map((value) => {
          if (value === null || value === undefined || value === "") return null;
          const numeric = Number(value);
          return Number.isFinite(numeric) ? numeric : null;
        }),
      }],
    };
  }
  const labels = raw.map((row, index) => row.label ?? row.date ?? row.name ?? index + 1);
  const keys = [...new Set(raw.flatMap((row) => Object.keys(row || {})))].filter(
    (key) => !["label", "date", "name"].includes(key) && !key.startsWith("_"),
  );
  return {
    labels,
    series: keys.map((key) => ({
      name: key,
      data: raw.map((row) => {
        const value = row?.[key];
        if (value === null || value === undefined || value === "") return null;
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric : null;
      }),
    })),
  };
}

function chartOption(kind, raw) {
  if (raw && !Array.isArray(raw) && raw.series && (raw.xAxis || raw.dataset || raw.options)) {
    return { ...baseOption(), ...(raw.options || raw) };
  }
  const normalized = Array.isArray(raw) ? rowsToSeries(raw) : {
    labels: raw?.labels || raw?.dates || [],
    series: raw?.series || (raw?.values ? [{ name: raw.name || "数值", data: raw.values }] : []),
  };
  const option = baseOption();
  option.xAxis.data = normalized.labels;
  option.series = normalized.series.map((series, index) => ({
    type: kind === "bar" ? "bar" : kind === "scatter" ? "scatter" : "line",
    showSymbol: kind === "scatter",
    symbolSize: 6,
    smooth: kind === "line",
    lineStyle: { width: 1.8 },
    areaStyle: kind === "area" && index === 0 ? { opacity: .09 } : undefined,
    barMaxWidth: 26,
    emphasis: { focus: "series" },
    ...series,
  }));
  if (kind === "bar") option.xAxis.boundaryGap = true;
  if (kind === "pie") {
    delete option.xAxis;
    delete option.yAxis;
    delete option.grid;
    option.tooltip.trigger = "item";
    option.series = [{
      type: "pie",
      radius: ["52%", "78%"],
      label: { color: css("--muted"), fontSize: 10 },
      data: normalized.labels.map((name, index) => ({ name, value: normalized.series[0]?.data[index] || 0 })),
    }];
  }
  if (kind === "gauge") {
    delete option.xAxis;
    delete option.yAxis;
    delete option.grid;
    option.series = [{
      type: "gauge", startAngle: 205, endAngle: -25, min: raw?.min || 0, max: raw?.max || 100,
      progress: { show: true, width: 12 }, axisLine: { lineStyle: { width: 12, color: [[1, css("--surface-3")]] } },
      axisTick: { show: false }, splitLine: { show: false }, axisLabel: { color: css("--muted"), distance: -34, fontSize: 9 },
      pointer: { width: 4 }, anchor: { show: true, size: 8 },
      title: { color: css("--muted"), offsetCenter: [0, "62%"], fontSize: 11 },
      detail: { valueAnimation: true, color: css("--ink"), fontSize: 28, fontFamily: css("--mono"), offsetCenter: [0, "20%"] },
      data: [{ value: raw?.value || 0, name: raw?.name || "Score" }],
    }];
  }
  if (kind === "graph" && raw?.nodes) {
    delete option.xAxis;
    delete option.yAxis;
    delete option.grid;
    option.tooltip.trigger = "item";
    option.series = [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 150, edgeLength: [55, 130], gravity: .08 },
      label: { show: true, color: css("--ink"), fontSize: 9 },
      edgeLabel: { show: false }, lineStyle: { color: css("--line-strong"), curveness: .12 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2 } },
      data: raw.nodes, links: raw.links || raw.edges || [], categories: raw.categories || [],
    }];
  }
  if (kind === "heatmap" && raw?.values) {
    option.tooltip.trigger = "item";
    option.xAxis.data = raw.x || raw.labels || [];
    option.yAxis = { type: "category", data: raw.y || [], axisLabel: { color: css("--muted"), fontSize: 10 }, axisLine: { lineStyle: { color: css("--line") } } };
    option.visualMap = { min: raw.min ?? -1, max: raw.max ?? 1, calculable: false, orient: "horizontal", left: "center", bottom: 0, inRange: { color: [css("--negative"), css("--surface-2"), css("--positive")] }, textStyle: { color: css("--muted"), fontSize: 9 } };
    option.grid.bottom = 45;
    option.series = [{ type: "heatmap", data: raw.values, label: { show: true, color: css("--ink"), fontSize: 9 } }];
  }
  return option;
}

function initializeChart(element) {
  if (charts.has(element)) return;
  const raw = parseSpec(element);
  if (!raw || (Array.isArray(raw) && raw.length === 0)) {
    element.classList.add("chart-empty");
    element.textContent = element.dataset.emptyLabel || "等待可用数据";
    return;
  }
  const instance = echarts.init(element, root.dataset.theme === "dark" ? "dark" : null, { renderer: "canvas" });
  instance.setOption(chartOption(element.dataset.chartKind || "line", raw), true);
  charts.set(element, { instance, raw, kind: element.dataset.chartKind || "line" });
}

function refreshCharts() {
  charts.forEach(({ instance, raw, kind }) => instance.setOption(chartOption(kind, raw), true));
}

const chartObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) {
      initializeChart(entry.target);
      chartObserver.unobserve(entry.target);
    }
  });
}, { rootMargin: "180px" }) : null;

document.querySelectorAll("[data-chart]").forEach((element) => chartObserver ? chartObserver.observe(element) : initializeChart(element));

const resizeObserver = "ResizeObserver" in window ? new ResizeObserver(() => charts.forEach(({ instance }) => instance.resize())) : null;
if (resizeObserver) document.querySelectorAll("[data-chart]").forEach((element) => resizeObserver.observe(element));
window.addEventListener("resize", () => charts.forEach(({ instance }) => instance.resize()));

document.querySelectorAll("[data-copy-link]").forEach((button) => {
  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(window.location.href);
    const original = button.textContent;
    button.textContent = "已复制";
    window.setTimeout(() => { button.textContent = original; }, 1500);
  });
});

document.querySelectorAll("[data-tab-target]").forEach((tab) => {
  tab.addEventListener("click", () => {
    const group = tab.closest("[data-tabs]");
    if (!group) return;
    const target = tab.dataset.tabTarget;
    group.querySelectorAll("[data-tab-target]").forEach((node) => node.setAttribute("aria-selected", String(node === tab)));
    group.querySelectorAll("[data-tab-panel]").forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== target; });
    window.setTimeout(() => charts.forEach(({ instance }) => instance.resize()), 0);
  });
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {}));
}
