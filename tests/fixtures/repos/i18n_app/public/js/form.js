// Client-side validation: errors associated via aria-describedby, announced in a live region,
// entered values preserved.
(function () {
  var form = document.getElementById("contact-form");
  if (!form) return;
  form.addEventListener("submit", function (e) {
    var bad = [];
    form.querySelectorAll("[required]").forEach(function (f) {
      var err = document.getElementById(f.id + "-error");
      if (!f.value.trim()) {
        f.setAttribute("aria-invalid", "true");
        err.textContent = window.i18n ? window.i18n.t("form.required") : "This field is required.";
        bad.push(f);
      } else {
        f.removeAttribute("aria-invalid");
        err.textContent = "";
      }
    });
    if (bad.length) {
      e.preventDefault();
      document.getElementById("form-status").textContent = window.i18n ? window.i18n.t("form.errors") : "Please correct the errors below.";
      bad[0].focus();
    }
  });
})();
