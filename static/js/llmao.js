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
      var thinking = btn.getAttribute("data-thinking") === "True" ||
        btn.getAttribute("data-thinking") === "true";
      var thinksDefault = btn.getAttribute("data-thinks-default") === "True" ||
        btn.getAttribute("data-thinks-default") === "true";
      var thinkText = thinking
        ? (thinksDefault ? "Supported (on by default)" : "Supported (off by default)")
        : "Not advertised";
      set("mdThinking", thinkText);
      set("mdNotes", btn.getAttribute("data-notes"));

      var supply = document.getElementById("mdSupplyBlock");
      var reveal = btn.getAttribute("data-reveal-supply") === "True" ||
        btn.getAttribute("data-reveal-supply") === "true";
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
});
