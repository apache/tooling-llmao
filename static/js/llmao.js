/* llmao front-end hooks */
document.addEventListener("DOMContentLoaded", function () {
  var revokeModal = document.getElementById("revokeModal");
  if (revokeModal) {
    revokeModal.addEventListener("show.bs.modal", function (event) {
      var btn = event.relatedTarget;
      if (!btn) return;
      var tokenId = btn.getAttribute("data-token-id") || "";
      var purpose = btn.getAttribute("data-purpose") || "";
      var afterPath = btn.getAttribute("data-after-path") || "/keys";
      var tokenInput = document.getElementById("revokeTokenId");
      var purposeEl = document.getElementById("revokePurpose");
      var afterInput = document.getElementById("revokeAfterPath");
      if (tokenInput) tokenInput.value = tokenId;
      if (purposeEl) purposeEl.textContent = purpose;
      if (afterInput) afterInput.value = afterPath;
    });
  }

  var modelModal = document.getElementById("modelDetailModal");
  if (modelModal) {
    modelModal.addEventListener("show.bs.modal", function (event) {
      var btn = event.relatedTarget;
      if (!btn) return;
      function set(id, val) {
        var el = document.getElementById(id);
        if (el) el.textContent = val || "—";
      }
      set("mdDisplayName", btn.getAttribute("data-display-name"));
      set("mdModelName", btn.getAttribute("data-model-name"));
      set("mdHosting", btn.getAttribute("data-hosting"));
      var selfCount = btn.getAttribute("data-self-count") || "0";
      var externalCount = btn.getAttribute("data-external-count") || "0";
      set("mdMix", selfCount + " self-hosted, " + externalCount + " external");
      set("mdContext", btn.getAttribute("data-context"));
      set("mdLicense", btn.getAttribute("data-license"));
      set("mdModality", btn.getAttribute("data-modality"));
      set("mdOpenness", btn.getAttribute("data-openness"));
      function truthy(v) {
        return v === "True" || v === "true" || v === "1" || v === "yes";
      }
      var thinking = truthy(btn.getAttribute("data-thinking"));
      var thinksDefault = truthy(btn.getAttribute("data-thinks-default"));
      var thinkText = thinking
        ? (thinksDefault ? "Supported (on by default)" : "Supported (off by default)")
        : "Not advertised";
      set("mdThinking", thinkText);
      set("mdNotes", btn.getAttribute("data-notes"));

      var supply = document.getElementById("mdSupplyBlock");
      var reveal = truthy(btn.getAttribute("data-reveal-supply"));
      if (supply) {
        if (reveal) {
          supply.classList.remove("d-none");
          set("mdWeights", btn.getAttribute("data-weights"));
          set("mdTraining", btn.getAttribute("data-training"));
          set("mdProvenance", btn.getAttribute("data-provenance"));
        } else {
          supply.classList.add("d-none");
        }
      }
    });
  }

  document.querySelectorAll(".js-copy-model").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy") || "";
      if (!text || !navigator.clipboard) return;
      var icon = btn.querySelector(".bi");
      var label = btn.getAttribute("title") || "";
      navigator.clipboard.writeText(text).then(function () {
        if (icon) icon.className = "bi bi-check-lg";
        btn.classList.add("model-copy-done");
        btn.setAttribute("title", "Copied");
        btn.setAttribute("aria-label", "Copied");
        setTimeout(function () {
          if (icon) icon.className = "bi bi-clipboard";
          btn.classList.remove("model-copy-done");
          btn.setAttribute("title", label);
          btn.setAttribute("aria-label", label);
        }, 1500);
      });
    });
  });

  var approveModal = document.getElementById("approveBoxModal");
  if (approveModal) {
    approveModal.addEventListener("show.bs.modal", function (event) {
      var btn = event.relatedTarget;
      if (!btn) return;
      var host = btn.getAttribute("data-host") || "";
      document.getElementById("approve-host").value = host;
      document.getElementById("approveBoxModalLabel").textContent = "Approve " + host;
      approveModal.querySelectorAll("input[name=model]").forEach(function (cb) {
        cb.checked = false;
      });
    });
  }

  // An attention item links to its row in Deployments. Flash the row on arrival.
  function flashDeployment(hash) {
    if (!hash || hash.indexOf("#dep-") !== 0) return;
    var row = document.getElementById(hash.slice(1));
    if (!row) return;
    row.classList.remove("dep-flash");
    void row.offsetWidth;
    row.classList.add("dep-flash");
  }
  document.querySelectorAll(".js-dep-link").forEach(function (link) {
    link.addEventListener("click", function () {
      flashDeployment(link.getAttribute("href"));
    });
  });
  flashDeployment(window.location.hash);

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  // Capacity: one bar per day of the period, drawn by ECharts from the JSON
  // on each element: {days: ["2026-10-19", ...], values: [...], unit: "tokens"}.
  // Every day of the period is on the axis, a day with nothing as zero, so a
  // series that stopped reads as having stopped.
  if (window.echarts) {
    var style = getComputedStyle(document.documentElement);
    var bar = style.getPropertyValue("--bs-primary").trim() || "#0d6efd";
    var ink = style.getPropertyValue("--bs-secondary-color").trim() || "#6c757d";
    var hairline = style.getPropertyValue("--bs-border-color").trim() || "#dee2e6";
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    function dayLabel(iso) {
      return months[parseInt(iso.slice(5, 7), 10) - 1] + " " + parseInt(iso.slice(8, 10), 10);
    }
    // The axis wants a short figure; the tooltip the whole one.
    function compact(unit) {
      return function (v) {
        if (unit === "usd") return "$" + (v >= 100 ? Math.round(v) : v);
        if (v >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
        if (v >= 1e3) return Math.floor(v / 1e3) + "k";
        return String(v);
      };
    }
    function whole(unit, v) {
      if (unit === "usd") return "$" + v.toFixed(2);
      return v.toLocaleString() + " " + unit;
    }
    var charts = [];
    document.querySelectorAll(".cap-chart[data-chart]").forEach(function (el) {
      var spec = JSON.parse(el.getAttribute("data-chart"));
      var chart = echarts.init(el, null, { renderer: "svg" });
      chart.setOption({
        color: [bar],
        grid: { left: 4, right: 4, top: 8, bottom: 4, containLabel: true },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          formatter: function (params) {
            var p = params[0];
            return dayLabel(p.name) + ": " + whole(spec.unit, p.value);
          },
        },
        xAxis: {
          type: "category",
          data: spec.days,
          axisTick: { show: false },
          axisLine: { lineStyle: { color: hairline } },
          axisLabel: { color: ink, formatter: dayLabel },
        },
        yAxis: {
          type: "value",
          minInterval: spec.unit === "usd" ? 0 : 1,
          splitLine: { lineStyle: { color: hairline } },
          axisLabel: { color: ink, formatter: compact(spec.unit) },
        },
        series: [
          {
            type: "bar",
            data: spec.values,
            barMaxWidth: 24,
            barCategoryGap: "20%",
            itemStyle: { borderRadius: [4, 4, 0, 0] },
          },
        ],
      });
      charts.push(chart);
    });
    window.addEventListener("resize", function () {
      charts.forEach(function (c) {
        c.resize();
      });
    });
  }

  var copyBtn = document.querySelector(".js-copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", function () {
      var field = document.querySelector(".js-secret-field");
      var icon = document.querySelector(".js-copy-icon");
      var label = document.querySelector(".js-copy-label");
      var status = document.querySelector(".js-copy-status");
      if (!field) return;

      navigator.clipboard.writeText(field.value).then(function () {
        label.textContent = "Copied";
        icon.className = "bi bi-check-lg";
        copyBtn.classList.remove("btn-outline-dark");
        copyBtn.classList.add("btn-success");
        status.textContent = "Secret copied to clipboard.";

        setTimeout(function () {
          label.textContent = "Copy";
          icon.className = "bi bi-clipboard";
          copyBtn.classList.remove("btn-success");
          copyBtn.classList.add("btn-outline-dark");
          status.textContent = "";
        }, 2000);
      });
    });
  }

});
