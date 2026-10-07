// Init script (runs before page scripts on every navigation, all frames).
// Records — never alters behaviour of — click listener registration and permission
// requests, so pointer-only controls and on-load permission prompts can be reported.
(() => {
  if (window.__dpiInit) return;
  window.__dpiInit = true;
  const perm = (window.__dpiPerm = []);
  const activeNow = () => !!(navigator.userActivation && navigator.userActivation.hasBeenActive);
  try {
    const orig = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function (type, fn, opts) {
      try {
        if ((type === "click" || type === "mousedown" || type === "pointerdown" || type === "touchstart") &&
            this instanceof Element) {
          this.__dpiClick = true;
        }
      } catch (e) {}
      return orig.call(this, type, fn, opts);
    };
  } catch (e) {}
  const wrap = (obj, name, label) => {
    try {
      if (!obj || typeof obj[name] !== "function") return;
      const o = obj[name];
      obj[name] = function () {
        perm.push({ api: label, t: Math.round(performance.now()), user_activated: activeNow() });
        return o.apply(this, arguments);
      };
    } catch (e) {}
  };
  try { wrap(navigator.geolocation, "getCurrentPosition", "geolocation"); } catch (e) {}
  try { wrap(navigator.geolocation, "watchPosition", "geolocation"); } catch (e) {}
  try { if (window.Notification) wrap(Notification, "requestPermission", "notifications"); } catch (e) {}
  try { if (navigator.mediaDevices) wrap(navigator.mediaDevices, "getUserMedia", "camera/microphone"); } catch (e) {}
})();
