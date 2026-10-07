// dpi-audit in-page helper library. Evaluated on demand (not subject to page CSP).
// Collects facts only; all decisions are made in Python (dpi_audit.runtime.rules).
(() => {
  if (window.__dpi && window.__dpi.version === 1) return true;
  const D = (window.__dpi = { version: 1 });
  let nextId = 1;
  const ids = new WeakMap();
  const byId = new Map();
  D.id = (el) => {
    if (!el) return null;
    if (!ids.has(el)) { const i = "e" + nextId++; ids.set(el, i); byId.set(i, new WeakRef(el)); }
    return ids.get(el);
  };
  D.el = (id) => { const r = byId.get(id); return r ? r.deref() : null; };

  D.cssPath = (el) => {
    if (!(el instanceof Element)) return "";
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 7) {
      let s = cur.localName;
      if (cur.id && /^[A-Za-z][\w-]*$/.test(cur.id) && document.querySelectorAll("#" + cur.id).length === 1) {
        parts.unshift(s + "#" + cur.id);
        break;
      }
      const p = cur.parentElement;
      if (p) {
        const sib = Array.from(p.children).filter((c) => c.localName === cur.localName);
        if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(cur) + 1})`;
      }
      parts.unshift(s);
      cur = p;
      if (cur && cur.localName === "html") break;
    }
    return parts.join(" > ");
  };

  D.visible = (el) => {
    if (!(el instanceof Element)) return false;
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };

  const hasAxe = () => typeof window.axe === "object" && axe.commons;
  D.axeSetup = () => { try { if (hasAxe()) axe.setup(document); return true; } catch (e) { return false; } };
  D.axeTeardown = () => { try { if (hasAxe()) axe.teardown(); } catch (e) {} };
  D.name = (el) => {
    try {
      if (hasAxe() && axe.utils && axe.utils.getNodeFromTree && axe.utils.getNodeFromTree(el)) {
        return axe.commons.text.accessibleText(el).trim().slice(0, 200);
      }
    } catch (e) {}
    const lab = el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("title"));
    if (lab) return lab.trim().slice(0, 200);
    const lb = el.getAttribute && el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean).map((x) => x.textContent).join(" ");
      if (t.trim()) return t.trim().slice(0, 200);
    }
    if (el.labels && el.labels.length) return Array.from(el.labels).map((l) => l.textContent).join(" ").trim().slice(0, 200);
    if (el.localName === "img") return (el.getAttribute("alt") || "").slice(0, 200);
    return (el.innerText || el.textContent || el.value || "").replace(/\s+/g, " ").trim().slice(0, 200);
  };
  D.role = (el) => {
    try { if (hasAxe() && axe.utils.getNodeFromTree(el)) return axe.commons.aria.getRole(el) || ""; } catch (e) {}
    return el.getAttribute("role") || el.localName;
  };
  D.excerpt = (el, n = 400) => {
    if (!el || !el.outerHTML) return "";
    const h = el.outerHTML;
    return h.length > n ? h.slice(0, n) + "…" : h;
  };
  D.region = (el) => {
    for (let c = el; c && c !== document.body; c = c.parentElement) {
      const r = (c.getAttribute && c.getAttribute("role")) || "";
      if (c.localName === "nav" || r === "navigation") return "nav";
      if (c.localName === "header" || r === "banner") return "header";
      if (c.localName === "footer" || r === "contentinfo") return "footer";
      if (c.localName === "main" || r === "main") return "main";
      if (c.localName === "aside" || r === "complementary") return "aside";
    }
    return "other";
  };
  D.langOf = (el) => {
    for (let c = el; c; c = c.parentElement) {
      const l = c.getAttribute && (c.getAttribute("lang") || c.getAttribute("xml:lang"));
      if (l !== null && l !== undefined && l !== "") return { lang: l, from: c === el ? "self" : D.cssPath(c) };
    }
    return { lang: null, from: null };
  };

  // ------------------------------------------------------------- text segments
  D.textSegments = (max = 1500) => {
    const out = [];
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => {
        const p = n.parentElement;
        if (!p) return NodeFilter.FILTER_REJECT;
        const tag = p.localName;
        if (["script", "style", "noscript", "template", "svg", "code", "pre", "textarea"].includes(tag)) return NodeFilter.FILTER_REJECT;
        if (!/\p{L}{2,}/u.test(n.nodeValue)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let n;
    const visCache = new Map();
    while ((n = walker.nextNode()) && out.length < max) {
      const p = n.parentElement;
      let vis = visCache.get(p);
      if (vis === undefined) { vis = D.visible(p); visCache.set(p, vis); }
      if (!vis) continue;
      const t = n.nodeValue.replace(/\s+/g, " ").trim();
      if (!t) continue;
      const lg = D.langOf(p);
      out.push({ t: t.slice(0, 300), p: D.cssPath(p), lang: lg.lang, region: D.region(p) });
    }
    return out;
  };

  // ------------------------------------------------------------ snapshot
  const NATIVE_FOCUSABLE = "a[href],area[href],button,input:not([type=hidden]),select,textarea,summary,iframe,audio[controls],video[controls],[contenteditable=''],[contenteditable=true],[tabindex]";
  const ARIA_WIDGET = ["button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option", "slider", "spinbutton", "combobox", "textbox", "searchbox", "treeitem", "gridcell"];
  const COMPOSITE = ["tablist", "menu", "menubar", "listbox", "radiogroup", "tree", "grid", "treegrid", "toolbar"];

  D.snapshot = (opts) => {
    opts = opts || {};
    const maxLinks = opts.maxLinks || 600;
    const axeOk = D.axeSetup();
    const de = document.documentElement;
    const body = document.body || de;
    const res = {
      url: location.href, title: document.title || "", html_lang: de.getAttribute("lang"),
      xml_lang: de.getAttribute("xml:lang"), html_dir: de.getAttribute("dir"),
      body_dir: getComputedStyle(body).direction,
      main_dir: (() => { const m = document.querySelector("main,[role=main]"); return m ? getComputedStyle(m).direction : null; })(),
      axe_names: axeOk,
    };
    const mv = document.querySelector("meta[name=viewport]");
    res.meta_viewport = mv ? mv.getAttribute("content") : null;
    const mr = document.querySelector("meta[http-equiv=refresh i]");
    res.meta_refresh = mr ? mr.getAttribute("content") : null;
    res.scroll = { scrollWidth: de.scrollWidth, clientWidth: de.clientWidth, scrollHeight: de.scrollHeight, innerWidth: innerWidth };

    // landmarks
    const q = (s) => Array.from(document.querySelectorAll(s));
    const mains = q("main,[role=main]").filter((e) => D.visible(e) || e.children.length);
    const navs = q("nav,[role=navigation]");
    res.landmarks = {
      main: mains.length,
      nav: navs.map((n) => ({
        selector: D.cssPath(n), label: n.getAttribute("aria-label") || "", region: D.region(n.parentElement || n),
        visible: D.visible(n),
        links: Array.from(n.querySelectorAll("a[href]")).slice(0, 80).map((a) => ({ text: D.name(a), href: a.href })),
      })),
      header: q("header,[role=banner]").length, footer: q("footer,[role=contentinfo]").length,
      search: q("search,[role=search]").length,
    };
    // headings
    res.headings = q("h1,h2,h3,h4,h5,h6,[role=heading]").filter(D.visible).slice(0, 200).map((h) => ({
      level: h.getAttribute("aria-level") ? parseInt(h.getAttribute("aria-level")) : parseInt(h.localName.slice(1)) || 2,
      text: (h.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120), selector: D.cssPath(h),
    }));
    // images
    res.images = q("img,[role=img],svg[role=img],input[type=image],area").slice(0, 400).map((im) => {
      const alt = im.getAttribute("alt");
      const role = im.getAttribute("role") || "";
      const hidden = im.getAttribute("aria-hidden") === "true";
      return { selector: D.cssPath(im), tag: im.localName, alt, has_alt: alt !== null, role, aria_hidden: hidden,
               decorative: alt === "" || role === "presentation" || role === "none" || hidden,
               name: D.name(im).slice(0, 120), visible: D.visible(im), in_link: !!im.closest("a[href],button"),
               src: (im.currentSrc || im.getAttribute("src") || "").slice(0, 200) };
    });
    // links
    const host = location.host;
    res.links = q("a[href],area[href]").slice(0, maxLinks).map((a) => {
      let abs = "";
      try { abs = new URL(a.getAttribute("href"), location.href).href; } catch (e) {}
      let internal = false;
      try { internal = new URL(abs).host === host; } catch (e) {}
      return { text: (a.innerText || "").replace(/\s+/g, " ").trim().slice(0, 120), name: D.name(a).slice(0, 120),
               href: a.getAttribute("href"), abs, internal, region: D.region(a), selector: D.cssPath(a),
               aria_label: a.getAttribute("aria-label"), aria_describedby: a.getAttribute("aria-describedby"),
               hreflang: a.getAttribute("hreflang"), lang: a.getAttribute("lang"), visible: D.visible(a),
               parent: D.cssPath(a.parentElement && a.parentElement.parentElement ? a.parentElement.parentElement : a.parentElement),
               download: a.hasAttribute("download"), target: a.getAttribute("target"), rel: a.getAttribute("rel") };
    });
    res.buttons = q("button,[role=button],input[type=button],input[type=submit]").slice(0, 300).map((b) => ({
      text: (b.innerText || b.value || "").replace(/\s+/g, " ").trim().slice(0, 80), name: D.name(b).slice(0, 120),
      selector: D.cssPath(b), visible: D.visible(b), expanded: b.getAttribute("aria-expanded"),
      controls: b.getAttribute("aria-controls"), haspopup: b.getAttribute("aria-haspopup"),
      toggle: b.getAttribute("data-toggle") || b.getAttribute("data-bs-toggle"),
      target: b.getAttribute("data-target") || b.getAttribute("data-bs-target"), lang: b.getAttribute("lang"),
      parent: D.cssPath(b.parentElement && b.parentElement.parentElement ? b.parentElement.parentElement : b.parentElement),
      region: D.region(b),
    }));
    // selects (language switcher candidates are decided in Python)
    res.selects = q("select").slice(0, 60).map((s) => ({
      selector: D.cssPath(s), name: D.name(s), visible: D.visible(s), aria_label: s.getAttribute("aria-label"),
      options: Array.from(s.options).slice(0, 60).map((o) => ({ text: (o.text || "").trim().slice(0, 60), value: o.value, lang: o.getAttribute("lang"), selected: o.selected })),
    }));
    // forms & fields
    const fieldSel = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=image]):not([type=reset]),select,textarea";
    const fieldInfo = (f) => {
      const lbls = f.labels ? Array.from(f.labels).map((l) => (l.innerText || "").replace(/\s+/g, " ").trim()) : [];
      const db = (f.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean).map((i) => document.getElementById(i)).filter(Boolean).map((x) => (x.innerText || x.textContent || "").trim()).join(" ");
      return { selector: D.cssPath(f), tag: f.localName, type: (f.getAttribute("type") || f.localName).toLowerCase(),
               name: f.getAttribute("name"), id: f.id || null, label_text: lbls.join(" ").slice(0, 160),
               acc_name: D.name(f).slice(0, 160), aria_label: f.getAttribute("aria-label"), labelledby: f.getAttribute("aria-labelledby"),
               title: f.getAttribute("title"), placeholder: f.getAttribute("placeholder"), required: f.required === true,
               aria_required: f.getAttribute("aria-required"), autocomplete: f.getAttribute("autocomplete"),
               pattern: f.getAttribute("pattern"), minlength: f.getAttribute("minlength"), maxlength: f.getAttribute("maxlength"),
               inputmode: f.getAttribute("inputmode"), describedby_text: db.slice(0, 200), visible: D.visible(f),
               onpaste: f.getAttribute("onpaste"), readonly: f.readOnly === true, disabled: f.disabled === true };
    };
    res.forms = q("form").slice(0, 30).map((fm) => ({
      selector: D.cssPath(fm), method: (fm.getAttribute("method") || "get").toLowerCase(), action: fm.action,
      role: fm.getAttribute("role"), visible: D.visible(fm),
      text: (fm.innerText || "").replace(/\s+/g, " ").trim().slice(0, 600),
      fields: Array.from(fm.querySelectorAll(fieldSel)).slice(0, 60).map(fieldInfo),
      submit: !!fm.querySelector("button:not([type=button]):not([type=reset]),input[type=submit],input[type=image]"),
    }));
    res.loose_fields = q(fieldSel).filter((f) => !f.form).slice(0, 60).map(fieldInfo);
    // dialogs and triggers
    res.dialogs = q("dialog,[role=dialog],[role=alertdialog],[aria-modal=true]").slice(0, 30).map((d) => ({
      selector: D.cssPath(d), role: d.getAttribute("role") || d.localName, aria_modal: d.getAttribute("aria-modal"),
      open: d.localName === "dialog" ? d.open : null, visible: D.visible(d), id: d.id || null }));
    const dlgIds = new Set(res.dialogs.map((d) => d.id).filter(Boolean));
    res.dialog_triggers = q("[aria-haspopup=dialog],[data-toggle=modal],[data-bs-toggle=modal],[aria-controls],[data-target],[data-bs-target],[popovertarget]").filter((t) => {
      if (!D.visible(t)) return false;
      if (t.getAttribute("aria-haspopup") === "dialog" || /modal/.test(t.getAttribute("data-toggle") || t.getAttribute("data-bs-toggle") || "")) return true;
      const c = (t.getAttribute("aria-controls") || t.getAttribute("data-target") || t.getAttribute("data-bs-target") || t.getAttribute("popovertarget") || "").replace(/^#/, "");
      const ref = c && document.getElementById(c);
      return !!(ref && (dlgIds.has(c) || ref.localName === "dialog" || /dialog/.test(ref.getAttribute("role") || "") || /\bmodal\b/.test(ref.className || "")));
    }).slice(0, 10).map((t) => ({ id: D.id(t), selector: D.cssPath(t), name: D.name(t), tag: t.localName }));
    // media
    res.media = q("video,audio").slice(0, 20).map((m) => {
      const box = m.closest("figure,section,article,div") || m.parentElement;
      const near = box ? (box.innerText || "") : "";
      return { tag: m.localName, selector: D.cssPath(m), autoplay: m.autoplay, loop: m.loop, muted: m.muted, controls: m.controls,
               tracks: Array.from(m.querySelectorAll("track")).map((t) => ({ kind: t.kind || "subtitles", srclang: t.srclang })),
               transcript_cue: /transcript|प्रतिलेख|transcripción|transcription/i.test(near),
               description_cue: /audio description|described version|ऑडियो विवरण/i.test(near), visible: D.visible(m) };
    });
    res.embeds = q("iframe[src]").slice(0, 40).map((f) => ({ src: f.src.slice(0, 200), title: f.getAttribute("title"), selector: D.cssPath(f), visible: D.visible(f) }));
    res.scripts = q("script[src]").slice(0, 120).map((s) => s.src.slice(0, 200));
    // breadcrumbs / search / captcha / drag / moving content / text-size controls
    res.breadcrumbs = q("[aria-label*=readcrumb i],[class*=breadcrumb i],[itemtype$=BreadcrumbList]").slice(0, 4).map((b) => ({
      selector: D.cssPath(b), in_nav: !!b.closest("nav,[role=navigation]"), nav_label: (b.closest("nav,[role=navigation]") || b).getAttribute("aria-label"),
      has_current: !!b.querySelector("[aria-current]"), items: b.querySelectorAll("li,a").length, visible: D.visible(b) }));
    res.breadcrumb_jsonld = q("script[type='application/ld+json']").some((s) => /BreadcrumbList/.test(s.textContent || ""));
    res.search_controls = q("search,[role=search],input[type=search],input[name=q],input[name=query],input[name=search],input[name=s],input[name=keyword]").slice(0, 6).map((s) => ({
      selector: D.cssPath(s), tag: s.localName, name: s.matches("input") ? D.name(s) : "", visible: D.visible(s), id: D.id(s) }));
    res.captcha = q("iframe[src*=recaptcha],iframe[src*=hcaptcha],iframe[src*=turnstile],.g-recaptcha,.h-captcha,.cf-turnstile,img[src*=captcha i],img[id*=captcha i],img[class*=captcha i],img[alt*=captcha i],input[name*=captcha i],canvas[id*=captcha i]").slice(0, 6).map((c) => {
      const box = c.closest("form,div") || c.parentElement;
      const audio = box ? Array.from(box.querySelectorAll("a,button,img,[role=button]")).some((x) => /audio|listen|सुन|speak|ऑडियो/i.test(D.name(x) + " " + (x.getAttribute("src") || "") + " " + (x.className || ""))) : false;
      return { selector: D.cssPath(c), tag: c.localName, vendor: /recaptcha/i.test(c.outerHTML) ? "recaptcha" : /hcaptcha/i.test(c.outerHTML) ? "hcaptcha" : /turnstile/i.test(c.outerHTML) ? "turnstile" : "custom", audio_alternative_cue: audio };
    });
    res.draggable = q("[draggable=true],[aria-grabbed],.ui-sortable,[class*=sortable i],[class*=draggable i]").filter(D.visible).slice(0, 10).map((d) => ({ selector: D.cssPath(d), excerpt: D.excerpt(d, 160) }));
    let anims = [];
    try {
      anims = document.getAnimations().filter((a) => a.playState === "running").slice(0, 200).map((a) => {
        const t = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
        const tgt = a.effect && a.effect.target;
        return { infinite: t.iterations === Infinity, duration: typeof t.duration === "number" ? t.duration : null,
                 active: typeof t.activeDuration === "number" ? t.activeDuration : null, target: tgt ? D.cssPath(tgt) : "",
                 type: a.constructor.name };
      });
    } catch (e) {}
    res.animations = anims;
    res.moving = {
      marquee: q("marquee").length, blink: q("blink").length,
      autoplay_media: q("video[autoplay],audio[autoplay]").length,
      carousels: q("[aria-roledescription*=carousel i],[class*=carousel i],[class*=slider i],[class*=swiper i],[class*=slick i],[class*=owl-carousel i],[class*=marquee i]").filter(D.visible).slice(0, 10).map((c) => ({
        selector: D.cssPath(c),
        pause_control: Array.from(c.querySelectorAll("button,a,[role=button]")).some((b) => /pause|stop|रोक|play/i.test(D.name(b) + " " + (b.className || ""))),
      })),
    };
    res.pause_controls = q("button,a,[role=button]").filter((b) => /\b(pause|stop)\b|रोकें|ठहर/i.test(D.name(b) + " " + (b.getAttribute("title") || "") + " " + (b.className || ""))).slice(0, 10).map((b) => ({ selector: D.cssPath(b), name: D.name(b) }));
    res.text_size_controls = q("button,a,[role=button],input[type=button]").filter((b) => {
      const n = (D.name(b) + " " + (b.getAttribute("title") || "") + " " + (b.innerText || b.value || "")).trim();
      return /^(a\s*[+-]{1,2}|a\+|a-|\+a|-a)$/i.test((b.innerText || b.value || "").trim()) || /increase (font|text)|font size|text size|larger text|फ़ॉन्ट|फॉन्ट|अक्षर/i.test(n);
    }).filter(D.visible).slice(0, 6).map((b) => ({ id: D.id(b), selector: D.cssPath(b), name: D.name(b), text: (b.innerText || b.value || "").trim().slice(0, 20) }));
    res.theme_toggles = q("button,a,[role=button],[role=switch],input[type=checkbox]").filter((b) => /dark|high contrast|contrast|theme|उच्च कंट्रास्ट|कंट्रास्ट/i.test(D.name(b) + " " + (b.getAttribute("title") || ""))).filter(D.visible).slice(0, 6).map((b) => ({ selector: D.cssPath(b), name: D.name(b) }));
    res.abbr_titles = q("abbr[title]").slice(0, 100).map((a) => (a.textContent || "").trim());
    res.small_targets = q("a[href],button,input:not([type=hidden]),select,[role=button],[role=link],[role=checkbox],[role=tab]").filter(D.visible).slice(0, 800).reduce((acc, el) => {
      const r = el.getBoundingClientRect();
      if (r.width < 24 || r.height < 24) acc += 1;
      return acc;
    }, 0);
    res.control_colors = D.controlColors();
    res.clipped = D.clippedText(300);
    res.overflow = D.overflowCulprits(innerWidth, 2);
    res.text = D.textSegments(opts.maxSegments || 1500);
    res.permission_calls = (window.__dpiPerm || []).slice(0, 20);
    D.axeTeardown();
    return res;
  };

  // ------------------------------------------------------------ colours
  const parseColor = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c || "");
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  D.effectiveBg = (el) => {
    for (let c = el; c; c = c.parentElement) {
      const cs = getComputedStyle(c);
      if (cs.backgroundImage && cs.backgroundImage !== "none") return { image: true, color: null };
      const col = parseColor(cs.backgroundColor);
      if (col && col.a > 0.5) return { image: false, color: col };
    }
    return { image: false, color: { r: 255, g: 255, b: 255, a: 1 } };
  };
  D.controlColors = () => {
    const out = [];
    const ctrls = Array.from(document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=image]):not([type=range]):not([type=color]):not([type=file]),select,textarea")).filter(D.visible).slice(0, 40);
    for (const c of ctrls) {
      const cs = getComputedStyle(c);
      const sides = ["Top", "Right", "Bottom", "Left"].map((s) => ({
        width: parseFloat(cs["border" + s + "Width"]) || 0, style: cs["border" + s + "Style"], color: parseColor(cs["border" + s + "Color"]) }));
      const parentBg = D.effectiveBg(c.parentElement || c);
      out.push({ selector: D.cssPath(c), sides, bg: parseColor(cs.backgroundColor), parent_bg: parentBg.color, parent_bg_image: parentBg.image,
                 box_shadow: cs.boxShadow, appearance: cs.appearance || cs.webkitAppearance });
    }
    return out;
  };

  // ------------------------------------------------------------ layout measurements
  D.clippedText = (max = 300) => {
    const out = [];
    const els = document.querySelectorAll("body *");
    let n = 0;
    for (const el of els) {
      if (n++ > 6000 || out.length >= max) break;
      if (!el.firstChild || el === document.body) continue;
      const cs = getComputedStyle(el);
      const clipX = /hidden|clip/.test(cs.overflowX), clipY = /hidden|clip/.test(cs.overflowY);
      if (!clipX && !clipY) continue;
      if (!/\p{L}{2,}/u.test(el.textContent || "")) continue;
      if (!D.visible(el)) continue;
      const rr = el.getBoundingClientRect();
      if (rr.height >= 0.9 * Math.max(document.documentElement.scrollHeight, innerHeight)) continue;  // page-level wrappers
      if ((clipX && el.scrollWidth > el.clientWidth + 1) || (clipY && el.scrollHeight > el.clientHeight + 1)) {
        out.push(D.cssPath(el));
      }
    }
    return out;
  };
  const EXEMPT = "table,pre,code,canvas,svg,video,iframe,math,[role=grid],[role=application],[class*=map i],[id*=map i]";
  D.overflowCulprits = (vw, tol) => {
    const de = document.documentElement;
    const res = { scrollWidth: Math.max(de.scrollWidth, document.body ? document.body.scrollWidth : 0), clientWidth: de.clientWidth, culprits: [] };
    const els = document.querySelectorAll("body *");
    let n = 0;
    for (const el of els) {
      if (n++ > 8000 || res.culprits.length >= 25) break;
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      if (r.right <= vw + tol || r.left >= vw) continue;  // within viewport, or entirely off-canvas
      if (!D.visible(el)) continue;
      // skip if an ancestor clips/scrolls horizontally (content scrolls inside its own container)
      let clipped = false;
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const cs = getComputedStyle(a);
        if (/auto|scroll|hidden|clip/.test(cs.overflowX)) {
          const ar = a.getBoundingClientRect();
          if (ar.right <= vw + tol) { clipped = true; break; }
        }
      }
      if (clipped) continue;
      // only report the outermost culprit
      if (res.culprits.some((c) => c._el && c._el.contains(el))) continue;
      res.culprits.push({ _el: el, selector: D.cssPath(el), right: Math.round(r.right), width: Math.round(r.width),
                          exempt: !!el.closest(EXEMPT) || el.matches(EXEMPT), tag: el.localName,
                          has_text: /\p{L}{2,}/u.test(el.innerText || "") });
    }
    res.culprits = res.culprits.map(({ _el, ...rest }) => rest);
    return res;
  };

  // ------------------------------------------------------------ keyboard support
  D.resetFocus = () => {
    try { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } catch (e) {}
    window.scrollTo(0, 0);
    return true;
  };
  D.focusInfo = () => {
    const el = document.activeElement;
    if (!el || el === document.body || el === document.documentElement) return { id: null, tag: el ? el.localName : null, body: true };
    const deep = (e) => (e && e.shadowRoot && e.shadowRoot.activeElement ? deep(e.shadowRoot.activeElement) : e);
    const f = deep(el);
    const r = f.getBoundingClientRect();
    const cs = getComputedStyle(f);
    const vw = innerWidth, vh = innerHeight;
    const pts = [];
    const x0 = Math.max(0, r.left), x1 = Math.min(vw - 1, r.right), y0 = Math.max(0, r.top), y1 = Math.min(vh - 1, r.bottom);
    let covered = null, coveredBy = null, inViewport = x1 > x0 && y1 > y0;
    if (inViewport) {
      const ix = Math.min(3, (x1 - x0) / 4), iy = Math.min(3, (y1 - y0) / 4);
      pts.push([(x0 + x1) / 2, (y0 + y1) / 2], [x0 + ix, y0 + iy], [x1 - ix, y0 + iy], [x0 + ix, y1 - iy], [x1 - ix, y1 - iy]);
      let hidden = 0;
      for (const [x, y] of pts) {
        const h = document.elementFromPoint(x, y);
        if (!h || h === f || f.contains(h) || h.contains(f)) continue;
        hidden += 1;
        coveredBy = coveredBy || D.cssPath(h);
      }
      covered = hidden === pts.length;
    } else {
      covered = true;
      coveredBy = "outside viewport after focus";
    }
    return {
      id: D.id(f), tag: f.localName, role: f.getAttribute("role") || "", name: D.name(f).slice(0, 100), selector: D.cssPath(f),
      rect: { x: r.left, y: r.top, w: r.width, h: r.height }, scrollY: window.scrollY, scrollX: window.scrollX,
      covered, covered_by: coveredBy, in_dialog: !!f.closest("dialog,[role=dialog],[role=alertdialog],[aria-modal=true]"),
      href: f.getAttribute("href"), outline: cs.outlineStyle + " " + cs.outlineWidth + " " + cs.outlineColor,
      box_shadow: cs.boxShadow, tabindex: f.getAttribute("tabindex"), excerpt: D.excerpt(f, 200),
    };
  };
  D.blurActive = () => { const el = document.activeElement; if (el && el.blur) el.blur(); return true; };
  D.refocus = (id) => { const el = D.el(id); if (el && el.focus) { el.focus({ preventScroll: true }); return document.activeElement === el; } return false; };

  D.interactive = (maxEls = 3000) => {
    const out = [];
    const seen = new Set();
    const add = (el, kind) => {
      if (seen.has(el) || !D.visible(el)) return;
      if (el.closest("[inert]") || el.closest("[aria-hidden=true]") && kind === "pointer") return;
      seen.add(el);
      const role = el.getAttribute("role") || "";
      const comp = el.closest(COMPOSITE.map((r) => `[role=${r}]`).join(","));
      out.push({ id: D.id(el), selector: D.cssPath(el), kind, tag: el.localName, role, name: D.name(el).slice(0, 80),
                 tabindex: el.getAttribute("tabindex"), disabled: el.disabled === true || el.getAttribute("aria-disabled") === "true",
                 composite: comp ? D.id(comp) : null, excerpt: D.excerpt(el, 160) });
    };
    document.querySelectorAll(NATIVE_FOCUSABLE).forEach((el) => {
      if (el.matches("[tabindex]") && !el.matches("a[href],button,input,select,textarea,summary,iframe") && parseInt(el.getAttribute("tabindex")) < 0) return;
      if (el.matches("a[href],button,input,select,textarea,summary,iframe,area")) add(el, "native");
      else if (el.matches("[tabindex]")) add(el, "tabindex");
      else add(el, "native");
    });
    document.querySelectorAll(ARIA_WIDGET.map((r) => `[role=${r}]`).join(",")).forEach((el) => add(el, "aria"));
    let n = 0;
    for (const el of document.querySelectorAll("body *")) {
      if (n++ > maxEls) break;
      if (seen.has(el)) continue;
      if (el.closest("a[href],button,input,select,textarea,summary,label,[role=button],[role=link],[tabindex]")) continue;
      const onclick = el.hasAttribute("onclick") || el.__dpiClick === true;
      if (!onclick) continue;
      if (el.querySelector("a[href],button,input,select,textarea,[tabindex]")) continue;  // container delegating to real controls
      const cs = getComputedStyle(el);
      if (cs.cursor === "pointer" || el.hasAttribute("onclick")) add(el, "pointer");
    }
    return out;
  };
  D.reachability = (reachedIds) => {
    const reached = new Set(reachedIds);
    const reachedEls = reachedIds.map((i) => D.el(i)).filter(Boolean);
    const last = reachedEls.length ? reachedEls[reachedEls.length - 1] : null;
    const reachedHrefs = new Set(reachedEls.map((r) => r.getAttribute && r.getAttribute("href")).filter(Boolean));
    return D.interactive().map((c) => {
      const el = D.el(c.id);
      let ok = reached.has(c.id);
      if (!ok && el) ok = reachedEls.some((r) => r.contains(el) || el.contains(r));
      if (!ok && c.composite) { const comp = D.el(c.composite); ok = !!comp && reachedEls.some((r) => comp.contains(r)); }
      const before_last = !!(el && last && (el === last || (last.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)));
      const dup = !!(el && !ok && el.getAttribute && el.getAttribute("href") && reachedHrefs.has(el.getAttribute("href")));
      return { ...c, reached: ok, before_last, duplicate_href_reached: dup };
    });
  };

  // safe in-page activation candidates (no navigation)
  D.activationCandidates = (max = 4) => {
    const c = Array.from(document.querySelectorAll("button[aria-expanded],[role=button][aria-expanded],button[aria-controls],summary,[role=tab]:not([aria-selected=true])"))
      .filter(D.visible).filter((b) => !b.closest("form") || b.type === "button").slice(0, max);
    return c.map((b) => ({ id: D.id(b), selector: D.cssPath(b), name: D.name(b), tag: b.localName, role: b.getAttribute("role") || "" }));
  };
  D.stateOf = (id) => {
    const el = D.el(id);
    if (!el) return null;
    const ctl = (el.getAttribute("aria-controls") || "").split(/\s+/).map((i) => document.getElementById(i)).filter(Boolean)[0];
    return { expanded: el.getAttribute("aria-expanded"), selected: el.getAttribute("aria-selected"), pressed: el.getAttribute("aria-pressed"),
             open: el.localName === "summary" && el.parentElement ? el.parentElement.open : null,
             controlled_visible: ctl ? D.visible(ctl) : null, active: document.activeElement ? D.id(document.activeElement) : null,
             mutations: D._mut || 0, url: location.href };
  };
  D.watchMutations = () => {
    D._mut = 0;
    D._added = [];
    if (D._obs) D._obs.disconnect();
    D._obs = new MutationObserver((recs) => {
      for (const r of recs) {
        D._mut += 1;
        if (r.type === "childList") {
          r.addedNodes.forEach((n) => {
            const el = n.nodeType === 1 ? n : n.parentElement;
            if (!el || D._added.length > 60) return;
            const t = (n.nodeType === 3 ? n.nodeValue : (n.innerText || n.textContent || "")).replace(/\s+/g, " ").trim();
            if (t && /\p{L}{2,}/u.test(t)) D._added.push({ el, text: t.slice(0, 200) });
          });
        } else if (r.type === "characterData") {
          const el = r.target.parentElement;
          const t = (r.target.nodeValue || "").trim();
          if (el && t && D._added.length <= 60) D._added.push({ el, text: t.slice(0, 200) });
        } else if (r.type === "attributes" && r.attributeName === "class") {
          const el = r.target;
          if (el && D.visible(el) && D._added.length <= 60) {
            const t = (el.innerText || "").replace(/\s+/g, " ").trim();
            if (t && t.length < 200 && /error|invalid|required|अनिवार्य|त्रुटि/i.test(el.className + " " + t)) D._added.push({ el, text: t.slice(0, 200) });
          }
        }
      }
    });
    D._obs.observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class", "aria-invalid", "hidden", "style"] });
    return true;
  };
  D.addedMessages = () => {
    return (D._added || []).filter((a) => a.el.isConnected && D.visible(a.el)).map((a) => {
      const live = a.el.closest("[aria-live],[role=alert],[role=status],[role=log],output");
      return { text: a.text, selector: D.cssPath(a.el), live: !!live && (live.getAttribute("aria-live") !== "off"),
               live_kind: live ? (live.getAttribute("role") || "aria-live=" + live.getAttribute("aria-live")) : null,
               focused: document.activeElement && (a.el.contains(document.activeElement) || document.activeElement.contains(a.el)) };
    });
  };
  D.pasteBlocked = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    let dt;
    try { dt = new DataTransfer(); dt.setData("text/plain", "x"); } catch (e) { dt = null; }
    const ev = new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true });
    el.dispatchEvent(ev);
    return { prevented: ev.defaultPrevented, onpaste_attr: el.getAttribute("onpaste"), autocomplete: el.getAttribute("autocomplete") };
  };
  D.bodyFontSize = () => {
    const p = document.querySelector("main p, article p, p, main, body");
    return p ? parseFloat(getComputedStyle(p).fontSize) : null;
  };
  D.bgLuminance = () => {
    const c = D.effectiveBg(document.body || document.documentElement).color;
    return c ? (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) : null;
  };
  return true;
})();
