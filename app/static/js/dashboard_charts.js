document.addEventListener("DOMContentLoaded", function () {
  const node = document.getElementById("dash-charts");
  if (!node) {
    return;
  }
  const palette = {
    green: "#0e6b4f",
    copper: "#b86a32",
    gold: "#d9a441",
    red: "#b23a32",
    slate: "#8a979f"
  };
  const charts = JSON.parse(node.textContent);

  function showEmpty(id) {
    const canvas = document.getElementById(id);
    const empty = document.getElementById(id + "-empty");
    if (canvas) {
      const box = canvas.closest(".chart-box");
      (box || canvas).hidden = true;
    }
    if (empty) {
      empty.hidden = false;
    }
  }

  charts.forEach(function (spec) {
    const canvas = document.getElementById(spec.id);
    if (!canvas) {
      return;
    }
    const hasData = spec.labels.length && spec.datasets.some(function (set) {
      return set.data.some(function (value) { return value > 0; });
    });
    if (!hasData || typeof Chart === "undefined") {
      showEmpty(spec.id);
      return;
    }
    const round = spec.type === "doughnut";
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
            backgroundColor: colors,
            borderColor: round ? "#fff" : colors,
            borderWidth: round ? 2 : 0,
            borderRadius: round ? 0 : 6,
            maxBarThickness: 38
          };
        })
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: spec.horizontal ? "y" : "x",
        plugins: {
          legend: { display: round || spec.datasets.length > 1, position: "bottom" }
        },
        scales: round ? {} : {
          x: { grid: { display: false }, ticks: { precision: 0 } },
          y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "rgba(0,0,0,.05)" } }
        }
      }
    });
  });
});
