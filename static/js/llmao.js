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
          set("mdProvider", btn.getAttribute("data-provider"));
          set("mdWeights", btn.getAttribute("data-weights"));
          set("mdTraining", btn.getAttribute("data-training"));
          set("mdProvenance", btn.getAttribute("data-provenance"));
        } else {
          supply.classList.add("d-none");
        }
      }
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
