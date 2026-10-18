/* CI Observatory: overview. Headline numbers, which repositories are fast and reliable, when runs start, and how
   many runs pass each day. */
(function () {
  'use strict';
  const O = window.O;
  const F = (O.fleet = {});
  const DECIDED = new Set(['success', 'failure']);

  // ---------------------------------------------------------------- data
  F.prep = function (data) {
    const runs = [];
    for (const r of data.runs || []) {
      const t0 = Date.parse(r.ts);
      if (isNaN(t0)) continue;
      // A re-run pushes a run's end days later; no hosted job runs past 6 hours, so cap there.
      const dur = Math.min(6 * 3600e3, Math.max(0, (r.wall_s || 0) * 1000));
      runs.push({ id: r.id, repo: r.repo, wf: r.workflow || '', ev: r.event || '', res: r.result || '', t0, dur, bot: !!r.bot });
    }
    runs.sort((a, b) => a.t0 - b.t0);
    F.runs = runs;
    F.start = runs.length ? runs[0].t0 : Date.now() - 864e5;
    F.end = runs.length ? runs[runs.length - 1].t0 : Date.now();
    const repoRows = new Map((data.repos || []).map((r) => [r.repo, r]));
    F.repos = [...O.groupBy(runs, (r) => r.repo).entries()].map(([repo, rs]) => {
      const decided = rs.filter((r) => DECIDED.has(r.res));
      const ok = decided.filter((r) => r.res === 'success').length;
      const walls = decided.map((r) => r.dur / 1000).filter((x) => x > 0);
      const row = repoRows.get(repo) || {};
      const days = row.covered_days || Math.max(1, (F.end - F.start) / 864e5);
      return {
        repo, short: O.fmt.short(repo), lang: row.language || 'Other', runs: rs.length, decided: decided.length, ok,
        fail: decided.length - ok, pass: decided.length ? ok / decided.length : null, med: O.median(walls),
        p90: O.quantile(walls, 0.9), perWeek: row.runs_per_week || (rs.length / days) * 7, truncated: !!row.truncated, days,
      };
    }).sort((a, b) => a.repo.localeCompare(b.repo));
    F.tzPrep();
  };
  /** Hour of day of every run in the chosen time zone. */
  F.tzPrep = function () { for (const r of F.runs) r.hod = O.hourOf(r.t0); };
  F.shown = () => (O.state.repo ? F.runs.filter((r) => r.repo === O.state.repo) : F.runs);

  F.init = function () {
    O.safe('kpis', F.kpis);
    O.safe('scatter', F.scatter);
    O.safe('hours', F.hours);
    O.safe('daily', F.daily);
    O.seg(O.$('#tz-seg'), O.TZ.map((z) => ({ id: z.id, label: z.label })), O.state.tz, (id) => {
      O.state.tz = id;
      F.tzPrep();
      O.safe('hours', F.hours);
      O.safe('daily', F.daily);
    }, 'Time zone');
    const btn = O.$('#scatter-table-btn');
    btn.addEventListener('click', () => {
      const on = btn.getAttribute('aria-pressed') !== 'true';
      btn.setAttribute('aria-pressed', String(on));
      btn.textContent = on ? 'Show as chart' : 'Show as table';
      O.$('#scatter').hidden = on;
      O.$('#scatter-table').hidden = !on;
      if (on) F.table();
    });
    O.on('repo', () => { O.safe('kpis', F.kpis); O.safe('scatter', F.scatter); O.safe('hours', F.hours); O.safe('daily', F.daily); if (btn.getAttribute('aria-pressed') === 'true') F.table(); });
    let width = 0;
    new ResizeObserver(() => { const w = O.$('#overview').clientWidth; if (Math.abs(w - width) > 4) { width = w; O.safe('scatter', F.scatter); O.safe('hours', F.hours); O.safe('daily', F.daily); } }).observe(O.$('#overview'));
  };

  // ---------------------------------------------------------------- headline numbers
  F.kpis = function () {
    const runs = F.shown();
    const decided = runs.filter((r) => DECIDED.has(r.res));
    const ok = decided.filter((r) => r.res === 'success').length;
    const walls = decided.map((r) => r.dur / 1000).filter((x) => x > 0);
    const days = Math.max(1, (F.end - F.start) / 864e5);
    const box = O.clear(O.$('#kpis'));
    box.appendChild(O.kpi('Runs', O.fmt.int(runs.length), `about ${O.fmt.num(runs.length / days)} a day`));
    box.appendChild(O.kpi('Passed', O.fmt.pct(ok / Math.max(1, decided.length)), `${O.fmt.int(decided.length - ok)} runs failed`));
    box.appendChild(O.kpi('A typical run takes', O.fmt.dur(O.median(walls)), 'half of all runs are faster'));
    box.appendChild(O.kpi('1 in 10 runs takes over', O.fmt.dur(O.quantile(walls, 0.9)), 'the long waits people notice'));
    if (!O.state.repo) box.appendChild(O.kpi('Repositories', O.fmt.int(F.repos.length), `${Math.round(days)} days of history`));
  };

  // ---------------------------------------------------------------- which repositories are fast and reliable
  F.scatter = function () {
    const box = O.$('#scatter');
    const rows = F.repos.filter((r) => r.med && r.pass != null && r.decided >= 10);
    if (!rows.length) { O.clear(box).appendChild(O.el('div', { class: 'empty', text: 'Not enough runs to compare repositories.' })); return; }
    const w = O.width(box), h = Math.round(Math.max(300, Math.min(460, w * 0.66)));
    const m = { l: 52, r: 18, t: 16, b: 50 };
    const lo = Math.max(4, Math.min(...rows.map((r) => r.med)) * 0.7);
    const hi = Math.max(lo * 5, Math.max(...rows.map((r) => r.med)) * 1.4);
    const x = O.log(lo, hi, m.l, w - m.r);
    const pmin = Math.max(0, Math.floor(Math.min(...rows.map((r) => r.pass)) * 10) / 10 - 0.05);
    const y = O.linear(pmin, 1, h - m.b, m.t);
    const xs = O.median(rows.map((r) => r.med)), ys = 0.9;
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Repositories by typical run time and pass rate' });
    // the good and the worrying corners
    const qx = x(xs), qy = y(ys);
    svg.appendChild(O.svg('rect', { x: m.l, y: m.t, width: Math.max(0, qx - m.l), height: Math.max(0, qy - m.t), fill: O.alpha(O.color.good, 0.07) }));
    svg.appendChild(O.svg('rect', { x: qx, y: qy, width: Math.max(0, w - m.r - qx), height: Math.max(0, h - m.b - qy), fill: O.alpha(O.color.crit, 0.07) }));
    svg.appendChild(O.svg('line', { x1: qx, x2: qx, y1: m.t, y2: h - m.b, stroke: O.alpha(O.color['ink-3'], 0.35), 'stroke-dasharray': '4 4' }));
    svg.appendChild(O.svg('line', { x1: m.l, x2: w - m.r, y1: qy, y2: qy, stroke: O.alpha(O.color['ink-3'], 0.35), 'stroke-dasharray': '4 4' }));
    // axes
    for (let v = Math.ceil(pmin * 20) / 20; v <= 1.0001; v += pmin < 0.6 ? 0.1 : 0.05) {
      svg.appendChild(O.svg('line', { x1: m.l, x2: w - m.r, y1: y(v), y2: y(v), class: 'gridline' }));
      svg.appendChild(O.svg('text', { x: m.l - 8, y: y(v) + 4, 'text-anchor': 'end', text: O.fmt.pct(v) }));
    }
    const ticks = [];
    for (const t of [[10, '10 s'], [30, '30 s'], [60, '1 min'], [120, '2 min'], [300, '5 min'], [600, '10 min'], [1200, '20 min'], [1800, '30 min'], [3600, '1 h']]) {
      if (t[0] >= lo && t[0] <= hi && (!ticks.length || x(t[0]) - x(ticks[ticks.length - 1][0]) >= 52)) ticks.push(t);
    }
    for (const [s, label] of ticks) {
      svg.appendChild(O.svg('line', { x1: x(s), x2: x(s), y1: m.t, y2: h - m.b, class: 'gridline' }));
      svg.appendChild(O.svg('text', { x: x(s), y: h - m.b + 18, 'text-anchor': 'middle', text: label }));
    }
    svg.appendChild(O.svg('text', { x: (m.l + w - m.r) / 2, y: h - 8, 'text-anchor': 'middle', class: 'axis-title', text: '← faster          typical run time          slower →' }));
    svg.appendChild(O.svg('text', { x: 14, y: (m.t + h - m.b) / 2, 'text-anchor': 'middle', class: 'axis-title', transform: `rotate(-90 14 ${(m.t + h - m.b) / 2})`, text: 'runs that pass' }));
    // bubbles, biggest first so small ones stay on top
    const maxWeek = Math.max(1, ...rows.map((r) => r.perWeek));
    const sel = O.state.repo;
    const dots = rows.map((r) => ({ r, cx: x(r.med), cy: y(r.pass), rad: 6 + 15 * Math.sqrt(r.perWeek / maxWeek) })).sort((a, b) => b.rad - a.rad);
    for (const d of dots) {
      const chosen = d.r.repo === sel;
      const g = O.svg('circle', { cx: d.cx, cy: d.cy, r: d.rad, fill: O.alpha(O.color.hud, chosen ? 0.75 : sel ? 0.12 : 0.32), stroke: chosen ? O.color.ink : O.alpha(O.color.hud, sel ? 0.35 : 0.85), 'stroke-width': chosen ? 2 : 1.2, style: 'cursor:pointer' });
      g.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(d.r.short, [
        ['Runs that pass', `${O.fmt.pct(d.r.pass, 1)} of ${O.fmt.int(d.r.decided)}`], ['Typical run', O.fmt.dur(d.r.med)], ['1 in 10 runs takes over', O.fmt.dur(d.r.p90)], ['Runs per week', O.fmt.num(d.r.perWeek)],
      ], 'Click to focus the page on this repository.')));
      g.addEventListener('pointerleave', () => O.tip.hide());
      g.addEventListener('click', () => O.selectRepo(d.r.repo));
      svg.appendChild(g);
    }
    // labels where they fit, never over another label
    const placed = [];
    const hitsBubble = (bx, by, bw, self) => dots.some((o) => o !== self && o.cx + o.rad > bx && o.cx - o.rad < bx + bw && o.cy + o.rad > by - 12 && o.cy - o.rad < by + 4);
    const free = (bx, by, bw) => bx >= 2 && bx + bw <= w - 2 && by - 12 >= 2 && by + 4 <= h - m.b && !placed.some((p) => bx < p.x + p.w && bx + bw > p.x && by - 12 < p.y + 4 && by + 4 > p.y - 12);
    for (const d of dots.slice().sort((a, b) => (b.r.repo === sel) - (a.r.repo === sel) || b.r.perWeek - a.r.perWeek)) {
      const text = d.r.short.length > 22 ? d.r.short.slice(0, 21) + '…' : d.r.short, tw = text.length * 7 + 4;
      const options = [[d.cx + d.rad + 5, d.cy + 4], [d.cx - d.rad - 5 - tw, d.cy + 4], [d.cx - tw / 2, d.cy - d.rad - 5], [d.cx - tw / 2, d.cy + d.rad + 15]];
      const spot = options.find(([bx, by]) => free(bx, by, tw) && !hitsBubble(bx, by, tw, d)) || options.find(([bx, by]) => free(bx, by, tw));
      if (!spot) continue;
      placed.push({ x: spot[0], y: spot[1], w: tw });
      svg.appendChild(O.svg('text', { x: spot[0], y: spot[1], class: (d.r.repo === sel ? 'lbl-strong' : 'lbl') + ' halo', style: (sel && d.r.repo !== sel ? 'opacity:.45;' : '') + 'pointer-events:none', text }));
    }
    O.clear(box).appendChild(svg);
    box.appendChild(O.el('div', { class: 'legend' },
      O.el('span', null, O.el('i', { style: { background: O.alpha(O.color.good, 0.45) } }), 'Fast and reliable (faster than typical, 90%+ pass)'),
      O.el('span', null, O.el('i', { style: { background: O.alpha(O.color.crit, 0.45) } }), 'Slow and often failing')));
    const good = rows.filter((r) => r.med <= xs && r.pass >= ys);
    const slow = rows.slice().sort((a, b) => b.med - a.med)[0];
    const weak = rows.slice().sort((a, b) => a.pass - b.pass)[0];
    O.put(O.clear(O.$('#scatter-answer')), [
      O.el('b', { text: `${good.length} of ${rows.length}` }), ' repositories are fast and reliable. Slowest: ',
      O.el('b', { text: slow.short }), ` (typical run ${O.fmt.dur(slow.med)}). Least reliable: `,
      O.el('b', { text: weak.short }), ` (${O.fmt.pct(weak.pass)} of runs pass).`,
    ]);
  };
  F.table = function () {
    O.table(O.$('#scatter-table'), [
      { key: 'short', label: 'Repository' },
      { key: 'pass', label: 'Runs that pass', num: true, render: (r) => O.fmt.pct(r.pass, 1) },
      { key: 'med', label: 'Typical run', num: true, render: (r) => O.fmt.dur(r.med) },
      { key: 'p90', label: '1 in 10 takes over', num: true, render: (r) => O.fmt.dur(r.p90) },
      { key: 'perWeek', label: 'Runs per week', num: true, render: (r) => O.fmt.num(r.perWeek) },
    ], F.repos, { sort: 'perWeek', onRow: (r) => O.selectRepo(r.repo), isSelected: (r) => r.repo === O.state.repo });
  };

  // ---------------------------------------------------------------- when do runs start
  F.hours = function () {
    const box = O.$('#hours');
    const runs = F.shown();
    const counts = new Array(24).fill(0), passed = new Array(24).fill(0), decided = new Array(24).fill(0);
    for (const r of runs) {
      const hr = Math.floor(r.hod) % 24;
      counts[hr]++;
      if (DECIDED.has(r.res)) { decided[hr]++; if (r.res === 'success') passed[hr]++; }
    }
    const w = O.width(box), h = 250, m = { l: 40, r: 8, t: 14, b: 34 };
    const max = Math.max(1, ...counts);
    const peak = counts.indexOf(max);
    const bw = (w - m.l - m.r) / 24;
    const y = O.linear(0, max, h - m.b, m.t);
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Runs started per hour of the day' });
    svg.appendChild(O.svg('rect', { x: m.l + 9 * bw, y: m.t - 6, width: 8 * bw, height: h - m.b - m.t + 6, fill: O.alpha(O.color.hud, 0.06), rx: 6 }));
    svg.appendChild(O.svg('text', { x: m.l + 13 * bw, y: m.t + 6, 'text-anchor': 'middle', class: 'axis-title', text: 'working hours' }));
    for (const v of O.ticks(0, max, 3)) {
      svg.appendChild(O.svg('line', { x1: m.l, x2: w - m.r, y1: y(v), y2: y(v), class: 'gridline' }));
      svg.appendChild(O.svg('text', { x: m.l - 8, y: y(v) + 4, 'text-anchor': 'end', text: O.fmt.int(v) }));
    }
    counts.forEach((n, hr) => {
      const bar = O.svg('rect', { x: m.l + hr * bw + 2, y: y(n), width: Math.max(2, bw - 4), height: h - m.b - y(n), rx: 3, fill: hr === peak ? O.color.hud : O.alpha(O.color.hud, 0.42) });
      bar.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(`${String(hr).padStart(2, '0')}:00–${String((hr + 1) % 24).padStart(2, '0')}:00`, [
        ['Runs started', O.fmt.int(n)], ['Share of the day', O.fmt.pct(n / Math.max(1, runs.length))], ['Runs that pass', O.fmt.pct(decided[hr] ? passed[hr] / decided[hr] : null)],
      ])));
      bar.addEventListener('pointerleave', () => O.tip.hide());
      svg.appendChild(bar);
      if (hr % (w < 520 ? 6 : 3) === 0) svg.appendChild(O.svg('text', { x: m.l + hr * bw + bw / 2, y: h - m.b + 18, 'text-anchor': 'middle', text: String(hr).padStart(2, '0') + ':00' }));
    });
    O.clear(box).appendChild(svg);
    const work = O.sum(counts.slice(9, 17));
    const zone = (O.TZ.find((z) => z.id === O.state.tz) || {}).label;
    O.put(O.clear(O.$('#hours-answer')), [
      'Busiest at ', O.el('b', { text: `${String(peak).padStart(2, '0')}:00–${String((peak + 1) % 24).padStart(2, '0')}:00` }), ` ${zone === 'Your time' ? 'your time' : zone}. `,
      O.el('b', { text: O.fmt.pct(1 - work / Math.max(1, runs.length)) }), ' of runs start outside working hours (bots and schedules).',
    ]);
  };

  // ---------------------------------------------------------------- runs per day
  F.daily = function () {
    const box = O.$('#daily');
    const runs = F.shown();
    const dayOf = (t) => Math.floor((t + O.offset(t, O.zone()) * 60000) / 864e5);
    const first = dayOf(F.start), last = dayOf(F.end);
    const days = Array.from({ length: last - first + 1 }, (_, i) => ({ day: first + i, ok: 0, bad: 0, other: 0 }));
    for (const r of runs) {
      const d = days[dayOf(r.t0) - first];
      if (!d) continue;
      if (r.res === 'success') d.ok++; else if (r.res === 'failure') d.bad++; else d.other++;
    }
    const w = O.width(box), h = 210, m = { l: 40, r: 8, t: 10, b: 30 };
    const max = Math.max(1, ...days.map((d) => d.ok + d.bad + d.other));
    const bw = (w - m.l - m.r) / days.length;
    const y = O.linear(0, max, h - m.b, m.t);
    const svg = O.svg('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Runs per day, passed and failed' });
    let lastLabel = -Infinity;
    for (const v of O.ticks(0, max, 3)) {
      svg.appendChild(O.svg('line', { x1: m.l, x2: w - m.r, y1: y(v), y2: y(v), class: 'gridline' }));
      svg.appendChild(O.svg('text', { x: m.l - 8, y: y(v) + 4, 'text-anchor': 'end', text: O.fmt.int(v) }));
    }
    days.forEach((d, i) => {
      const x0 = m.l + i * bw;
      const date = new Date(d.day * 864e5);
      const weekday = date.getUTCDay();
      if (weekday === 0 || weekday === 6) svg.appendChild(O.svg('rect', { x: x0, y: m.t, width: bw, height: h - m.b - m.t, fill: O.alpha(O.color.ink, 0.03) }));
      let base = h - m.b;
      for (const [n, color] of [[d.ok, O.color.good], [d.other, O.color['c-other']], [d.bad, O.color.crit]]) {
        if (!n) continue;
        const top = y(n) - (h - m.b);
        svg.appendChild(O.svg('rect', { x: x0 + 1.5, y: base + top, width: Math.max(1.5, bw - 3), height: -top, fill: color, rx: Math.min(3, bw / 4) }));
        base += top;
      }
      const hit = O.svg('rect', { x: x0, y: m.t, width: bw, height: h - m.b - m.t, fill: 'transparent' });
      const total = d.ok + d.bad + d.other;
      hit.addEventListener('pointermove', (e) => O.tip.show(e, O.tipBody(date.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' }), [
        ['Runs', O.fmt.int(total)], ['Passed', O.fmt.int(d.ok)], ['Failed', O.fmt.int(d.bad)], ['Cancelled or skipped', O.fmt.int(d.other)],
      ])));
      hit.addEventListener('pointerleave', () => O.tip.hide());
      svg.appendChild(hit);
      if (weekday === 1 && x0 - lastLabel >= 64) lastLabel = x0, svg.appendChild(O.svg('text', { x: x0 + bw / 2, y: h - 10, 'text-anchor': 'middle', text: date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' }) }));
    });
    O.clear(box).appendChild(svg);
    O.legend(O.$('#daily-legend'), [{ label: 'Passed', color: O.color.good }, { label: 'Failed', color: O.color.crit }, { label: 'Cancelled or skipped', color: O.color['c-other'] }]);
    const worst = days.filter((d) => d.ok + d.bad >= 10).map((d) => ({ d, rate: d.bad / (d.ok + d.bad) })).sort((a, b) => b.rate - a.rate)[0];
    const busiest = days.slice().sort((a, b) => b.ok + b.bad + b.other - (a.ok + a.bad + a.other))[0];
    const label = (d) => new Date(d.day * 864e5).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
    O.put(O.clear(O.$('#daily-answer')), [
      'Busiest day: ', O.el('b', { text: label(busiest) }), ` (${O.fmt.int(busiest.ok + busiest.bad + busiest.other)} runs). `,
      worst ? ['Most failures: ', O.el('b', { text: label(worst.d) }), ` (${O.fmt.pct(worst.rate)} of runs failed). `] : '',
      'Weekends are shaded.',
    ]);
  };
})();
