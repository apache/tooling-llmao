/* llmao front-end hooks */
document.addEventListener("DOMContentLoaded", function () {
  var revokeModal = document.getElementById("revokeModal");
  if (!revokeModal) return;
  revokeModal.addEventListener("show.bs.modal", function (event) {
    var btn = event.relatedTarget;
    if (!btn) return;
    var tokenId = btn.getAttribute("data-token-id") || "";
    var purpose = btn.getAttribute("data-purpose") || "";
    var tokenInput = document.getElementById("revokeTokenId");
    var purposeEl = document.getElementById("revokePurpose");
    if (tokenInput) tokenInput.value = tokenId;
    if (purposeEl) purposeEl.textContent = purpose;
  });
});
