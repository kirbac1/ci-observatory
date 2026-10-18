/* CI Observatory: shared helpers. Everything hangs off window.O. No libraries: plain DOM, SVG and canvas. */
(function () {
  'use strict';
  const O = (window.O = window.O || {});
  O.reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const SVGNS = 'http://www.w3.org/2000/svg';

  // ---------------------------------------------------------------- DOM
  O.$ = (sel, root) => (root || document).querySelector(sel);
  O.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  O.clear = (node) => { while (node && node.firstChild) node.removeChild(node.firstChild); return node; };
  function put(node, kids) {
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      node.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  O.put = put;
  O.el = function (tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else if (k === 'data') for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
      else if (k in node && typeof v !== 'string' && k !== 'role') node[k] = v;
      else node.setAttribute(k, v === true ? '' : v);
    }
    return put(node, kids);
  };
  O.svg = function (tag, attrs, ...kids) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'text') node.textContent = v;
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else node.setAttribute(k, v);
    }
    return put(node, kids);
  };
  O.width = (node, fallback) => Math.max(260, Math.floor((node && node.clientWidth) || fallback || 800));

  // ---------------------------------------------------------------- numbers and time
  const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
  const nf1 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 });
  O.fmt = {
    int: (n) => (n == null || isNaN(n) ? '–' : nf0.format(Math.round(n))),
    num: (n, d) => (n == null || isNaN(n) ? '–' : d === 0 ? nf0.format(n) : d === 2 ? n.toFixed(2) : nf1.format(n)),
    pct: (x, d) => (x == null || isNaN(x) ? '–' : (x * 100).toFixed(d == null ? 0 : d) + '%'),
    big: (n) => (n == null ? '–' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : nf0.format(n)),
    dur(s) {
      if (s == null || isNaN(s)) return '–';
      s = Math.round(s);
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
      return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
    },
    mins: (m) => (m == null ? '–' : m < 60 ? nf1.format(m) + ' min' : (m / 60).toFixed(1) + ' h'),
    hours(h) {
      if (h == null || isNaN(h)) return '–';
      if (h < 1) return Math.max(1, Math.round(h * 60)) + ' min';
      if (h < 48) return h.toFixed(1) + ' h';
      return (h / 24).toFixed(1) + ' d';
    },
    short: (repo) => String(repo || '').split('/').pop(),
  };
  O.TZ = [
    { id: 'UTC', label: 'UTC', zone: 'UTC' },
    { id: 'HEL', label: 'Helsinki', zone: 'Europe/Helsinki' },
    { id: 'LOCAL', label: 'Your time', zone: undefined },
  ];
  const fmtCache = new Map();
  function dtf(zone, opts) {
    const key = (zone || 'local') + JSON.stringify(opts);
    if (!fmtCache.has(key)) fmtCache.set(key, new Intl.DateTimeFormat('en-GB', Object.assign({ timeZone: zone }, opts)));
    return fmtCache.get(key);
  }
  O.zone = () => (O.TZ.find((z) => z.id === O.state.tz) || O.TZ[0]).zone;
  O.fmt.clock = (ms) => dtf(O.zone(), { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(ms);
  O.fmt.hm = (ms) => dtf(O.zone(), { hour: '2-digit', minute: '2-digit', hour12: false }).format(ms);
  O.fmt.day = (ms) => dtf(O.zone(), { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }).format(ms);
  O.fmt.dayShort = (ms) => dtf(O.zone(), { day: 'numeric', month: 'short' }).format(ms);
  O.fmt.when = (ms) => dtf(O.zone(), { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).format(ms);
  const offsetCache = new Map();
  /** Minutes east of UTC for a zone at a moment, cached per hour. */
  O.offset = function (ms, zone) {
    if (zone === 'UTC') return 0;
    const key = (zone || 'local') + Math.floor(ms / 3600000);
    let off = offsetCache.get(key);
    if (off == null) {
      if (zone === undefined) off = -new Date(ms).getTimezoneOffset();
      else {
        const p = {};
        for (const part of dtf(zone, { year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', hourCycle: 'h23' }).formatToParts(ms)) p[part.type] = part.value;
        off = Math.round((Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute) - Math.floor(ms / 60000) * 60000) / 60000);
      }
      offsetCache.set(key, off);
    }
    return off;
  };
  /** Hour of day as a fraction (0..24) in the selected zone. */
  O.hourOf = (ms) => {
    const local = ms + O.offset(ms, O.zone()) * 60000;
    return ((local % 86400000) + 86400000) % 86400000 / 3600000;
  };

  // ---------------------------------------------------------------- scales and stats
  O.linear = function (d0, d1, r0, r1) {
    const span = d1 - d0 || 1;
    const f = (v) => r0 + ((v - d0) / span) * (r1 - r0);
    f.invert = (p) => d0 + ((p - r0) / (r1 - r0 || 1)) * span;
    f.domain = [d0, d1];
    return f;
  };
  O.log = function (d0, d1, r0, r1) {
    const l0 = Math.log(d0), l1 = Math.log(d1);
    const f = (v) => r0 + ((Math.log(Math.min(d1, Math.max(d0, v))) - l0) / (l1 - l0 || 1)) * (r1 - r0);
    f.invert = (p) => Math.exp(l0 + ((p - r0) / (r1 - r0 || 1)) * (l1 - l0));
    return f;
  };
  O.ticks = function (min, max, count) {
    const span = max - min;
    if (!(span > 0)) return [min];
    const step0 = span / Math.max(1, count || 5);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= step0) || mag * 10;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  };
  O.sum = (arr, f) => arr.reduce((a, x) => a + (f ? f(x) : x) || a, 0);
  O.quantile = function (values, q) {
    const v = values.filter((x) => x != null && !isNaN(x)).sort((a, b) => a - b);
    if (!v.length) return null;
    const pos = (v.length - 1) * q, lo = Math.floor(pos), hi = Math.ceil(pos);
    return v[lo] + (v[hi] - v[lo]) * (pos - lo);
  };
  O.median = (values) => O.quantile(values, 0.5);
  /** Wilson 95% interval for k successes out of n. */
  O.wilson = function (k, n) {
    if (!n) return [null, null];
    const z = 1.96, p = k / n, d = 1 + (z * z) / n;
    const c = p + (z * z) / (2 * n), r = z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n));
    return [Math.max(0, (c - r) / d), Math.min(1, (c + r) / d)];
  };
  O.groupBy = function (arr, key) {
    const m = new Map();
    for (const x of arr) { const k = key(x); if (!m.has(k)) m.set(k, []); m.get(k).push(x); }
    return m;
  };

  // ---------------------------------------------------------------- colors (read from CSS tokens)
  O.color = {};
  O.readColors = function () {
    const cs = getComputedStyle(document.documentElement);
    for (const name of ['hud', 'hud-2', 'ink', 'ink-2', 'ink-3', 'good', 'warn', 'serious', 'crit', 'good-ink', 'crit-ink', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6', 'c7', 'c-other', 'hull', 'hull-2', 'void', 'band-1', 'band-2', 'band-3', 'band-4']) {
      O.color[name] = (cs.getPropertyValue('--' + name) || '').trim() || '#888';
    }
    O.ACT = {
      setup: { label: 'Setup', color: O.color.c1 },
      deps: { label: 'Dependencies', color: O.color.c2 },
      containers: { label: 'Containers', color: O.color.c3 },
      build: { label: 'Build', color: O.color.c4 },
      test: { label: 'Tests', color: O.color.c5 },
      lint: { label: 'Lint', color: O.color.c6 },
      security: { label: 'Security scan', color: O.color.c7 },
      other: { label: 'Other', color: O.color['c-other'] },
    };
    O.ACT_CODE = { s: 'setup', d: 'deps', k: 'containers', b: 'build', t: 'test', l: 'lint', x: 'security', o: 'other' };
  };
  O.resultColor = (res) => (res === 'success' ? O.color.good : res === 'failure' || res === 'timed_out' || res === 'startup_failure' ? O.color.crit : O.color['c-other']);
  /** Dark or white text, whichever reads better on a filled color. */
  O.inkOn = function (hex) {
    const h = (hex || '#888').replace('#', '');
    const c = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] < 0.19 ? '#ffffff' : '#04121a';
  };
  O.alpha = function (hex, a) {
    const h = hex.replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  };

  // ---------------------------------------------------------------- tooltip
  O.tip = {
    node: null,
    show(evt, content) {
      const node = this.node || (this.node = document.getElementById('tip'));
      if (!node) return;
      O.clear(node);
      put(node, [content]);
      node.hidden = false;
      this.move(evt);
    },
    move(evt) {
      const node = this.node;
      if (!node || node.hidden) return;
      const x = evt.clientX, y = evt.clientY;
      const w = node.offsetWidth, h = node.offsetHeight;
      let left = x + 14, top = y + 14;
      if (left + w > window.innerWidth - 8) left = Math.max(8, x - w - 14);
      if (top + h > window.innerHeight - 8) top = Math.max(8, y - h - 14);
      node.style.left = left + 'px';
      node.style.top = top + 'px';
    },
    hide() { if (this.node) this.node.hidden = true; },
  };
  /** Tooltip body: a heading, label/value rows and an optional note. */
  O.tipBody = function (title, rows, note) {
    return [
      title ? O.el('h4', { text: title }) : null,
      ...(rows || []).filter(Boolean).map(([k, v]) => O.el('div', { class: 'row' }, O.el('span', { text: k }), O.el('span', { text: v }))),
      note ? O.el('p', { text: note }) : null,
    ];
  };

  // ---------------------------------------------------------------- state and events
  O.state = { repo: null, tz: 'UTC', job: null };
  const handlers = {};
  O.on = (name, fn) => { (handlers[name] = handlers[name] || []).push(fn); };
  O.emit = (name, data) => { for (const fn of handlers[name] || []) { try { fn(data); } catch (e) { console.error(e); } } };
  O.selectRepo = function (repo) {
    O.state.repo = O.state.repo === repo ? null : repo;
    O.emit('repo', O.state.repo);
  };

  // ---------------------------------------------------------------- tables
  /**
   * Sortable table. columns: [{key, label, num, value(row), render(row), title}]. Rows are data objects.
   * opts: {sort, desc, onRow(row), isSelected(row), max}
   */
  O.table = function (container, columns, rows, opts) {
    opts = opts || {};
    let sortKey = opts.sort || null, desc = opts.desc !== false;
    const wrap = O.el('div', { class: 'tbl-wrap' });
    function value(col, row) { return col.value ? col.value(row) : row[col.key]; }
    function draw() {
      O.clear(wrap);
      let data = rows.slice();
      const col = columns.find((c) => c.key === sortKey);
      if (col) {
        data.sort((a, b) => {
          const va = value(col, a), vb = value(col, b);
          if (va == null && vb == null) return 0;
          if (va == null) return 1;
          if (vb == null) return -1;
          return (typeof va === 'string' ? va.localeCompare(vb) : va - vb) * (desc ? -1 : 1);
        });
      }
      if (opts.max) data = data.slice(0, opts.max);
      const head = O.el('tr', null, columns.map((c) => {
        const th = O.el('th', { class: c.num ? 'n' : null, title: c.title || null, scope: 'col' }, c.label + (c.key === sortKey ? (desc ? ' ▾' : ' ▴') : ''));
        if (c.sortable !== false) {
          th.dataset.sort = c.key;
          th.tabIndex = 0;
          const flip = () => { if (sortKey === c.key) desc = !desc; else { sortKey = c.key; desc = !!c.num; } draw(); };
          th.addEventListener('click', flip);
          th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } });
        }
        return th;
      }));
      const body = O.el('tbody', null, data.map((row) => {
        const tr = O.el('tr', { class: [opts.onRow ? 'click' : '', opts.isSelected && opts.isSelected(row) ? 'sel' : ''].join(' ').trim() || null },
          columns.map((c) => {
            const content = c.render ? c.render(row) : value(c, row);
            return O.el('td', { class: c.num ? 'n' : null }, content == null ? '–' : content);
          }));
        if (opts.onRow) {
          tr.tabIndex = 0;
          tr.addEventListener('click', () => opts.onRow(row));
          tr.addEventListener('keydown', (e) => { if (e.key === 'Enter') opts.onRow(row); });
        }
        return tr;
      }));
      wrap.appendChild(O.el('table', { class: 'tbl' }, O.el('thead', null, head), body));
    }
    draw();
    O.clear(container).appendChild(wrap);
    return { redraw: draw };
  };

  // ---------------------------------------------------------------- small widgets
  O.status = (kind, icon, text, title) => O.el('span', { class: 'st ' + kind, title: title || null }, O.el('i', { 'aria-hidden': 'true', text: icon }), text);
  O.good = (text, title) => O.status('good', '✓', text, title);
  O.warn = (text, title) => O.status('warn', '!', text, title);
  O.bad = (text, title) => O.status('crit', '✕', text, title);
  O.na = (text, title) => O.status('na', '–', text || 'n/a', title);
  O.kpi = (label, value, sub, cls) => O.el('div', { class: 'kpi' + (cls ? ' ' + cls : '') }, O.el('div', { class: 'l', text: label }), O.el('div', { class: 'v' }, value), sub ? O.el('div', { class: 's' }, sub) : null);
  O.legend = (container, items) => {
    O.clear(container);
    for (const it of items) container.appendChild(O.el('span', { class: it.line ? 'line' : null }, O.el('i', { style: { background: it.color } }), it.label));
  };
  O.seg = function (container, options, current, onPick, label) {
    O.clear(container);
    container.setAttribute('role', 'group');
    if (label) container.setAttribute('aria-label', label);
    for (const opt of options) {
      const button = O.el('button', { type: 'button', 'aria-pressed': String(opt.id === current), text: opt.label, title: opt.title || null });
      button.addEventListener('click', () => {
        for (const b of container.children) b.setAttribute('aria-pressed', String(b === button));
        onPick(opt.id);
      });
      container.appendChild(button);
    }
  };
  /** Copy text to the clipboard from a click handler, with a selectable fallback. */
  O.copy = function (text, button) {
    const done = (ok) => { if (button) { const old = button.textContent; button.textContent = ok ? 'Copied' : 'Select and copy'; setTimeout(() => (button.textContent = old), 1600); } };
    try {
      navigator.clipboard.writeText(text).then(() => done(true), () => { fallback(); done(false); });
    } catch (e) { fallback(); done(false); }
    function fallback() {
      const ta = O.el('textarea', { style: { position: 'fixed', left: '-9999px' } }, text);
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) { /* the text stays selected for the user */ }
      setTimeout(() => ta.remove(), 2000);
    }
  };

  // ---------------------------------------------------------------- canvas
  O.canvas = function (canvas) {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(10, canvas.clientWidth), h = Math.max(10, canvas.clientHeight);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h, dpr };
  };

  // ---------------------------------------------------------------- two simple chart shapes used everywhere
  /**
   * Horizontal bars: one labelled row per item, the value written after the bar.
   * rows: [{label, value, text, color, tip: [[k, v]], note, faded, onClick}]; opts: {left, max, reference: {value, label}}
   */
  O.hbars = function (container, rows, opts) {
    opts = opts || {};
    const w = O.width(container), rh = 32;
    const left = Math.min(opts.left || 190, Math.round(w * 0.42));
    const m = { l: left, r: opts.right || 110, t: opts.reference ? 22 : 4 };
    const h = m.t + rows.length * rh + 4;
    const max = opts.max || Math.max(1e-9, ...rows.map((r) => r.value));
    const x = O.linear(0, max, m.l, w - m.r);
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': opts.label || '' });
    const maxChars = Math.max(10, Math.floor((left - 14) / 7.2));
    rows.forEach((r, i) => {
      const y = m.t + i * rh;
      const label = r.label.length > maxChars ? r.label.slice(0, maxChars - 1) + '…' : r.label;
      svg.appendChild(O.svg('text', { x: m.l - 12, y: y + 20, 'text-anchor': 'end', class: r.strong ? 'lbl-strong' : 'lbl', text: label }));
      const bw = Math.max(3, x(r.value) - m.l);
      const bar = O.svg('rect', { x: m.l, y: y + 7, width: bw, height: rh - 14, rx: 5, fill: r.color || O.color.c1, opacity: r.faded ? 0.45 : 1 });
      if (r.tip || r.note) {
        bar.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(r.label, r.tip || [], r.note)));
        bar.addEventListener('pointerleave', () => O.tip.hide());
      }
      if (r.onClick) { bar.style.cursor = 'pointer'; bar.addEventListener('click', r.onClick); }
      svg.appendChild(bar);
      svg.appendChild(O.svg('text', { x: m.l + bw + 8, y: y + 20, class: 'lbl', text: r.text != null ? r.text : O.fmt.int(r.value) }));
    });
    if (opts.reference && opts.reference.value != null) {
      const rx = x(opts.reference.value);
      svg.appendChild(O.svg('line', { x1: rx, x2: rx, y1: m.t - 6, y2: h - 2, stroke: O.color.ink, 'stroke-dasharray': '4 4', opacity: 0.55 }));
      svg.appendChild(O.svg('text', { x: rx, y: m.t - 9, 'text-anchor': 'middle', class: 'lbl', text: opts.reference.label }));
    }
    O.clear(container).appendChild(svg);
  };
  /** One bar split into shares, with the bigger shares named inside it. parts: [{label, value, color}] */
  O.split = function (container, parts, opts) {
    opts = opts || {};
    const w = O.width(container), h = opts.height || 40;
    const total = O.sum(parts, (p) => p.value) || 1;
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': parts.map((p) => `${p.label} ${O.fmt.pct(p.value / total)}`).join(', ') });
    let at = 0;
    for (const p of parts) {
      if (!p.value) continue;
      const x0 = (at / total) * w, pw = (p.value / total) * w;
      const rect = O.svg('rect', { x: x0, y: 0, width: Math.max(1, pw - 2), height: h, rx: 6, fill: p.color });
      rect.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(p.label, [['Share', O.fmt.pct(p.value / total, 1)], ...(p.tip || [])])));
      rect.addEventListener('pointerleave', () => O.tip.hide());
      svg.appendChild(rect);
      const text = `${p.label} ${O.fmt.pct(p.value / total)}`;
      const ink = O.inkOn(p.color);
      if (pw > text.length * 7.4 + 14) svg.appendChild(O.svg('text', { x: x0 + 10, y: h / 2 + 4.5, style: `fill:${ink};font-size:13px;font-weight:600;pointer-events:none`, text }));
      else if (pw > 44) svg.appendChild(O.svg('text', { x: x0 + 8, y: h / 2 + 4.5, style: `fill:${ink};font-size:12.5px;font-weight:600;pointer-events:none`, text: O.fmt.pct(p.value / total) }));
      at += p.value;
    }
    O.clear(container).appendChild(svg);
  };

  /** Run a panel's renderer; on error, say so in the panel instead of breaking the page. */
  O.safe = function (target, fn) {
    try { return fn(); } catch (e) {
      console.error(e);
      const node = typeof target === 'string' ? document.getElementById(target) : target;
      if (node) O.clear(node).appendChild(O.el('div', { class: 'err', text: 'This panel could not be drawn: ' + (e && e.message ? e.message : e) }));
    }
  };
})();
