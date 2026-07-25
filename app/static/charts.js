/* global Chart */
(function () {
  "use strict";

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || fallback;
  }

  function theme() {
    const accent = cssVar("--accent", "#0f6b5c");
    const ink = cssVar("--ink", "#1c2421");
    const muted = cssVar("--muted", "#5c675f");
    const line = cssVar("--line", "#c9c0b0");
    const danger = cssVar("--danger", "#8b1e1e");
    const warn = cssVar("--warn", "#8a3b12");
    const ok = cssVar("--ok", "#1f6b3a");
    const body =
      cssVar("--font-body", '"Source Sans 3", "Segoe UI", sans-serif');
    return {
      accent,
      accentSoft: hexToRgba(accent, 0.22),
      ink,
      muted,
      line,
      danger,
      warn,
      ok,
      body,
      grid: hexToRgba(line, 0.65),
    };
  }

  function hexToRgba(hex, alpha) {
    const raw = hex.replace("#", "");
    if (raw.length !== 6) {
      return hex;
    }
    const r = parseInt(raw.slice(0, 2), 16);
    const g = parseInt(raw.slice(2, 4), 16);
    const b = parseInt(raw.slice(4, 6), 16);
    return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
  }

  function moneyTick(value) {
    if (value == null || Number.isNaN(value)) {
      return "";
    }
    const abs = Math.abs(value);
    if (abs >= 1000) {
      return "£" + (value / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    }
    return "£" + Number(value).toFixed(0);
  }

  function moneyTooltip(context) {
    const label = context.dataset.label ? context.dataset.label + ": " : "";
    const value = context.parsed.y != null ? context.parsed.y : context.parsed.x;
    if (value == null) {
      return label;
    }
    return (
      label +
      "£" +
      Number(value).toLocaleString("en-GB", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })
    );
  }

  function baseOptions(t) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600, easing: "easeOutQuart" },
      plugins: {
        legend: {
          display: false,
          labels: {
            color: t.muted,
            font: { family: t.body, size: 12 },
          },
        },
        tooltip: {
          backgroundColor: "rgba(28, 36, 33, 0.92)",
          titleFont: { family: t.body, size: 13 },
          bodyFont: { family: t.body, size: 13 },
          padding: 10,
          cornerRadius: 4,
          callbacks: { label: moneyTooltip },
        },
      },
      scales: {
        x: {
          grid: { color: t.grid, drawBorder: false },
          ticks: { color: t.muted, font: { family: t.body, size: 11 } },
        },
        y: {
          grid: { color: t.grid, drawBorder: false },
          ticks: {
            color: t.muted,
            font: { family: t.body, size: 11 },
            callback: moneyTick,
          },
        },
      },
    };
  }

  function readData(el) {
    const id = el.getAttribute("data-chart-data");
    if (!id) {
      return null;
    }
    const node = document.getElementById(id);
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent || "null");
    } catch (err) {
      console.warn("FinanceCharts: bad JSON for", id, err);
      return null;
    }
  }

  function paceCompare(canvas, data, t) {
    const avg = Number(data.avg_daily) || 0;
    const safe = Number(data.safe_daily) || 0;
    const over = avg > safe && safe > 0;
    const barColor = over ? t.warn : t.accent;
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels: ["Your pace", "Safe daily"],
        datasets: [
          {
            data: [avg, safe],
            backgroundColor: [barColor, hexToRgba(t.accent, 0.35)],
            borderRadius: 4,
            maxBarThickness: 48,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        indexAxis: "y",
        scales: {
          x: {
            ...baseOptions(t).scales.x,
            beginAtZero: true,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback: moneyTick,
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            grid: { display: false },
            ticks: { color: t.ink, font: { family: t.body, size: 12 } },
          },
        },
      },
    });
  }

  function periodSeries(canvas, data, t) {
    const labels = data.labels || [];
    const values = (data.values || []).map(Number);
    return new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "28-day pace",
            data: values,
            borderColor: t.accent,
            backgroundColor: t.accentSoft,
            fill: true,
            tension: 0.35,
            pointRadius: 4,
            pointBackgroundColor: t.accent,
            pointBorderColor: "#fffdf8",
            pointBorderWidth: 2,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        plugins: {
          ...baseOptions(t).plugins,
          legend: { display: false },
        },
        scales: {
          ...baseOptions(t).scales,
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: true,
          },
        },
      },
    });
  }

  function horizontalBars(canvas, data, t) {
    const labels = data.labels || [];
    const values = (data.values || []).map(Number);
    const overs = data.over || [];
    const colors = values.map((_, i) =>
      overs[i] ? t.danger : hexToRgba(t.accent, 0.75)
    );
    const limits = data.limits;
    // Grow frame so Chart.js does not auto-skip category labels.
    const frame = canvas.closest(".chart-frame");
    if (frame && labels.length) {
      frame.style.height = Math.max(220, labels.length * 38 + 48) + "px";
    }
    const datasets = [
      {
        label: data.value_label || "Spent",
        data: values,
        backgroundColor: colors,
        borderRadius: 3,
        maxBarThickness: 22,
      },
    ];
    if (limits && limits.length) {
      datasets.push({
        label: "Limit",
        data: limits.map(Number),
        backgroundColor: hexToRgba(t.line, 0.55),
        borderRadius: 3,
        maxBarThickness: 22,
      });
    }
    return new Chart(canvas, {
      type: "bar",
      data: { labels, datasets },
      options: {
        ...baseOptions(t),
        indexAxis: "y",
        plugins: {
          ...baseOptions(t).plugins,
          legend: {
            display: Boolean(limits && limits.length),
            position: "bottom",
            labels: {
              color: t.muted,
              font: { family: t.body, size: 12 },
              boxWidth: 12,
            },
          },
        },
        scales: {
          x: {
            ...baseOptions(t).scales.x,
            beginAtZero: true,
            ticks: {
              ...baseOptions(t).scales.x.ticks,
              callback: moneyTick,
            },
          },
          y: {
            ...baseOptions(t).scales.y,
            grid: { display: false },
            ticks: {
              color: t.ink,
              font: { family: t.body, size: 12 },
              autoSkip: false,
              maxRotation: 0,
              minRotation: 0,
            },
          },
        },
      },
    });
  }

  function balanceRunway(canvas, data, t) {
    const labels = data.labels || [];
    const values = (data.values || []).map(Number);
    const markers = data.markers || [];
    return new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Projected balance",
            data: values,
            borderColor: t.accent,
            backgroundColor: t.accentSoft,
            fill: true,
            tension: 0.2,
            pointRadius: labels.map((_, i) => (markers[i] ? 5 : 0)),
            pointBackgroundColor: labels.map((_, i) =>
              markers[i] === "payday" ? t.ok : markers[i] ? t.warn : t.accent
            ),
            pointBorderColor: "#fffdf8",
            pointBorderWidth: 2,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        plugins: {
          ...baseOptions(t).plugins,
          tooltip: {
            ...baseOptions(t).plugins.tooltip,
            callbacks: {
              label: moneyTooltip,
              afterBody: function (items) {
                const idx = items[0] && items[0].dataIndex;
                const note = data.notes && data.notes[idx];
                return note ? [note] : [];
              },
            },
          },
        },
      },
    });
  }

  function monthlyTrend(canvas, data, t) {
    // Expect chronological labels (oldest → newest) for left-to-right reading.
    const labels = data.labels || [];
    const values = (data.values || []).map(Number);
    return new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [
          {
            label: "Spent",
            data: values,
            backgroundColor: hexToRgba(t.accent, 0.7),
            borderRadius: 4,
            maxBarThickness: 36,
          },
        ],
      },
      options: {
        ...baseOptions(t),
        scales: {
          ...baseOptions(t).scales,
          y: {
            ...baseOptions(t).scales.y,
            beginAtZero: true,
          },
        },
      },
    });
  }

  const builders = {
    paceCompare,
    periodSeries,
    horizontalBars,
    balanceRunway,
    monthlyTrend,
  };

  function initAll() {
    if (typeof Chart === "undefined") {
      return;
    }
    Chart.defaults.font.family = theme().body;
    const t = theme();
    document.querySelectorAll("[data-chart]").forEach(function (el) {
      const kind = el.getAttribute("data-chart");
      const builder = builders[kind];
      if (!builder) {
        return;
      }
      const data = readData(el);
      if (!data) {
        return;
      }
      const canvas = el.querySelector("canvas");
      if (!canvas) {
        return;
      }
      builder(canvas, data, t);
    });
  }

  window.FinanceCharts = { initAll, theme };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})();
