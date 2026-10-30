document.addEventListener("DOMContentLoaded", function () {
  const palette = {
    green: "#0e6b4f",
    copper: "#b86a32",
    gold: "#d9a441",
    red: "#b23a32",
    slate: "#8a979f"
  };
  const kwacha = new Intl.NumberFormat("en-ZM", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const compact = new Intl.NumberFormat("en-ZM", { notation: "compact", maximumFractionDigits: 1 });

  function money(value) {
    return (value < 0 ? "-K" : "K") + kwacha.format(Math.abs(value));
  }

  function setState(id, state) {
    const canvas = document.getElementById(id);
    if (!canvas) {
      return;
    }
    const box = canvas.closest("[data-chart-box]");
    const loading = box && box.querySelector("[data-chart-loading]");
    if (loading) {
      loading.hidden = true;
    }
    if (state === "drawn") {
      return;
    }
    if (box) {
      box.hidden = true;
    }
    const target = document.getElementById(id + (state === "error" ? "-error" : "-empty"));
    if (target) {
      target.hidden = false;
    }
  }

  function draw(spec) {
    const canvas = document.getElementById(spec.id);
    if (!canvas) {
      return;
    }
    const hasData = spec.labels.length && spec.datasets.some(function (set) {
      return set.data.some(function (value) { return value !== 0; });
    });
    if (!hasData) {
      setState(spec.id, "empty");
      return;
    }
    if (typeof Chart === "undefined") {
      setState(spec.id, "error");
      return;
    }
    const round = spec.type === "doughnut";
    const line = spec.type === "line";
    const valueAxis = spec.horizontal ? "x" : "y";
    const labelAxis = spec.horizontal ? "y" : "x";
    const scales = {};
    if (!round) {
      scales[labelAxis] = { grid: { display: false }, ticks: { autoSkip: true, maxRotation: 0 } };
      scales[valueAxis] = {
        beginAtZero: true,
        grid: { color: "rgba(0,0,0,.05)" },
        ticks: spec.money
          ? { callback: function (value) { return "K" + compact.format(value); } }
          : { precision: 0 }
      };
      if (spec.horizontal) {
        scales.y.ticks.callback = function (value) {
          const label = this.getLabelForValue(value);
          return label.length > 22 ? label.slice(0, 21) + "…" : label;
        };
      }
    }
    try {
      new Chart(canvas, {
        type: spec.type,
        data: {
          labels: spec.labels,
          datasets: spec.datasets.map(function (set) {
            const colors = set.colors
              ? set.colors.map(function (name) { return palette[name] || name; })
              : palette[set.color] || palette.green;
            return {
              label: set.label,
              data: set.data,
              backgroundColor: line ? colors + "22" : colors,
              borderColor: round ? "#fff" : colors,
              borderWidth: round ? 2 : line ? 2.5 : 0,
              borderRadius: round || line ? 0 : 6,
              maxBarThickness: 38,
              tension: 0.3,
              fill: line,
              pointRadius: line && spec.labels.length > 40 ? 0 : 3
            };
          })
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          indexAxis: spec.horizontal ? "y" : "x",
          interaction: { mode: round ? "nearest" : "index", intersect: round },
          plugins: {
            legend: { display: round || spec.datasets.length > 1, position: "bottom" },
            tooltip: {
              callbacks: {
                label: function (context) {
                  const raw = round ? context.parsed : context.parsed[valueAxis];
                  return " " + context.dataset.label + ": " + (spec.money ? money(raw) : raw);
                }
              }
            }
          },
          scales: scales
        }
      });
      setState(spec.id, "drawn");
    } catch (error) {
      setState(spec.id, "error");
    }
  }

  const node = document.getElementById("report-charts-data");
  if (node) {
    let charts = [];
    try {
      charts = JSON.parse(node.textContent);
    } catch (error) {
      charts = [];
    }
    charts.forEach(draw);
  }

  document.querySelectorAll("[data-report-filters]").forEach(function (form) {
    const period = form.querySelector("[data-period-select]");
    const custom = form.querySelector("[data-custom-range]");
    function syncCustom() {
      if (!period || !custom) {
        return;
      }
      const show = period.value === "custom";
      custom.hidden = !show;
      custom.querySelectorAll("input").forEach(function (input) { input.disabled = !show; });
    }
    if (period) {
      period.addEventListener("change", syncCustom);
      syncCustom();
    }
  });

  document.querySelectorAll("[data-report-filters], [data-compare-form]").forEach(function (form) {
    form.addEventListener("submit", function () {
      const button = form.querySelector("[data-report-apply]");
      if (button) {
        button.disabled = true;
        button.insertAdjacentHTML("afterbegin", '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>');
      }
      document.querySelectorAll("[data-report-body]").forEach(function (body) {
        body.classList.add("is-loading");
        body.setAttribute("aria-busy", "true");
      });
    });
  });

  document.querySelectorAll("[data-planner-jump]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.value) {
        window.location.href = select.value;
      }
    });
  });

  window.addEventListener("pageshow", function () {
    document.querySelectorAll("[data-report-body].is-loading").forEach(function (body) {
      body.classList.remove("is-loading");
      body.removeAttribute("aria-busy");
    });
    document.querySelectorAll("[data-report-apply]:disabled").forEach(function (button) {
      button.disabled = false;
      const spinner = button.querySelector(".spinner-border");
      if (spinner) {
        spinner.remove();
      }
    });
  });
});
