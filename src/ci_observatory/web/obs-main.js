/* CI Observatory: start-up, header, the section sidebar (one section at a time), the repository filter and keyboard
   shortcuts. */
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
    O.safe('toc', () => badges(data));
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
    const focus = O.$('#focus');
    for (const r of O.fleet.repos.slice().sort((a, b) => b.runs - a.runs)) focus.appendChild(O.el('option', { value: r.repo, text: `${r.short} (${O.fmt.int(r.runs)} runs)` }));
    focus.addEventListener('change', () => {
      if (focus.value) { if (focus.value !== O.state.repo) O.selectRepo(focus.value); }
      else if (O.state.repo) O.selectRepo(O.state.repo);
    });
    if (O.fleet.repos.length < 2) focus.closest('.focus').hidden = true;
    const note = O.$('#filter');
    const show = (repo) => {
      focus.value = repo || '';
      O.clear(note);
      if (!repo) { note.textContent = 'Every section covers all of them. Click a repository in any chart to focus on it.'; return; }
      O.put(note, ['Every section shows only ', O.el('b', { text: O.fmt.short(repo) }), '. ', O.el('button', { class: 'chip', type: 'button', text: 'Show all', title: 'Clear (Esc)', on: { click: () => O.selectRepo(repo) } })]);
    };
    show(null);
    O.on('repo', show);
  }

  /** The short figure beside each section in the sidebar. A missing number leaves its badge out. */
  function badges(data) {
    const set = (id, text, tone) => {
      const b = O.$(`[data-badge="${id}"]`);
      if (!b || !text) return;
      b.textContent = text;
      if (tone) b.classList.add(tone);
    };
    const decided = O.sum(O.fleet.repos, (r) => r.decided), ok = O.sum(O.fleet.repos, (r) => r.ok);
    if (decided) set('overview', `${O.fmt.pct(ok / decided)} pass`);
    const shipped = O.dora && O.dora.repos ? O.sum(O.dora.repos, (r) => r.deployments || 0) : 0;
    if (shipped) set('delivery', `${O.fmt.int(shipped)} ${O.dora.repos.some((r) => r.source === 'releases') ? 'releases' : 'deploys'}`);
    const logs = data.logs && data.logs.totals;
    if (logs && logs.jobs) set('health', logs.failed ? `${logs.failed} failed` : 'no failures', logs.failed ? 'warn' : null);
    if (O.rec && O.rec.jobs && O.rec.jobs.length) set('job', `${O.rec.jobs.length} jobs`);
    const tests = data.tests && data.tests.summary;
    if (tests && tests.tests_run) {
      const waste = O.tests && O.tests.waste ? O.tests.waste(tests.extra || {}) : 0;
      set('tests', waste ? `${O.fmt.int(waste)} repeats` : `${O.fmt.int(tests.unique_tests)} tests`, waste ? 'warn' : null);
    }
    const sig = data.signals && data.signals.totals;
    if (sig && sig.prs) set('ai', `${O.fmt.pct(sig.ai_prs / sig.prs)} of PRs`);
    const fixes = O.solutions && O.solutions.cards;
    if (fixes && fixes.length) set('fixes', `${fixes.length} fixes`);
  }

  /**
   * One section at a time, chosen in the sidebar. The address keeps the section (#delivery), so links, reloads and the
   * back button land on it. "Show everything on one page" brings back the long page, for searching or printing.
   */
  function nav() {
    const obs = O.$('#obs'), side = O.$('#side'), toggle = O.$('#side-toggle'), allBtn = O.$('#all-toggle');
    const links = O.$$('#toc a');
    const ids = links.map((a) => a.dataset.section);
    const part = (a, sel) => a.querySelector(sel).textContent;
    let all = false;
    try { all = localStorage.getItem('ci-observatory-all') === '1'; } catch (e) { /* stays one section at a time */ }
    let current = ids.includes(location.hash.slice(1)) ? location.hash.slice(1) : ids[0];

    const menu = (open) => { side.classList.toggle('open', open); toggle.setAttribute('aria-expanded', String(open)); };
    toggle.addEventListener('click', () => menu(!side.classList.contains('open')));

    const mark = (id) => {
      for (const a of links) {
        const on = a.dataset.section === id;
        a.classList.toggle('on', on);
        if (on) a.setAttribute('aria-current', 'location'); else a.removeAttribute('aria-current');
      }
      const a = links[ids.indexOf(id)];
      O.$('#side-no').textContent = part(a, '.no');
      O.$('#side-current').textContent = part(a, 'b');
    };
    const pager = () => {
      const box = O.clear(O.$('#pager')), i = ids.indexOf(current);
      const card = (j, dir) => O.el('a', { class: dir, href: '#' + ids[j] },
        O.el('span', { class: 'dir', text: dir === 'prev' ? '← Previous' : 'Next →' }),
        O.el('b', null, O.el('span', { class: 'no', text: part(links[j], '.no') }), part(links[j], 'b')),
        O.el('small', { text: part(links[j], 'small') }));
      if (i > 0) box.appendChild(card(i - 1, 'prev'));
      if (i < ids.length - 1) box.appendChild(card(i + 1, 'next'));
    };
    const show = (id, jump) => {
      current = ids.includes(id) ? id : ids[0];
      obs.classList.toggle('all', all);
      for (const sid of ids) document.getElementById(sid).hidden = !all && sid !== current;
      if (current !== 'job' && O.rec && O.rec.playing && !all) O.rec.play(false);
      mark(current);
      pager();
      menu(false);
      if (!jump) return;
      if (all) document.getElementById(current).scrollIntoView({ behavior: O.reduced ? 'auto' : 'smooth', block: 'start' });
      else if (window.scrollY > O.$('#shell').offsetTop) window.scrollTo({ top: O.$('#shell').offsetTop - 8 });
    };

    document.addEventListener('click', (e) => {
      const a = e.target.closest && e.target.closest('a[href^="#"]');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
      const id = a.getAttribute('href').slice(1);
      if (!ids.includes(id)) return;
      e.preventDefault();
      if (id !== current || all) history.pushState(null, '', '#' + id);
      show(id, true);
    });
    window.addEventListener('popstate', () => show(location.hash.slice(1), true));
    allBtn.addEventListener('click', () => {
      all = !all;
      try { localStorage.setItem('ci-observatory-all', all ? '1' : '0'); } catch (e) { /* only for this visit */ }
      allBtn.textContent = all ? 'Show one section at a time' : 'Show everything on one page';
      allBtn.setAttribute('aria-pressed', String(all));
      show(current, true);
    });
    allBtn.textContent = all ? 'Show one section at a time' : 'Show everything on one page';
    allBtn.setAttribute('aria-pressed', String(all));

    // On the long page, the sidebar follows the section being read.
    const seen = new Map();
    const io = new IntersectionObserver((entries) => {
      if (!all) return;
      for (const e of entries) seen.set(e.target.id, e.intersectionRatio);
      let best = null, ratio = 0;
      for (const id of ids) { const r = seen.get(id) || 0; if (r > ratio) { best = id; ratio = r; } }
      if (best) { current = best; mark(best); }
    }, { threshold: [0, 0.1, 0.25, 0.5, 0.75] });
    ids.forEach((id) => io.observe(document.getElementById(id)));

    O.go = (id) => { if (id !== current) history.pushState(null, '', '#' + id); show(id, true); };
    show(current, false);
    // Opened on a section's address: start at the top of the page, not where the browser jumped to for the anchor.
    if (!all && ids.includes(location.hash.slice(1))) {
      const top = () => window.scrollTo(0, 0);
      if (document.readyState === 'complete') top(); else window.addEventListener('load', top, { once: true });
    }
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
