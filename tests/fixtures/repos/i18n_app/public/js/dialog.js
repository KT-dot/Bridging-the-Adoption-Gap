// Accessible modal: focus moves in, Tab is contained, Escape closes, focus returns.
(function () {
  var openBtn = document.getElementById("help-open");
  var dlg = document.getElementById("help-dialog");
  if (!openBtn || !dlg) return;
  var closeBtn = document.getElementById("help-close");
  function focusables() { return Array.prototype.slice.call(dlg.querySelectorAll("a[href],button,input,select,textarea")); }
  function close() { dlg.hidden = true; openBtn.focus(); }
  openBtn.addEventListener("click", function () { dlg.hidden = false; closeBtn.focus(); });
  closeBtn.addEventListener("click", close);
  dlg.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    var f = focusables(), first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
})();
