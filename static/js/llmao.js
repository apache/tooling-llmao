/* llmao front-end hooks */
document.addEventListener("DOMContentLoaded", function () {
  var revokeModal = document.getElementById("revokeModal");
  if (!revokeModal) return;
  revokeModal.addEventListener("show.bs.modal", function (event) {
    var btn = event.relatedTarget;
    if (!btn) return;
    var token = btn.getAttribute("data-token") || "";
    var purpose = btn.getAttribute("data-purpose") || "";
    var tokenInput = document.getElementById("revokeToken");
    var purposeEl = document.getElementById("revokePurpose");
    if (tokenInput) tokenInput.value = token;
    if (purposeEl) purposeEl.textContent = purpose;
  });
});
