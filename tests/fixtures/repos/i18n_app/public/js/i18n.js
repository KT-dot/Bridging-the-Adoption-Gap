// Minimal first-party i18n: loads /locales/<lang>.json, applies [data-i18n] keys,
// updates <html lang>, and persists the choice in localStorage.
(function () {
  var root = document.currentScript.src.replace(/js\/i18n\.js.*$/, "");
  var i18n = window.i18n = { lang: "en", dict: {} };
  i18n.t = function (key) { return i18n.dict[key] || key; };
  function apply() {
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      var k = el.getAttribute("data-i18n");
      if (i18n.dict[k]) el.textContent = i18n.dict[k];
    });
    var t = document.querySelector("[data-i18n-title]");
    if (t && i18n.dict[t.getAttribute("data-i18n-title")]) document.title = i18n.dict[t.getAttribute("data-i18n-title")] + " – " + i18n.t("site.name");
    document.documentElement.lang = i18n.lang;
    var sel = document.getElementById("lang-select");
    if (sel) sel.value = i18n.lang;
  }
  i18n.changeLanguage = function (lang) {
    return fetch(root + "locales/" + lang + ".json").then(function (r) { return r.json(); }).then(function (d) {
      i18n.dict = d; i18n.lang = lang;
      try { localStorage.setItem("lang", lang); } catch (e) {}
      apply();
    });
  };
  var saved = null;
  try { saved = localStorage.getItem("lang"); } catch (e) {}
  if (saved && saved !== "en") i18n.changeLanguage(saved);
  document.addEventListener("change", function (e) {
    if (e.target && e.target.id === "lang-select") i18n.changeLanguage(e.target.value);
  });
})();
