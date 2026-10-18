/* CI Observatory: start-up, header, navigation, the repository filter and keyboard shortcuts. */
(function () {
  'use strict';
  const O = window.O;

  function start() {
    let data;
    try {
      data = JSON.parse(document.getElementById('obs-data').textContent);
    } catch (e) {
      document.getElementById('obs').prepend(O.el('div', { class: 'err wrap', text: 'The report data could not be read: ' + e.message }));
      return;
    }
    window.OBS = data;
    O.readColors();
    O.fleet.prep(data);
    header(data);
    O.safe('kpis', () => O.fleet.init());
    O.safe('dora-tiles', () => O.dora.init(data));
    O.safe('job-answer', () => O.rec.init(data));
    O.safe('causes', () => O.diag.init(data));
    O.safe('tests-kpis', () => O.tests.init(data));
    O.safe('ai-kpis', () => O.ai.init(data));
    O.safe('solution-cards', () => O.solutions.init(data));
    O.safe('method-body', () => O.method(data));
    nav();
    keys();
    loop();
    intro(data);
  }

  function header(data) {
    const meta = data.meta || {};
    const logs = data.logs && data.logs.totals;
    O.put(O.clear(O.$('#status-line')), [
      O.el('span', null, O.el('i', { class: 'led' }), `${O.fmt.int(O.fleet.runs.length)} runs in ${O.fleet.repos.length === 1 ? '1 repository' : O.fleet.repos.length + ' repositories'}`),
      logs ? O.el('span', { text: `${logs.jobs} job logs read` }) : null,
      meta.generated_at ? O.el('span', { text: `Updated ${new Date(meta.generated_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' })}` }) : null,
    ]);
    const filter = O.$('#filter');
    const show = (repo) => {
      O.clear(filter);
      if (!repo) { filter.appendChild(O.el('span', { text: 'Showing all repositories' })); return; }
      O.put(filter, ['Showing ', O.el('b', { text: O.fmt.short(repo) }), ' ', O.el('button', { class: 'chip', type: 'button', text: 'Show all', title: 'Clear (Esc)', on: { click: () => O.selectRepo(repo) } })]);
    };
    show(null);
    O.on('repo', show);
  }

  function nav() {
    const links = O.$$('.nav a');
    const sections = links.map((a) => document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
    const seen = new Map();
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) seen.set(e.target.id, e.intersectionRatio);
      let best = null, ratio = 0;
      for (const s of sections) { const r = seen.get(s.id) || 0; if (r > ratio) { best = s.id; ratio = r; } }
      for (const a of links) a.classList.toggle('on', a.getAttribute('href') === '#' + best);
    }, { threshold: [0, 0.1, 0.25, 0.5, 0.75] });
    sections.forEach((s) => io.observe(s));
  }

  function keys() {
    document.addEventListener('keydown', (e) => {
      const tag = (e.target && e.target.tagName) || '';
      if (/INPUT|SELECT|TEXTAREA/.test(tag) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'Escape' && O.state.repo) O.selectRepo(O.state.repo);
    });
  }

  /** Only the job log player animates; the loop idles otherwise. */
  function loop() {
    let last = performance.now();
    const frame = (now) => {
      const dt = Math.min(100, now - last);
      last = now;
      if (!document.hidden && O.rec.playing) O.rec.frame(dt);
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  function intro(data) {
    const boot = O.$('#boot');
    const finish = () => {
      if (boot.classList.contains('gone')) return;
      boot.classList.add('gone');
      setTimeout(() => { boot.hidden = true; }, 500);
    };
    if (O.reduced || location.hash === '#noboot' || (data.meta && data.meta.noboot)) { finish(); return; }
    const sig = data.signals && data.signals.totals, logs = data.logs && data.logs.totals;
    const lines = [
      `> Reading ${O.fmt.int(O.fleet.runs.length)} CI runs from ${O.fleet.repos.length === 1 ? '1 repository' : O.fleet.repos.length + ' repositories'}`,
      logs ? `> Decoding ${logs.jobs} job logs` : null,
      sig ? `> Tracing AI in ${sig.prs} pull requests` : null,
      '> Ready',
    ].filter(Boolean);
    const pre = O.$('#boot-text');
    let i = 0;
    const tick = () => {
      if (boot.classList.contains('gone')) return;
      if (i >= lines.length) { setTimeout(finish, 400); return; }
      pre.textContent += (i ? '\n' : '') + lines[i++];
      setTimeout(tick, 200);
    };
    boot.addEventListener('click', finish);
    document.addEventListener('keydown', finish, { once: true });
    tick();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
