/* llmao front-end hooks */
document.addEventListener("DOMContentLoaded", function () {
  var revokeModal = document.getElementById("revokeModal");
  if (!revokeModal) return;
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
});
