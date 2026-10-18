/* CI Observatory: delivery (DORA), inside a job, the health check and the test audit. */
(function () {
  'use strict';
  const O = window.O;

  // ================================================================ delivery (DORA)
  const D = (O.dora = {});
  D.BAND = { 4: 'Elite', 3: 'High', 2: 'Medium', 1: 'Low' };
  D.metrics = [
    { key: 'deploys_per_week', label: 'How often they ship', unit: 'per week', fmt: (v) => O.fmt.num(v, v < 1 ? 2 : 1), band: (v) => (v >= 7 ? 4 : v >= 1 ? 3 : v >= 0.25 ? 2 : 1) },
    { key: 'lead_time_h', label: 'From pull request to release', unit: '', fmt: (v) => O.fmt.hours(v), band: (v) => (v < 24 ? 4 : v < 168 ? 3 : v < 720 ? 2 : 1) },
    { key: 'cfr', label: 'Releases that needed a fix', unit: '', fmt: (v) => O.fmt.pct(v), band: (v) => (v <= 0.05 ? 4 : v <= 0.1 ? 3 : v <= 0.15 ? 2 : 1) },
    { key: 'restore_h', label: 'Time to fix a broken main branch', unit: '', fmt: (v) => O.fmt.hours(v), band: (v) => (v < 1 ? 4 : v < 24 ? 3 : v < 168 ? 2 : 1) },
  ];
  D.badge = (b) => (b ? O.el('span', { class: 'band b' + b }, O.el('i'), D.BAND[b]) : O.el('span', { class: 'band na' }, O.el('i'), 'no data'));
  D.init = function (data) {
    const sig = data.signals;
    if (!sig || !sig.repos) { O.$('#dora-answer').textContent = 'No delivery data in this report.'; return; }
    D.repos = sig.repos.map((r) => Object.assign({ repo: r.repo, short: O.fmt.short(r.repo) }, r.dora));
    D.since = Date.parse(sig.since); D.until = Date.parse(sig.until);
    const withSource = D.repos.filter((r) => r.source);
    const attempts = O.sum(D.repos, (r) => r.cfr_attempts || 0), failed = O.sum(D.repos, (r) => r.cfr_failed || 0);
    D.fleet = {
      deploys_per_week: O.median(withSource.map((r) => r.deploys_per_week)),
      lead_time_h: O.median(D.repos.map((r) => r.lead_time_h)),
      cfr: attempts ? failed / attempts : null,
      restore_h: O.median(D.repos.map((r) => r.restore_h)),
      none: D.repos.length - withSource.length, failed, attempts,
    };
    const draw = () => { O.safe('dora-tiles', D.tiles); O.safe('dora-table', D.table); O.safe('dora-cadence', D.cadence); };
    draw();
    O.on('repo', draw);
  };
  D.tiles = function () {
    const sel = O.state.repo && D.repos.find((r) => r.repo === O.state.repo);
    const src = sel || D.fleet;
    const box = O.clear(O.$('#dora-tiles'));
    const notes = {
      deploys_per_week: sel ? (sel.source ? `${sel.deployments} ${sel.source === 'releases' ? 'releases' : 'deploys'} in the window` : 'No releases or deploys found') : `The typical repository. ${D.fleet.none} show no releases at all.`,
      lead_time_h: sel ? `${sel.lead_samples} pull requests reached a release` : 'Typical time from opening a pull request until it is released.',
      cfr: sel ? (sel.cfr_attempts ? `${sel.cfr_failed} of ${sel.cfr_attempts}` : 'No releases to judge') : `${D.fleet.failed} of ${D.fleet.attempts} were followed by a quick fix or revert.`,
      restore_h: sel ? (sel.restore_samples ? `${sel.restore_samples} times main broke and was fixed` : 'Main did not break, or no data') : 'Typical time until a failing main branch is green again.',
    };
    for (const m of D.metrics) {
      const v = src[m.key];
      const band = v == null || (m.key === 'deploys_per_week' && sel && !sel.source) ? null : m.band(v);
      box.appendChild(O.el('div', { class: 'tile' },
        O.el('div', { class: 'l', text: m.label }),
        O.el('div', { class: 'v' }, v == null ? '–' : m.fmt(v), m.unit ? O.el('small', { text: m.unit }) : null),
        O.el('div', { class: 'steps', 'aria-label': band ? `Rated ${D.BAND[band]}` : 'Not rated' }, [1, 2, 3, 4].map((b) => O.el('span', { class: b === band ? `on b${b}` : null, text: D.BAND[b] }))),
        O.el('div', { class: 'd', text: notes[m.key] })));
    }
    const ratings = D.metrics.map((m) => [m.label.toLowerCase(), D.fleet[m.key] == null ? null : m.band(D.fleet[m.key])]).filter(([, b]) => b);
    const best = ratings.slice().sort((a, b) => b[1] - a[1])[0];
    const worst = ratings.slice().sort((a, b) => a[1] - b[1])[0];
    O.put(O.clear(O.$('#dora-answer')), sel ? ['Showing ', O.el('b', { text: sel.short }), '. Clear the selection to see all repositories.'] : [
      'Strongest: ', O.el('b', { text: `${best[0]} (${D.BAND[best[1]]})` }), '. Weakest: ', O.el('b', { text: `${worst[0]} (${D.BAND[worst[1]]})` }), '.',
      ' These are estimates from releases and CI, not production data.',
    ]);
  };
  D.table = function () {
    const cell = (m, r) => {
      const v = r[m.key];
      if (v == null || (m.key === 'deploys_per_week' && !r.source)) return O.el('span', { class: 'muted', text: m.key === 'deploys_per_week' ? 'no releases' : '–' });
      return O.el('span', null, m.fmt(v), '  ', D.badge(m.band(v)));
    };
    O.table(O.$('#dora-table'), [
      { key: 'short', label: 'Repository' },
      { key: 'deploys_per_week', label: 'Ships per week', num: true, render: (r) => cell(D.metrics[0], r) },
      { key: 'lead_time_h', label: 'Pull request to release', num: true, render: (r) => cell(D.metrics[1], r) },
      { key: 'cfr', label: 'Needed a fix', num: true, render: (r) => cell(D.metrics[2], r) },
      { key: 'restore_h', label: 'Main fixed within', num: true, render: (r) => cell(D.metrics[3], r) },
    ], D.repos, { sort: 'deploys_per_week', onRow: (r) => O.selectRepo(r.repo), isSelected: (r) => r.repo === O.state.repo });
  };
  D.cadence = function () {
    const box = O.$('#dora-cadence');
    const rows = D.repos.filter((r) => r.deployments).sort((a, b) => b.deployments - a.deployments);
    const none = D.repos.filter((r) => !r.deployments).map((r) => r.short);
    const w = O.width(box), left = Math.min(190, w * 0.4), m = { l: left, r: 12, t: 6, b: 26 }, rh = 24;
    const h = m.t + m.b + rows.length * rh;
    const x = O.linear(D.since, D.until, m.l, w - m.r);
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Releases and deploys per repository over time' });
    rows.forEach((r, i) => {
      const y = m.t + i * rh;
      const sel = r.repo === O.state.repo;
      svg.appendChild(O.svg('text', { x: m.l - 12, y: y + 16, 'text-anchor': 'end', class: sel ? 'lbl-strong' : 'lbl', text: r.short.length > 24 ? r.short.slice(0, 23) + '…' : r.short }));
      svg.appendChild(O.svg('line', { x1: m.l, x2: w - m.r, y1: y + 12, y2: y + 12, class: 'gridline' }));
      const color = r.source === 'releases' ? O.color.c1 : O.color.c3;
      for (const t of r.deploy_times || []) svg.appendChild(O.svg('circle', { cx: x(Date.parse(t)), cy: y + 12, r: 5, fill: color }));
    });
    for (let d = Math.ceil(D.since / 864e5 / 7) * 7 * 864e5; d <= D.until; d += 7 * 864e5) svg.appendChild(O.svg('text', { x: x(d), y: h - 6, 'text-anchor': 'middle', text: O.fmt.dayShort(d) }));
    O.clear(box).appendChild(svg);
    if (none.length) box.appendChild(O.el('p', { class: 'panel-foot', text: `No releases or deploys: ${none.join(', ')}.` }));
    O.legend(O.$('#dora-cadence-legend'), [{ label: 'Release', color: O.color.c1 }, { label: 'Deploy', color: O.color.c3 }]);
  };

  // ================================================================ inside a job
  const R = (O.rec = { filter: 'all', query: '', t: 0, playing: false, kinds: 'important' });
  R.KIND = { err: 'Error', fail: 'Test failed', warn: 'Warning', cache: 'Cache', test: 'Tests', cmd: 'Command', sys: 'Runner', gap: 'Waiting', dock: 'Docker', out: 'Output' };
  R.IMPORTANT = new Set(['err', 'fail', 'warn', 'test', 'cmd', 'cache', 'gap', 'dock']);
  R.init = function (data) {
    R.jobs = ((data.logs && data.logs.jobs) || []).slice();
    if (!R.jobs.length) { O.$('#job-answer').textContent = 'No job logs in this report.'; return; }
    const tools = O.$('#joblist-tools');
    const seg = O.el('div', { class: 'seg' });
    O.seg(seg, [{ id: 'all', label: 'All' }, { id: 'failure', label: 'Failed' }, { id: 'success', label: 'Passed' }], R.filter, (id) => { R.filter = id; R.list(); }, 'Job result');
    tools.appendChild(seg);
    tools.appendChild(O.el('input', { type: 'search', placeholder: 'Find a job', 'aria-label': 'Find a job', style: { flex: '1 1 110px', minWidth: '0' }, on: { input: (e) => { R.query = e.target.value.toLowerCase(); R.list(); } } }));
    const ltools = O.$('#log-tools');
    R.playBtn = O.el('button', { class: 'btn primary', type: 'button', text: '▶ Play', on: { click: () => R.play(!R.playing) } });
    ltools.appendChild(R.playBtn);
    ltools.appendChild(O.el('select', { 'aria-label': 'Which lines', on: { change: (e) => { R.kinds = e.target.value; R.feed(); } } },
      [['important', 'Important lines'], ['problems', 'Errors and warnings'], ['all', 'Everything']].map(([v, l]) => O.el('option', { value: v, text: l }))));
    O.on('repo', () => R.list());
    let width = 0;
    new ResizeObserver(() => { const w = O.$('#job-steps').clientWidth; if (R.job && Math.abs(w - width) > 4) { width = w; O.safe('job-steps', R.steps); O.safe('job-split', R.split); } }).observe(O.$('#job-steps'));
    R.list();
    const pick = R.jobs.filter((j) => j.failure && j.failure.class === 'test' && j.dur <= 900).sort((a, b) => b.lines - a.lines)[0];
    R.open((pick || R.jobs.find((j) => j.failure) || R.jobs[0]).id, false);
  };
  R.visible = () => R.jobs.filter((j) => (R.filter === 'all' || j.result === R.filter) && (!O.state.repo || j.repo === O.state.repo) && (!R.query || `${j.repo} ${j.workflow} ${j.job}`.toLowerCase().includes(R.query)));
  R.list = function () {
    const box = O.clear(O.$('#joblist'));
    const jobs = R.visible();
    if (!jobs.length) { box.appendChild(O.el('div', { class: 'empty', style: { padding: '14px' }, text: 'No jobs match.' })); return; }
    for (const [repo, group] of O.groupBy(jobs.sort((a, b) => a.repo.localeCompare(b.repo) || (a.result === 'failure' ? -1 : 1)), (j) => j.repo)) {
      box.appendChild(O.el('div', { class: 'grp', text: O.fmt.short(repo) }));
      for (const j of group) {
        box.appendChild(O.el('button', { type: 'button', role: 'listitem', 'aria-current': String(j.id === (R.job && R.job.id)), on: { click: () => R.open(j.id, true) } },
          j.result === 'success' ? O.good('', 'Passed') : O.bad('', 'Failed'),
          O.el('span', { class: 'nm' }, j.job, O.el('small', { text: j.workflow })),
          O.el('span', { class: 'd', text: O.fmt.dur(j.dur) })));
      }
    }
  };
  R.open = function (id, scroll) {
    const job = R.jobs.find((j) => j.id === id);
    if (!job) return;
    R.play(false);
    R.job = job;
    R.span = Math.max(job.dur, (job.trace.length * job.bucket) || 1, ...job.steps.map((s) => s.t + s.d));
    R.t = R.span;
    O.$('#job-title').textContent = `${O.fmt.short(job.repo)} · ${job.job}`;
    O.safe('job-answer', R.answer);
    O.safe('job-split', R.split);
    O.safe('job-steps', R.steps);
    O.safe('job-facts', R.facts);
    O.safe('feed', R.feed);
    R.list();
    if (scroll) O.$('#job').scrollIntoView({ behavior: O.reduced ? 'auto' : 'smooth', block: 'start' });
  };
  R.answer = function () {
    const job = R.job;
    const total = Math.max(1, O.sum(Object.values(job.act)));
    const top = Object.entries(job.act).sort((a, b) => b[1] - a[1])[0];
    O.put(O.clear(O.$('#job-answer')), [
      O.el('b', { text: `${job.result === 'failure' ? 'Failed' : 'Passed'} after ${O.fmt.dur(job.dur)}.` }),
      top && top[1] ? ` ${O.ACT[top[0]].label} took most of the time (${O.fmt.pct(top[1] / total)}).` : '',
      job.quiet >= 20 ? ` For ${O.fmt.dur(job.quiet)} it printed nothing, usually while waiting on a compile, download or test.` : '',
    ]);
    const box = O.clear(O.$('#job-problem'));
    if (job.failure) {
      box.appendChild(O.el('div', { class: 'problem' },
        O.el('div', { class: 'what', text: `What went wrong: ${(O.diag.CLASS[job.failure.class] || job.failure.class).toLowerCase()}, in “${job.failure.step}”` }),
        O.el('div', { class: 'why', text: O.diag.tidy(job.failure.signature) || 'No single error line stood out; see the log.' })));
    }
  };
  R.split = function () {
    const job = R.job;
    const parts = Object.keys(O.ACT).map((k) => ({ label: O.ACT[k].label, value: job.act[k] || 0, color: O.ACT[k].color, tip: [['Seconds', O.fmt.int(job.act[k] || 0)]] }));
    O.split(O.$('#job-split'), parts);
    O.legend(O.$('#job-legend'), parts.filter((p) => p.value).map((p) => ({ label: p.label, color: p.color })));
  };
  R.steps = function () {
    const job = R.job, box = O.$('#job-steps');
    const steps = job.steps.filter((s) => s.d >= 1 || s.result === 'failure');
    const w = O.width(box), narrow = w < 520;
    const m = { l: narrow ? 10 : Math.min(200, w * 0.32), r: 60, t: 22, b: 8 }, rh = 26;
    const h = m.t + steps.length * rh + m.b;
    const x = O.linear(0, R.span, m.l, w - m.r);
    R.x = x;
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': `Steps of ${job.job} with their durations` });
    svg.appendChild(O.svg('defs', null, O.svg('pattern', { id: 'quiet', width: 7, height: 7, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      O.svg('rect', { width: 7, height: 7, fill: O.alpha(O.color.warn, 0.12) }), O.svg('line', { x1: 0, y1: 0, x2: 0, y2: 7, stroke: O.alpha(O.color.warn, 0.7), 'stroke-width': 2 }))));
    for (const v of O.ticks(0, R.span, narrow ? 3 : 6)) {
      svg.appendChild(O.svg('line', { x1: x(v), x2: x(v), y1: m.t - 4, y2: h - m.b, class: 'gridline' }));
      svg.appendChild(O.svg('text', { x: x(v), y: m.t - 8, 'text-anchor': 'middle', text: O.fmt.dur(v) }));
    }
    const chars = Math.max(8, Math.floor((m.l - 14) / 7));
    steps.forEach((s, i) => {
      const y = m.t + i * rh;
      const failed = s.result === 'failure';
      const name = s.name.length > chars ? s.name.slice(0, chars - 1) + '…' : s.name;
      if (!narrow) svg.appendChild(O.svg('text', { x: m.l - 12, y: y + 17, 'text-anchor': 'end', class: failed ? 'lbl-strong' : 'lbl', style: failed ? `fill:${O.color['crit-ink']}` : null, text: name }));
      const x0 = x(s.t), x1 = Math.max(x0 + 3, x(s.t + s.d));
      const act = O.ACT[s.cat] || O.ACT.other;
      const bar = O.svg('rect', { x: x0, y: y + 5, width: x1 - x0, height: rh - 10, rx: 4, fill: failed ? O.color.crit : act.color });
      bar.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(s.name, [['Took', O.fmt.dur(s.d)], ['Started at', O.fmt.dur(s.t)], ['Kind of work', act.label], ['Result', failed ? 'failed' : s.result || '–']], s.cmd ? 'Runs: ' + s.cmd : null)));
      bar.addEventListener('pointerleave', () => O.tip.hide());
      bar.addEventListener('click', () => R.seek(s.t));
      svg.appendChild(bar);
      for (const g of (job.gaps || []).filter((g) => g.step === s.name)) {
        const gx = x(g.t), gw = Math.max(3, x(g.t + g.d) - gx);
        const r = O.svg('rect', { x: gx, y: y + 5, width: gw, height: rh - 10, rx: 3, fill: 'url(#quiet)' });
        r.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(`${Math.round(g.d)} s with no output`, [['In step', s.name]], g.before ? `Last line before it: ${g.before}` : null)));
        r.addEventListener('pointerleave', () => O.tip.hide());
        svg.appendChild(r);
      }
      const label = narrow ? `${name} · ${O.fmt.dur(s.d)}` : O.fmt.dur(s.d);
      svg.appendChild(O.svg('text', { x: Math.min(x1 + 6, w - m.r + 6), y: y + 17, class: 'lbl', text: narrow && x1 > w - 140 ? O.fmt.dur(s.d) : label }));
    });
    R.head = O.svg('line', { x1: x(R.t), x2: x(R.t), y1: m.t - 4, y2: h - m.b, stroke: O.color.ink, 'stroke-width': 1.5, 'stroke-dasharray': '4 3', style: 'pointer-events:none', opacity: R.t >= R.span ? 0 : 0.8 });
    svg.appendChild(R.head);
    O.clear(box).appendChild(svg);
    if ((job.gaps || []).length) box.appendChild(O.el('div', { class: 'legend' }, O.el('span', null, O.el('i', { style: { background: `repeating-linear-gradient(45deg, ${O.alpha(O.color.warn, 0.7)} 0 2px, transparent 2px 5px)` } }), 'No output for 20 s or more'), O.el('span', null, O.el('i', { style: { background: O.color.crit } }), 'Failed step')));
  };
  R.facts = function () {
    const j = R.job, box = O.clear(O.$('#job-facts'));
    const t = j.tests, cache = j.cache || {};
    const writes = (j.token && j.token.write) || [];
    const items = [
      ['Tests', t ? `${O.fmt.int(t.total)} run, ${t.failed} failed` : 'no test summary in the log'],
      ['Cache', cache.hit || cache.miss ? `${cache.hit || 0} hit, ${cache.miss || 0} missed` : 'not used'],
      ['Downloads', `${O.fmt.int(j.downloads)} files${j.pulls ? `, ${j.pulls} Docker images` : ''}`],
      ['Warnings', O.fmt.int(j.warnings)],
      ['Token could write to', writes.length >= 10 ? `everything (${writes.length} areas)` : writes.length ? writes.join(', ') : 'nothing (read-only)'],
      ['Machine', j.runner && j.runner.image ? j.runner.image : (j.labels || []).join(', ') || '–'],
    ];
    for (const [k, v] of items) box.appendChild(O.el('div', null, O.el('span', { text: k }), O.el('b', { text: v })));
  };
  R.rows = () => R.job.feed.filter(([, kind]) => R.kinds === 'all' || (R.kinds === 'important' && R.IMPORTANT.has(kind)) || (R.kinds === 'problems' && (kind === 'err' || kind === 'fail' || kind === 'warn')));
  R.feed = function () {
    const box = O.clear(O.$('#feed'));
    const rows = R.rows();
    R.feedRows = rows.map(([t, kind, text]) => {
      const node = O.el('div', { on: { click: () => R.seek(t) } },
        O.el('span', { class: 't', text: `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(Math.floor(t % 60)).padStart(2, '0')}` }),
        O.el('span', { class: 'k k-' + kind, text: R.KIND[kind] || kind }),
        O.el('span', { class: 'x', text: kind === 'gap' ? text.replace(' without output · after:', ' with no output, after') : O.diag.tidy(text) }));
      box.appendChild(node);
      return { t, node };
    });
    if (!rows.length) box.appendChild(O.el('div', { class: 'empty', style: { padding: '12px' }, text: 'No lines of this kind.' }));
    R.mark();
  };
  R.mark = function () {
    if (!R.feedRows) return;
    let now = null;
    for (const row of R.feedRows) {
      const future = row.t > R.t;
      row.node.classList.toggle('future', R.playing && future);
      row.node.classList.remove('now');
      if (!future) now = row;
    }
    if (now && R.t < R.span) {
      now.node.classList.add('now');
      const box = O.$('#feed');
      box.scrollTop = Math.max(0, now.node.offsetTop - box.clientHeight + 48);
    }
    if (R.head && R.x) { R.head.setAttribute('x1', R.x(R.t)); R.head.setAttribute('x2', R.x(R.t)); R.head.setAttribute('opacity', R.t >= R.span ? 0 : 0.8); }
  };
  R.seek = function (t) { R.t = Math.max(0, Math.min(R.span, t)); R.mark(); };
  R.play = function (on) {
    if (!R.job) return;
    if (on && R.t >= R.span * 0.99) R.t = 0;
    R.playing = on;
    if (R.playBtn) R.playBtn.textContent = on ? '❚❚ Pause' : '▶ Play';
    R.mark();
  };
  /** Called every frame while playing: a whole job plays in about 40 seconds. */
  R.frame = function (dt) {
    if (!R.playing || !R.job) return;
    R.t += (dt / 1000) * Math.max(4, R.span / 40);
    if (R.t >= R.span) { R.t = R.span; R.play(false); }
    R.mark();
  };

  // ================================================================ health check
  const X = (O.diag = {});
  X.CLASS = {
    test: 'Tests failed', lint: 'Lint or formatting', build: 'Build error', deps: 'Dependency problem',
    policy: 'PR title or changelog check', config: 'Broken workflow setup', infra: 'Network or rate limit',
    timeout: 'Timed out', upstream: 'Another job failed first', other: 'Other',
  };
  X.CODE = new Set(['test', 'lint', 'build']);
  /** pre-commit's "ruff format.......Failed" reads better as "ruff format … Failed". */
  X.tidy = (text) => (text || '').replace(/\.{4,}/g, ' … ');
  X.init = function (data) {
    X.jobs = (data.logs && data.logs.jobs) || [];
    X.repos = data.repos || [];
    const draw = () => { O.safe('causes', X.causes); O.safe('time-split', X.time); O.safe('checks', X.checks); };
    draw();
    O.on('repo', draw);
    let width = 0;
    new ResizeObserver(() => { const w = O.$('#health').clientWidth; if (Math.abs(w - width) > 4) { width = w; O.safe('causes', X.causes); O.safe('time-split', X.time); } }).observe(O.$('#health'));
  };
  X.causes = function () {
    const box = O.clear(O.$('#causes'));
    const failed = X.jobs.filter((j) => j.failure && (!O.state.repo || j.repo === O.state.repo));
    if (!failed.length) { box.appendChild(O.el('div', { class: 'empty', text: 'No failed jobs in the sample.' })); return; }
    const groups = [...O.groupBy(failed, (j) => j.failure.class).entries()].sort((a, b) => b[1].length - a[1].length);
    const chart = O.el('div', { class: 'chart' });
    box.appendChild(chart);
    O.hbars(chart, groups.map(([cls, jobs]) => ({
      label: X.CLASS[cls] || cls, value: jobs.length, text: String(jobs.length), color: X.CODE.has(cls) ? O.color.serious : O.color.crit,
      tip: jobs.slice(0, 3).map((j) => [O.fmt.short(j.repo), j.job.slice(0, 28)]), note: 'Open “Inside a job” to read one.',
    })), { left: 230, right: 40, label: 'Failed jobs by cause' });
    box.appendChild(O.el('div', { class: 'legend' }, O.el('span', null, O.el('i', { style: { background: O.color.serious } }), 'A problem in the code'), O.el('span', null, O.el('i', { style: { background: O.color.crit } }), 'A problem around the code')));
    const outside = failed.filter((j) => !X.CODE.has(j.failure.class)).length;
    box.appendChild(O.el('p', { class: 'answer' }, O.el('b', { text: `${outside} of ${failed.length}` }), ' failures had nothing to do with the code itself, so a better setup would have prevented them.'));
    const details = O.el('details', { style: { marginTop: '10px' } }, O.el('summary', { text: 'Show an example of each' }));
    for (const [cls, jobs] of groups) {
      const j = jobs[0];
      details.appendChild(O.el('div', { style: { margin: '8px 0', fontSize: '14px' } },
        O.el('div', { style: { color: O.color.ink }, text: `${X.CLASS[cls] || cls} · ${O.fmt.short(j.repo)}` }),
        O.el('button', { class: 'linkbtn', type: 'button', style: { textAlign: 'left', fontFamily: 'var(--mono)', fontSize: '12.5px' }, text: X.tidy(j.failure.signature || j.failure.step).slice(0, 140), title: 'Open this job', on: { click: () => O.rec.open(j.id, true) } })));
    }
    box.appendChild(details);
  };
  X.time = function () {
    const box = O.$('#time-split');
    const ok = X.jobs.filter((j) => j.result === 'success');
    const order = Object.keys(O.ACT);
    O.legend(O.$('#time-legend'), order.map((k) => ({ label: O.ACT[k].label, color: O.ACT[k].color })));
    const rows = [...O.groupBy(ok, (j) => j.repo).entries()].map(([repo, jobs]) => {
      const act = {};
      for (const j of jobs) for (const [k, v] of Object.entries(j.act)) act[k] = (act[k] || 0) + v;
      return { repo, label: O.fmt.short(repo), act, total: O.sum(Object.values(act)), jobs: jobs.length };
    }).filter((r) => r.total);
    const all = { repo: null, label: 'All repositories', act: {}, total: 0, jobs: ok.length };
    for (const r of rows) { for (const [k, v] of Object.entries(r.act)) all.act[k] = (all.act[k] || 0) + v; all.total += r.total; }
    rows.sort((a, b) => (b.act.test || 0) / b.total - (a.act.test || 0) / a.total);
    const shown = [all, ...rows.filter((r) => !O.state.repo || r.repo === O.state.repo)];
    const w = O.width(box), left = Math.min(190, w * 0.36), m = { l: left, r: 8 }, rh = 30;
    const h = shown.length * rh + 8;
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Share of job time by kind of work, per repository' });
    shown.forEach((r, i) => {
      const y = i * rh + (i ? 8 : 0);
      svg.appendChild(O.svg('text', { x: m.l - 12, y: y + 19, 'text-anchor': 'end', class: r.repo === null ? 'lbl-strong' : 'lbl', text: r.label.length > 24 ? r.label.slice(0, 23) + '…' : r.label }));
      let at = 0;
      for (const k of order) {
        const v = r.act[k] || 0;
        if (!v) continue;
        const x0 = m.l + (at / r.total) * (w - m.l - m.r), x1 = m.l + ((at + v) / r.total) * (w - m.l - m.r);
        const seg = O.svg('rect', { x: x0, y: y + 6, width: Math.max(1, x1 - x0 - 2), height: rh - 12, rx: 4, fill: O.ACT[k].color });
        seg.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(`${r.label}: ${O.ACT[k].label.toLowerCase()}`, [['Share of job time', O.fmt.pct(v / r.total, 1)], ['Jobs read', String(r.jobs)]])));
        seg.addEventListener('pointerleave', () => O.tip.hide());
        svg.appendChild(seg);
        if (x1 - x0 > 46) svg.appendChild(O.svg('text', { x: x0 + 7, y: y + 19.5, style: `fill:${O.inkOn(O.ACT[k].color)};font-size:12px;font-weight:600;pointer-events:none`, text: O.fmt.pct(v / r.total) }));
        at += v;
      }
    });
    O.clear(box).appendChild(svg);
    const share = (k) => (all.act[k] || 0) / Math.max(1, all.total);
    O.put(O.clear(O.$('#time-answer')), ['Running tests takes ', O.el('b', { text: O.fmt.pct(share('test')) }), ' of the time and building ', O.el('b', { text: O.fmt.pct(share('build')) }), '. Setup and downloading dependencies take ', O.el('b', { text: O.fmt.pct(share('setup') + share('deps')) }), ', which caching can shrink.']);
  };
  X.repoLogs = function (repo) {
    const jobs = X.jobs.filter((j) => j.repo === repo);
    return {
      jobs: jobs.length,
      writes: Math.max(0, ...jobs.map((j) => ((j.token && j.token.write) || []).length)),
      hits: O.sum(jobs, (j) => j.cache.hit), miss: O.sum(jobs, (j) => j.cache.miss),
      depr: O.sum(jobs, (j) => (j.warn_kinds && j.warn_kinds.deprecation) || 0),
    };
  };
  X.checks = function () {
    const rows = X.repos.filter((r) => !O.state.repo || r.repo === O.state.repo).map((r) => Object.assign({ short: O.fmt.short(r.repo) }, r, { logs: X.repoLogs(r.repo) }));
    const ratio = (n, d) => (d ? n / d : null);
    O.table(O.$('#checks'), [
      { key: 'short', label: 'Repository' },
      { key: 'token', label: 'CI token rights', value: (r) => r.logs.writes, render: (r) => (!r.logs.jobs ? O.na('no logs') : r.logs.writes >= 10 ? O.bad('can write everything', 'In a sampled job the GITHUB_TOKEN could write to every area, including code and packages.') : r.logs.writes ? O.warn(`writes ${r.logs.writes} area${r.logs.writes > 1 ? 's' : ''}`) : O.good('read-only')) },
      { key: 'pinned', label: 'Actions pinned', value: (r) => ratio(r.hyg_pinned_actions, r.hyg_external_actions), render: (r) => { const v = ratio(r.hyg_pinned_actions, r.hyg_external_actions); const text = `${r.hyg_pinned_actions || 0} of ${r.hyg_external_actions || 0}`; return v == null ? O.na('none used') : v >= 0.9 ? O.good(text) : v >= 0.3 ? O.warn(text) : O.bad(text, 'Third-party actions not pinned to an exact version can change under you.'); } },
      { key: 'timeouts', label: 'Time limits set', value: (r) => ratio(r.hyg_timeouts, r.hyg_files), render: (r) => { const v = ratio(r.hyg_timeouts, r.hyg_files); const text = `${r.hyg_timeouts || 0} of ${r.hyg_files || 0} workflows`; return v == null ? O.na() : v >= 0.9 ? O.good(text) : v >= 0.5 ? O.warn(text) : O.bad(text, 'Without a time limit a stuck job runs for 6 hours.'); } },
      { key: 'cache', label: 'Cache works', value: (r) => ratio(r.logs.hits, r.logs.hits + r.logs.miss), render: (r) => { const n = r.logs.hits + r.logs.miss; if (!n) return O.na('not used'); const v = r.logs.hits / n; const text = `${r.logs.hits} of ${n} hit`; return v >= 0.8 ? O.good(text) : v >= 0.5 ? O.warn(text) : O.bad(text); } },
      { key: 'depr', label: 'Outdated actions', value: (r) => r.logs.depr, render: (r) => (!r.logs.jobs ? O.na('no logs') : r.logs.depr ? O.warn(`${r.logs.depr} warnings`, 'GitHub warned that an action or feature used here is being retired.') : O.good('none')) },
    ], rows, { sort: 'short', desc: false, onRow: (r) => O.selectRepo(r.repo), isSelected: (r) => r.repo === O.state.repo });
  };

  // ================================================================ test audit
  const T = (O.tests = {});
  T.WASTE = [['in_security_scan', 'inside a security scan'], ['double_trigger', 'push and pull request both ran it'], ['cross_workflow', 'another workflow ran it'], ['same_workflow', 'another job ran it'], ['within_job', 'twice in one job']];
  T.waste = (e) => O.sum(T.WASTE, ([k]) => (e && e[k]) || 0);
  T.first = (a) => Math.max(0, a.tests_run - O.sum(Object.values(a.extra || {})));
  T.init = function (data) {
    T.data = data.tests;
    if (!T.data || !T.data.repos) { O.$('#tests-kpis').appendChild(O.el('div', { class: 'empty', text: 'No test audit in this report.' })); return; }
    T.repos = T.data.repos;
    O.safe('tests-kpis', T.kpis);
    O.safe('tests-bars', T.bars);
    O.safe('tests-findings', T.findings);
    const pick = O.$('#tests-repo');
    for (const a of T.repos) pick.appendChild(O.el('option', { value: a.repo, text: O.fmt.short(a.repo) }));
    pick.value = T.repos.slice().sort((a, b) => T.waste(b.extra) - T.waste(a.extra) || b.tests_run - a.tests_run)[0].repo;
    pick.addEventListener('change', () => O.safe('tests-detail', T.detail));
    O.safe('tests-detail', T.detail);
    O.legend(O.$('#tests-legend'), [
      { label: 'Each test once', color: O.alpha(O.color.ink, 0.35) },
      { label: 'Repeated for another version', color: O.color.c1 },
      { label: 'Repeated for no benefit', color: O.color.c2 },
    ]);
    O.on('repo', (repo) => { if (repo && T.repos.some((a) => a.repo === repo)) { pick.value = repo; O.safe('tests-detail', T.detail); } O.safe('tests-bars', T.bars); });
    let width = 0;
    new ResizeObserver(() => { const w = O.$('#tests').clientWidth; if (Math.abs(w - width) > 4) { width = w; O.safe('tests-bars', T.bars); } }).observe(O.$('#tests'));
  };
  T.kpis = function () {
    const s = T.data.summary, e = s.extra || {};
    const box = O.clear(O.$('#tests-kpis'));
    box.appendChild(O.kpi('Test runs', O.fmt.int(s.tests_run), `on ${s.repos} pull request commits`));
    box.appendChild(O.kpi('Different tests', O.fmt.int(s.unique_tests), `each ran ${(s.tests_run / Math.max(1, s.unique_tests)).toFixed(1)} times on average`));
    box.appendChild(O.kpi('Repeats with no benefit', O.fmt.int(T.waste(e)), 'same test, nothing new learned'));
    box.appendChild(O.kpi('Extra time for versions', O.fmt.mins(s.matrix_extra_minutes), 'testing every Python or Java version'));
  };
  T.bars = function () {
    const box = O.$('#tests-bars');
    const rows = T.repos.filter((a) => a.tests_run).sort((a, b) => b.tests_run - a.tests_run);
    const quiet = T.repos.filter((a) => !a.tests_run).map((a) => O.fmt.short(a.repo));
    const w = O.width(box), left = Math.min(190, w * 0.36), m = { l: left, r: 56 }, rh = 28;
    const h = rows.length * rh + 26;
    const max = Math.max(1, ...rows.map((a) => a.tests_run));
    const x = O.linear(0, max, m.l, w - m.r);
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Test runs per repository, split into first runs and repeats' });
    rows.forEach((a, i) => {
      const y = i * rh;
      const sel = a.repo === O.state.repo;
      svg.appendChild(O.svg('text', { x: m.l - 12, y: y + 18, 'text-anchor': 'end', class: sel ? 'lbl-strong' : 'lbl', text: O.fmt.short(a.repo).slice(0, 24) }));
      const parts = [
        ['Each test once', T.first(a), O.alpha(O.color.ink, 0.35), []],
        ['Repeated for another version', a.extra.matrix || 0, O.color.c1, []],
        ['Repeated for no benefit', T.waste(a.extra), O.color.c2, T.WASTE.filter(([k]) => a.extra[k]).map(([k, label]) => [label, O.fmt.int(a.extra[k])])],
      ];
      let at = 0;
      for (const [label, v, color, why] of parts) {
        if (!v) continue;
        const x0 = x(at), x1 = x(at + v);
        const rect = O.svg('rect', { x: x0, y: y + 5, width: Math.max(2, x1 - x0 - 2), height: rh - 10, rx: 4, fill: color });
        rect.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(`${O.fmt.short(a.repo)}: ${label.toLowerCase()}`, [['Test runs', O.fmt.int(v)], ...why])));
        rect.addEventListener('pointerleave', () => O.tip.hide());
        rect.addEventListener('click', () => { O.$('#tests-repo').value = a.repo; O.safe('tests-detail', T.detail); });
        svg.appendChild(rect);
        at += v;
      }
      svg.appendChild(O.svg('text', { x: x(a.tests_run) + 6, y: y + 18, class: 'lbl', text: `×${(a.factor || 1).toFixed(1)}` }));
    });
    for (const v of O.ticks(0, max, 4)) svg.appendChild(O.svg('text', { x: x(v), y: h - 6, 'text-anchor': 'middle', text: O.fmt.big(v) }));
    O.clear(box).appendChild(svg);
    box.appendChild(O.el('p', { class: 'panel-foot', text: `×2.0 means each test ran twice. ${quiet.length ? `No test names in the logs of: ${quiet.join(', ')}.` : ''}` }));
  };
  T.findings = function () {
    const box = O.clear(O.$('#tests-findings'));
    const items = [];
    for (const a of T.repos) {
      const name = O.fmt.short(a.repo);
      const scans = (a.redundant || []).filter((r) => r.relation === 'security-scan');
      if (scans.length) items.push(['crit', `${name}: the security scan runs the whole test suite`, `${O.fmt.int(a.extra.in_security_scan)} test runs per pull request that the scan does not need.`]);
      if (a.extra.double_trigger) items.push(['crit', `${name}: CI runs twice for every push to a pull request`, `Once for "push" and once for "pull_request": ${O.fmt.int(a.extra.double_trigger)} repeated test runs.`]);
      if (a.extra.cross_workflow) items.push(['crit', `${name}: two workflows run the same tests`, `${O.fmt.int(a.extra.cross_workflow)} repeated test runs.`]);
      for (const mtx of (a.matrix || []).filter((x) => x.extra_minutes >= 15)) items.push(['warn', `${name}: tests run for ${mtx.legs} versions on every pull request`, `${mtx.variants.slice(0, 5).join(', ')}: ${O.fmt.mins(mtx.extra_minutes)} extra each time. Test the newest and oldest on PRs, all of them after merge.`]);
      if (a.always_skipped >= 2) items.push(['info', `${name}: ${a.always_skipped} tests never run`, 'They are skipped in every job, often because CI lacks the credentials they need.']);
      if (a.blind_minutes >= 30) items.push(['info', `${name}: test jobs don't say which tests ran`, `${O.fmt.mins(a.blind_minutes)} of test jobs where repeats and flaky tests can't be seen.`]);
    }
    const rank = { crit: 0, warn: 1, info: 2 };
    items.sort((p, q) => rank[p[0]] - rank[q[0]]);
    if (!items.length) { box.appendChild(O.el('div', { class: 'empty', text: 'No repeated test runs found.' })); return; }
    const list = O.el('div', { class: 'findings' });
    items.forEach(([lvl, title, detail], i) => {
      list.appendChild(O.el('div', { class: 'finding', hidden: i >= 5 },
        lvl === 'crit' ? O.status('crit', '✕', '') : lvl === 'warn' ? O.status('warn', '!', '') : O.status('na', 'i', ''),
        O.el('div', null, O.el('div', { class: 't', text: title }), O.el('div', { class: 'd', text: detail }))));
    });
    box.appendChild(list);
    if (items.length > 5) {
      const more = O.el('button', { class: 'linkbtn', type: 'button', style: { marginTop: '10px' }, text: `Show ${items.length - 5} more` });
      more.addEventListener('click', () => { for (const n of list.children) n.hidden = false; more.remove(); });
      box.appendChild(more);
    }
  };
  T.detail = function () {
    const a = T.repos.find((x) => x.repo === O.$('#tests-repo').value);
    if (!a) return;
    O.put(O.clear(O.$('#tests-detail-answer')), [
      `Commit ${a.sha}${a.pr ? ' (pull request #' + a.pr + ')' : ''}: ${a.jobs} jobs read. `,
      a.tests_run ? [O.el('b', { text: `${O.fmt.int(a.tests_run)} test runs of ${O.fmt.int(a.unique_tests)} tests` }), '.'] : 'No test names found in these logs.',
    ]);
    O.table(O.$('#tests-detail'), [
      { key: 'workflow', label: 'Workflow' },
      { key: 'job', label: 'Job', render: (j) => O.el('span', null, j.job, j.scan ? [' ', O.el('span', { class: 'tag', text: 'security scan' })] : null) },
      { key: 'event', label: 'Started by' },
      { key: 'minutes', label: 'Minutes', num: true, render: (j) => O.fmt.num(j.minutes) },
      { key: 'tests', label: 'Tests run', num: true, render: (j) => (j.tests ? O.fmt.int(j.tests) : '–') },
      { key: 'result', label: 'Result', render: (j) => (j.result === 'success' ? O.good('passed') : O.bad(j.result)) },
    ], a.jobs_detail, { sort: 'minutes' });
  };
})();
