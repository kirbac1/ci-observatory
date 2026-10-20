/* CI Observatory: AI impact, the solution box and how it's measured. */
(function () {
  'use strict';
  const O = window.O;

  // ================================================================ AI impact
  const A = (O.ai = {});
  A.INSTRUCTIONS = ['AGENTS.md', 'CLAUDE.md', 'GEMINI.md', 'Copilot instructions', 'Copilot path instructions', 'Cursor rules', 'Windsurf rules', 'Cline rules'];
  A.WHO = [
    { key: 'human', label: 'People', color: () => O.color.c1 },
    { key: 'bot', label: 'Bots', color: () => O.color.c2 },
    { key: 'ai', label: 'AI agents', color: () => O.color.c3 },
    { key: 'schedule', label: 'Schedules', color: () => O.color.c4 },
    { key: 'unknown', label: 'Unknown', color: () => O.color['c-other'] },
  ];
  A.init = function (data) {
    A.sig = data.signals;
    if (!A.sig || !A.sig.repos) { O.$('#ai-kpis').appendChild(O.el('div', { class: 'empty', text: 'No AI data in this report.' })); return; }
    A.repos = A.sig.repos;
    A.prs = [];
    for (const r of A.repos) for (const p of r.prs) A.prs.push(Object.assign({ repo: r.repo }, p));
    const draw = () => {
      O.safe('ai-kpis', A.kpis);
      O.safe('ai-tools', A.tools);
      O.safe('ai-models', A.models);
      O.safe('ai-outcomes', A.outcomes);
      O.safe('ai-who', A.who);
      O.safe('ai-repos', A.table);
    };
    draw();
    let width = 0;
    new ResizeObserver(() => requestAnimationFrame(() => { const w = O.$('#ai').clientWidth; if (Math.abs(w - width) > 4) { width = w; draw(); } })).observe(O.$('#ai'));
  };
  A.hasInstructions = (r) => A.INSTRUCTIONS.some((k) => r.files && r.files[k]);
  A.kpis = function () {
    const t = A.sig.totals;
    const people = A.prs.filter((p) => p.author !== 'bot').length;
    const box = O.clear(O.$('#ai-kpis'));
    box.appendChild(O.kpi('Pull requests that name AI', O.fmt.pct(t.ai_prs / Math.max(1, t.prs)), `${t.ai_prs} of ${t.prs}`));
    box.appendChild(O.kpi('Commits that name AI', O.fmt.pct(t.ai_commits / Math.max(1, t.commits)), `${t.ai_commits} of ${t.commits} on main branches`));
    box.appendChild(O.kpi('Pull requests reviewed by AI', O.fmt.pct(t.ai_reviewed_prs / Math.max(1, people)), `${t.ai_reviewed_prs} pull requests`));
    box.appendChild(O.kpi('CI time involving AI', O.fmt.pct(t.ai_minutes / Math.max(1, t.minutes)), 'AI review, AI agents, AI-written changes'));
  };
  A.spread = (repos) => Object.entries(repos || {}).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, v]) => [O.fmt.short(k), `${v} PRs`]);
  A.tools = function () {
    const rows = A.sig.agents.filter((g) => g.kind === 'agent').map((g) => ({
      label: g.name === 'Agent not named' ? 'Tool not named' : g.name, value: g.prs, text: `${g.prs} PRs`,
      color: g.name === 'Agent not named' ? O.color['c-other'] : O.color.c3, tip: A.spread(g.repos),
      note: g.name === 'Agent not named' ? 'The commit names a model, such as "Assisted-by: Claude Opus 5.5", but not the tool that ran it.' : null,
    })).sort((a, b) => b.value - a.value);
    if (!rows.length) { O.clear(O.$('#ai-tools')).appendChild(O.el('div', { class: 'empty', text: 'No pull request names an AI tool.' })); return; }
    O.hbars(O.$('#ai-tools'), rows, { left: 170, right: 80, label: 'Pull requests per AI tool' });
  };
  A.models = function () {
    const rows = A.sig.agents.filter((g) => g.kind === 'model').map((g) => ({ label: g.name, value: g.prs, text: `${g.prs} PRs`, color: O.color.c7, tip: A.spread(g.repos) })).sort((a, b) => b.value - a.value);
    if (!rows.length) { O.clear(O.$('#ai-models')).appendChild(O.el('div', { class: 'empty', text: 'No model is named.' })); return; }
    O.hbars(O.$('#ai-models'), rows, { left: 170, right: 80, label: 'Pull requests per AI model' });
  };
  /** Counts per group of pull requests: changes that hit a problem, from CI, review, reverts and later fixes. */
  A.groups = function (prs) {
    const groups = new Map();
    const add = (kind, name, p) => {
      const key = kind + ':' + name;
      if (!groups.has(key)) groups.set(key, { kind, name, prs: 0, problem: 0, first: 0, withCi: 0, requested: 0, reverted: 0, repos: {} });
      const g = groups.get(key);
      g.prs++;
      g.repos[p.repo] = (g.repos[p.repo] || 0) + 1;
      if (p.ci && p.ci.rounds) { g.withCi++; if (p.ci.first_failed) g.first++; }
      if (p.reviews.changes_requested) g.requested++;
      if (p.reverted || p.fixups) g.reverted++;
      if ((p.ci && p.ci.first_failed) || p.reviews.changes_requested || p.reverted || p.fixups) g.problem++;
    };
    for (const p of prs) {
      if (p.author === 'bot') continue;
      if (!p.agents.length) add('baseline', 'No AI named', p);
      for (const a of p.agents) add('agent', a === 'Agent not named' ? 'Tool not named' : a, p);
      for (const m of p.models) add('model', m, p);
      for (const r of Object.keys(p.reviews.ai || {})) add('reviewer', 'Reviewed by ' + r, p);
    }
    return [...groups.values()];
  };
  A.outcomes = function () {
    const groups = A.groups(A.prs);
    const base = groups.find((g) => g.kind === 'baseline');
    const order = { baseline: 0, agent: 1, model: 2, reviewer: 3 };
    const rows = groups.filter((g) => g.kind === 'baseline' || g.prs >= 5).sort((a, b) => order[a.kind] - order[b.kind] || b.prs - a.prs);
    const small = groups.filter((g) => g.kind !== 'baseline' && g.prs < 5);
    const color = { baseline: O.color['ink-2'], agent: O.color.c3, model: O.color.c7, reviewer: O.color.c1 };
    O.hbars(O.$('#ai-outcomes'), rows.map((g) => ({
      label: g.name, value: g.problem / g.prs, strong: g.kind === 'baseline',
      text: `${O.fmt.pct(g.problem / g.prs)} of ${g.prs}${g.prs < 10 ? ' · too few' : ''}`, color: color[g.kind], faded: g.prs < 10,
      tip: [['Pull requests', String(g.prs)], ['First CI run failed', `${g.first} of ${g.withCi}`], ['Changes requested', String(g.requested)], ['Reverted or fixed later', String(g.reverted)]],
      note: g.prs < 10 ? 'Fewer than 10 pull requests: too few to compare.' : null,
    })), { left: 220, right: 140, max: Math.max(0.35, ...rows.map((g) => g.problem / g.prs)), reference: base ? { value: base.problem / base.prs, label: 'no AI' } : null, label: 'Share of pull requests with a problem' });
    if (small.length) O.$('#ai-outcomes').appendChild(O.el('p', { class: 'panel-foot', text: `Not shown, fewer than 5 pull requests: ${small.map((g) => `${g.name} (${g.prs})`).join(', ')}.` }));
    // A difference only counts when the 95% ranges do not overlap.
    const [blo, bhi] = base ? O.wilson(base.problem, base.prs) : [0, 1];
    const verdicts = groups.filter((g) => (g.kind === 'agent' || g.kind === 'model') && g.prs >= 10).map((g) => {
      const [lo, hi] = O.wilson(g.problem, g.prs);
      return { g, better: hi < blo, worse: lo > bhi };
    });
    const better = verdicts.filter((v) => v.better).map((v) => v.g.name), worse = verdicts.filter((v) => v.worse).map((v) => v.g.name);
    const ranked = groups.filter((g) => (g.kind === 'agent' || g.kind === 'model') && g.prs >= 5).sort((a, b) => a.problem / a.prs - b.problem / b.prs);
    O.put(O.clear(O.$('#ai-outcomes-answer')), [
      base ? ['Without AI, ', O.el('b', { text: O.fmt.pct(base.problem / base.prs) }), ' of pull requests hit a problem. '] : '',
      better.length || worse.length
        ? [better.length ? ['Clearly fewer problems: ', O.el('b', { text: better.join(', ') }), '. '] : '', worse.length ? ['Clearly more: ', O.el('b', { text: worse.join(', ') }), '. '] : '']
        : ['No AI tool or model is clearly better or worse yet: the samples are too small. ', ranked.length ? ['Lowest so far: ', O.el('b', { text: `${ranked[0].name} (${O.fmt.pct(ranked[0].problem / ranked[0].prs)} of ${ranked[0].prs})` }), '.'] : ''],
    ]);
  };
  A.who = function () {
    const sum = {};
    let total = 0, ai = 0;
    for (const r of A.repos) {
      for (const [k, v] of Object.entries(r.ci.who_minutes || {})) { sum[k] = (sum[k] || 0) + v; total += v; }
      ai += (r.ci.ai_minutes && r.ci.ai_minutes.any) || 0;
    }
    const parts = A.WHO.map((k) => ({ label: k.label, value: sum[k.key] || 0, color: k.color(), tip: [['Run minutes', O.fmt.int(sum[k.key] || 0)]] }));
    O.split(O.$('#ai-who'), parts);
    O.legend(O.$('#ai-who-legend'), parts.filter((p) => p.value).map((p) => ({ label: p.label, color: p.color })));
    const share = (k) => (sum[k] || 0) / Math.max(1, total);
    O.put(O.clear(O.$('#ai-who-answer')), [
      'People start ', O.el('b', { text: O.fmt.pct(share('human')) }), ' of CI time and AI agents ', O.el('b', { text: O.fmt.pct(share('ai')) }), '. ',
      'Counting CI that checks AI-written changes, AI is involved in ', O.el('b', { text: O.fmt.pct(ai / Math.max(1, total)) }), '.',
    ]);
  };
  A.table = function () {
    const rows = A.repos.map((r) => {
      const prs = r.prs.filter((p) => p.author !== 'bot');
      const review = r.ai_workflows.filter((w) => w.kind === 'copilot-review');
      return {
        repo: r.repo, short: O.fmt.short(r.repo), docs: A.INSTRUCTIONS.filter((k) => r.files[k]),
        aiPrs: prs.filter((p) => p.agents.length).length, prs: prs.length, aiCommits: r.commits.ai, commits: r.commits.total,
        reviewRuns: O.sum(review, (w) => w.runs || 0), reviewOn: review.length > 0,
      };
    });
    O.table(O.$('#ai-repos'), [
      { key: 'short', label: 'Repository' },
      { key: 'aiPrs', label: 'AI pull requests', num: true, value: (r) => r.aiPrs / Math.max(1, r.prs), render: (r) => `${r.aiPrs} of ${r.prs}` },
      { key: 'aiCommits', label: 'AI commits', num: true, value: (r) => r.aiCommits / Math.max(1, r.commits), render: (r) => `${r.aiCommits} of ${r.commits}` },
      { key: 'docs', label: 'Instructions for AI', value: (r) => r.docs.length, render: (r) => (r.docs.length ? O.good(r.docs.length > 1 ? `${r.docs.length} files` : r.docs[0], r.docs.join(', ')) : (r.aiCommits || r.aiPrs) ? O.bad('missing', 'AI is used here, but there is no AGENTS.md or similar telling it how to build and test') : O.na('none')) },
      { key: 'reviewRuns', label: 'Copilot review', num: true, render: (r) => (r.reviewRuns ? `${r.reviewRuns} runs` : r.reviewOn ? O.el('span', { class: 'muted', text: 'on, unused' }) : '–') },
    ], rows, { sort: 'aiCommits', onRow: (r) => O.selectRepo(r.repo), isSelected: (r) => r.repo === O.state.repo });
  };

  // ================================================================ solution box
  const S = (O.solutions = { area: 'All' });
  S.SEV = { crit: ['Critical', 9], high: ['High', 6], med: ['Medium', 3], low: ['Low', 1] };
  S.EFFORT = { S: ['quick fix', 1], M: ['some work', 2], L: ['project', 3] };
  S.PHASE = { 1: ['Remove waste', 'stop work that adds nothing'], 2: ['Fix what breaks', 'make red mean something again'], 3: ['Speed up', 'make the remaining work faster'], 4: ['Prevent', 'set defaults so it stays fixed'] };
  S.init = function (data) {
    S.cards = [];
    for (const rule of S.RULES) { try { const card = rule(data); if (card) S.cards.push(card); } catch (e) { console.error('solution rule failed', e); } }
    S.cards.forEach((c) => { c.score = (S.SEV[c.sev][1] * (c.confidence || 1)) / S.EFFORT[c.effort][1]; });
    S.cards.sort((a, b) => a.phase - b.phase || b.score - a.score);
    const principle = O.clear(O.$('#sol-principle'));
    Object.entries(S.PHASE).forEach(([k, [name]], i) => {
      if (i) principle.appendChild(O.el('span', { class: 'arrow', text: '→' }));
      principle.appendChild(O.el('span', { class: 'stepbox' }, O.el('b', { text: k }), name));
    });
    const areas = ['All', ...new Set(S.cards.map((c) => c.area))];
    const tools = O.clear(O.$('#sol-tools'));
    const seg = O.el('div', { class: 'seg', style: { flexWrap: 'wrap' } });
    O.seg(seg, areas.map((a) => ({ id: a, label: a === 'All' ? `All (${S.cards.length})` : a })), S.area, (id) => { S.area = id; S.render(); }, 'Area');
    tools.appendChild(seg);
    tools.appendChild(O.el('button', { class: 'btn', type: 'button', text: 'Copy as Markdown', on: { click: (e) => O.copy(S.markdown(), e.currentTarget) } }));
    S.render();
  };
  S.render = function () {
    const box = O.clear(O.$('#solution-cards'));
    const cards = S.cards.filter((c) => S.area === 'All' || c.area === S.area);
    if (!cards.length) { box.appendChild(O.el('div', { class: 'empty', text: 'Nothing to fix in this area.' })); return; }
    let phase = null;
    for (const c of cards) {
      if (c.phase !== phase) {
        phase = c.phase;
        box.appendChild(O.el('div', { class: 'phase' }, O.el('b', { text: `${phase}. ${S.PHASE[phase][0]}` }), O.el('span', { text: S.PHASE[phase][1] })));
      }
      const how = O.el('details', null, O.el('summary', { text: 'How to fix it' }),
        O.el('ol', null, c.fix.map((s) => O.el('li', { text: s }))),
        c.snippet ? O.el('pre', { text: c.snippet }) : null,
        c.repos && c.repos.length ? O.el('div', { class: 'repos' }, c.repos.slice(0, 10).map((r) => O.el('button', { type: 'button', text: O.fmt.short(r), title: 'Focus the page on ' + r, on: { click: () => O.selectRepo(r) } }))) : null);
      box.appendChild(O.el('article', { class: 'card sev-' + c.sev },
        O.el('div', { class: 'top' },
          O.status(c.sev === 'crit' ? 'crit' : c.sev === 'high' ? 'serious' : c.sev === 'med' ? 'warn' : 'na', c.sev === 'low' ? 'i' : '!', S.SEV[c.sev][0]),
          O.el('span', { class: 'tag', text: c.area }), O.el('span', { class: 'tag', text: S.EFFORT[c.effort][0] })),
        O.el('h4', { text: c.title }),
        O.el('div', { class: 'line' }, O.el('b', { text: 'Problem: ' }), c.problem),
        O.el('div', { class: 'line' }, O.el('b', { text: 'Gain: ' }), c.gain),
        how));
    }
  };
  S.markdown = function () {
    const lines = ['# Solution box', ''];
    for (const c of S.cards.filter((x) => S.area === 'All' || x.area === S.area)) {
      lines.push(`## ${c.title}`, '', `*${S.SEV[c.sev][0]} · ${c.area} · ${S.EFFORT[c.effort][0]} · step ${c.phase}: ${S.PHASE[c.phase][0]}*`, '', `**Problem:** ${c.problem}`, '');
      c.fix.forEach((s, i) => lines.push(`${i + 1}. ${s}`));
      if (c.snippet) lines.push('', '```yaml', c.snippet, '```');
      lines.push('', `**Gain:** ${c.gain}`, '');
      if (c.repos && c.repos.length) lines.push(`Repositories: ${c.repos.join(', ')}`, '');
    }
    return lines.join('\n');
  };
  const short = (names, n) => names.slice(0, n || 3).map(O.fmt.short).join(', ') + (names.length > (n || 3) ? ` and ${names.length - (n || 3)} more` : '');
  const distinct = (jobs, n) => {
    const out = [], repos = new Set();
    for (const j of jobs) { if (!repos.has(j.repo)) { repos.add(j.repo); out.push(j); } if (out.length >= n) break; }
    return out;
  };
  S.RULES = [
    // ---- 1. remove waste
    function securityScanTests(data) {
      const hits = ((data.tests && data.tests.repos) || []).filter((a) => (a.extra.in_security_scan || 0) > 0);
      if (!hits.length) return null;
      const runs = O.sum(hits, (a) => a.extra.in_security_scan);
      return { phase: 1, area: 'Tests', sev: 'high', effort: 'S', repos: hits.map((a) => a.repo), title: 'Stop running tests inside the security scan',
        problem: `In ${short(hits.map((a) => a.repo))}, CodeQL runs the whole test suite: ${O.fmt.int(runs)} test runs per pull request it doesn't need. A failing test can even fail the scan.`,
        fix: ['Let CodeQL analyse Java without building (build-mode: none), or build with tests skipped.', 'Only build Java in the job that analyses Java.', 'Keep the real test run in the normal pipeline, once.'],
        snippet: "- uses: github/codeql-action/init@v4\n  with:\n    languages: ${{ matrix.language }}\n    build-mode: none   # or: ./mvnw -B -DskipTests package",
        gain: `${O.fmt.int(runs)} fewer test runs per pull request and faster security results.` };
    },
    function doubleTriggers(data) {
      const hits = ((data.tests && data.tests.repos) || []).filter((a) => (a.extra.double_trigger || 0) > 0);
      if (!hits.length) return null;
      return { phase: 1, area: 'Tests', sev: 'high', effort: 'S', repos: hits.map((a) => a.repo), title: 'Run CI once per push, not twice',
        problem: `In ${short(hits.map((a) => a.repo))}, the same workflow starts for both "push" and "pull_request", so every test runs twice.`,
        fix: ['Run on push only for the main branch and tags.', 'Keep pull_request for branches.', 'Cancel older runs when a new commit arrives.'],
        snippet: "on:\n  push:\n    branches: [main]\n  pull_request:\nconcurrency:\n  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n  cancel-in-progress: true",
        gain: 'Half the CI load of those workflows on pull requests.' };
    },
    function matrixCost(data) {
      const heavy = [];
      for (const a of (data.tests && data.tests.repos) || []) for (const m of a.matrix || []) if (m.extra_minutes >= 15) heavy.push(Object.assign({ repo: a.repo }, m));
      if (!heavy.length) return null;
      heavy.sort((x, y) => y.extra_minutes - x.extra_minutes);
      const total = O.sum(heavy, (m) => m.extra_minutes);
      return { phase: 1, area: 'Tests', sev: 'med', effort: 'S', confidence: 0.8, repos: [...new Set(heavy.map((m) => m.repo))], title: 'Test every version after merge, not on every pull request',
        problem: `Running the same tests on every Python or Java version adds ${O.fmt.mins(total)} to each pull request (most in ${short(heavy.map((m) => m.repo))}).`,
        fix: ['On pull requests, test the oldest and newest supported versions.', 'Test all versions after merge, nightly or in the merge queue.', 'Turn on fail-fast so one failure stops the rest.'],
        snippet: "strategy:\n  fail-fast: true\n  matrix:\n    python: ${{ github.event_name == 'pull_request'\n      && fromJSON('[\"3.10\", \"3.14\"]')\n      || fromJSON('[\"3.10\", \"3.11\", \"3.12\", \"3.13\", \"3.14\"]') }}",
        gain: `Up to ${O.fmt.mins(total)} less waiting per pull request, same coverage before release.` };
    },
    // ---- 2. fix what breaks
    function brokenDeploys(data) {
      const repos = ((data.signals && data.signals.repos) || []).filter((r) => r.dora && r.dora.source === 'deploy workflow runs' && r.dora.cfr_attempts >= 3 && r.dora.cfr >= 0.5);
      if (!repos.length) return null;
      return { phase: 2, area: 'Delivery', sev: 'crit', effort: 'S', repos: repos.map((r) => r.repo), title: 'Repair the deploys that always fail',
        problem: `${repos.map((r) => `${O.fmt.short(r.repo)}: ${r.dora.cfr_failed} of ${r.dora.cfr_attempts} deploys failed`).join('; ')}.`,
        fix: ['Open the failing deploy in "Inside a job" and fix the error it shows.', 'Run the same build on pull requests so it cannot break unnoticed.', 'Alert the owners when a deploy fails twice in a row.'],
        gain: 'Deploys work again; fixes reach users.' };
    },
    function redMain(data) {
      const repos = ((data.signals && data.signals.repos) || []).filter((r) => r.dora && r.dora.main_red_share >= 0.1).sort((a, b) => b.dora.main_red_share - a.dora.main_red_share);
      if (!repos.length) return null;
      return { phase: 2, area: 'Reliability', sev: repos[0].dora.main_red_share >= 0.3 ? 'crit' : 'high', effort: 'M', repos: repos.map((r) => r.repo), title: 'Keep the main branch green',
        problem: `${repos.slice(0, 3).map((r) => `${O.fmt.short(r.repo)} was red ${O.fmt.pct(r.dora.main_red_share)} of the time`).join(', ')}. A red main hides new breakage.`,
        fix: ['Require the main-branch checks on pull requests too.', 'Post to the team channel when main turns red; revert first, then fix.', 'Use a merge queue so main only gets tested merges.'],
        gain: 'Faster recovery; releases are never blocked by old failures.' };
    },
    function writeAllToken(data) {
      const byRepo = O.groupBy((data.logs && data.logs.jobs) || [], (j) => j.repo);
      const hits = [...byRepo.entries()].filter(([, jobs]) => jobs.some((j) => ((j.token && j.token.write) || []).length >= 10)).map(([repo]) => repo);
      if (!hits.length) return null;
      return { phase: 2, area: 'Security', sev: 'high', effort: 'S', repos: hits, title: 'Make the CI token read-only by default',
        problem: `In ${hits.length} of ${byRepo.size} repositories, CI jobs could write to everything: code, packages and releases.`,
        fix: ['Set the organisation default to read-only (Settings → Actions → Workflow permissions).', 'Give write access per job, only where it is needed.'],
        snippet: 'permissions:\n  contents: read\njobs:\n  release:\n    permissions:\n      contents: write   # only this job',
        gain: 'A compromised step can no longer change code or publish packages.' };
    },
    function unpinned(data) {
      const repos = (data.repos || []).filter((r) => (r.hyg_external_actions || 0) >= 5 && (r.hyg_pinned_actions || 0) / r.hyg_external_actions < 0.5);
      if (!repos.length) return null;
      return { phase: 2, area: 'Security', sev: 'med', effort: 'S', repos: repos.map((r) => r.repo), title: 'Pin actions to exact versions',
        problem: `Only ${O.sum(repos, (r) => r.hyg_pinned_actions || 0)} of ${O.sum(repos, (r) => r.hyg_external_actions)} third-party actions are pinned in ${repos.length} repositories. A moved tag can change what runs.`,
        fix: ['Pin each action to its commit SHA, with the version as a comment (pinact does this in one command).', 'Let Dependabot update the pins weekly.'],
        snippet: "# .github/dependabot.yml\nversion: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule: { interval: weekly }",
        gain: 'Supply-chain safety at no cost.' };
    },
    function deprecations(data) {
      const jobs = ((data.logs && data.logs.jobs) || []).filter((j) => (j.deprecations || []).length || (j.failure && j.failure.class === 'config'));
      if (!jobs.length) return null;
      const repos = [...new Set(jobs.map((j) => j.repo))];
      const broken = jobs.filter((j) => j.failure && j.failure.class === 'config').length;
      return { phase: 2, area: 'Reliability', sev: broken ? 'high' : 'med', effort: 'S', repos, title: 'Update outdated actions before they stop working',
        problem: `${repos.length} repositories get "deprecated" warnings from GitHub${broken ? `, and ${broken} jobs already fail on a retired action` : ''}.`,
        fix: ['Move actions to their current major versions.', 'Group these updates into one Dependabot pull request per repository.'],
        gain: 'No surprise breakage when GitHub removes the old versions.' };
    },
    function infraFailures(data) {
      const jobs = ((data.logs && data.logs.jobs) || []).filter((j) => j.failure && (j.failure.class === 'infra' || j.failure.class === 'deps'));
      if (!jobs.length) return null;
      return { phase: 2, area: 'Reliability', sev: 'med', effort: 'S', confidence: 0.8, repos: [...new Set(jobs.map((j) => j.repo))], title: 'Remove flaky downloads and rate limits',
        problem: `${jobs.length} failures were not about the code, e.g. ${distinct(jobs, 2).map((j) => `${O.fmt.short(j.repo)}: "${(j.failure.signature || '').slice(0, 60)}"`).join('; ')}.`,
        fix: ['Pass the CI token to tool installers so they are not rate limited.', 'Cache tools and pin them to versions that exist.', 'Retry network steps once.'],
        snippet: '- uses: arduino/setup-task@v2\n  with:\n    repo-token: ${{ secrets.GITHUB_TOKEN }}',
        gain: 'Fewer red runs that nobody can fix by changing code.' };
    },
    function lowPassRate() {
      const repos = (O.fleet.repos || []).filter((r) => r.decided >= 50 && r.pass < 0.8).sort((a, b) => a.pass - b.pass);
      if (!repos.length) return null;
      return { phase: 2, area: 'Reliability', sev: repos[0].pass < 0.6 ? 'high' : 'med', effort: 'M', repos: repos.map((r) => r.repo), title: 'Fix the repositories where runs usually fail',
        problem: `${repos.slice(0, 3).map((r) => `${r.short}: ${O.fmt.pct(r.pass)} pass`).join(', ')}. When CI is always red, people stop looking.`,
        fix: ['Read the top failure in "Inside a job" and fix it.', 'Move flaky tests to a quarantine list and track them.', 'Delete workflows nobody owns.'],
        gain: 'A red run means something again.' };
    },
    function alwaysSkipped(data) {
      const hits = ((data.tests && data.tests.repos) || []).filter((a) => a.always_skipped >= 2);
      if (!hits.length) return null;
      return { phase: 2, area: 'Tests', sev: 'low', effort: 'M', repos: hits.map((a) => a.repo), title: 'Run or delete tests that never run',
        problem: `${O.sum(hits, (a) => a.always_skipped)} tests are skipped in every CI job (${hits.map((a) => `${O.fmt.short(a.repo)} ${a.always_skipped}`).join(', ')}).`,
        fix: ['Run them nightly with the credentials they need (cloud access through OIDC, not stored keys).', 'Delete the ones nobody will run.'],
        gain: 'Test results you can trust.' };
    },
    // ---- 3. speed up
    function downloads(data) {
      const byRepo = O.groupBy(((data.logs && data.logs.jobs) || []).filter((j) => j.result === 'success'), (j) => j.repo);
      const heavy = [...byRepo.entries()].map(([repo, jobs]) => ({ repo, per: O.sum(jobs, (j) => j.downloads) / jobs.length, miss: O.sum(jobs, (j) => j.cache.miss), hit: O.sum(jobs, (j) => j.cache.hit) })).filter((r) => r.per >= 300 || (r.miss >= 3 && r.miss > r.hit)).sort((a, b) => b.per - a.per);
      if (!heavy.length) return null;
      return { phase: 3, area: 'Speed', sev: 'med', effort: 'S', repos: heavy.map((r) => r.repo), title: 'Cache dependencies instead of downloading them each time',
        problem: `${heavy.slice(0, 2).map((r) => `${O.fmt.short(r.repo)} downloads about ${O.fmt.int(r.per)} files per job`).join('; ')}.`,
        fix: ['Turn on the cache in the setup action (cache: maven, gradle, pip or npm).', 'Save the cache from main so pull requests can reuse it.'],
        snippet: '- uses: actions/setup-java@v5\n  with:\n    distribution: temurin\n    java-version: 21\n    cache: maven',
        gain: 'Usually 30–90 s faster per job, and fewer network failures.' };
    },
    function slowFeedback() {
      const slow = (O.fleet.repos || []).filter((r) => r.decided >= 20 && r.med >= 600).sort((a, b) => b.med - a.med);
      if (!slow.length) return null;
      return { phase: 3, area: 'Speed', sev: 'med', effort: 'M', confidence: 0.8, repos: slow.map((r) => r.repo), title: 'Get slow pipelines under ten minutes',
        problem: `${slow.slice(0, 3).map((r) => `${r.short} takes ${O.fmt.dur(r.med)}`).join(', ')}. After ten minutes people switch tasks.`,
        fix: ['Split long test suites into parallel jobs.', 'Run slow end-to-end tests in the merge queue instead of on every push.', 'Look for steps that wait rather than work in "Inside a job".'],
        gain: 'Faster reviews and fewer context switches.' };
    },
    // ---- 4. prevent
    function blindTests(data) {
      const hits = ((data.tests && data.tests.repos) || []).filter((a) => a.blind_minutes >= 30);
      if (!hits.length) return null;
      return { phase: 4, area: 'Visibility', sev: 'low', effort: 'S', repos: hits.map((a) => a.repo), title: 'Make test jobs say which tests ran',
        problem: `In ${short(hits.map((a) => a.repo))}, test jobs don't list their tests, so slow, flaky or repeated tests stay hidden.`,
        fix: ['Print one line per test (Gradle testLogging, pytest -v, go test -v).', 'Upload JUnit reports and show them in the pull request.'],
        snippet: 'tasks.test {\n  testLogging { events("passed", "skipped", "failed") }\n}',
        gain: 'Problems in tests become visible.' };
    },
    function timeouts(data) {
      const repos = (data.repos || []).filter((r) => r.hyg_files && (r.hyg_timeouts || 0) / r.hyg_files < 0.5);
      if (!repos.length) return null;
      return { phase: 4, area: 'Visibility', sev: 'low', effort: 'S', repos: repos.map((r) => r.repo), title: 'Give every job a time limit',
        problem: `${repos.length} repositories set a time limit in fewer than half of their workflows, so a stuck job can run for 6 hours.`,
        fix: ['Set timeout-minutes on every job, about three times its normal length.'],
        snippet: 'jobs:\n  test:\n    timeout-minutes: 30',
        gain: 'Stuck jobs stop in minutes instead of hours.' };
    },
    function policyGates(data) {
      const jobs = ((data.logs && data.logs.jobs) || []).filter((j) => j.failure && j.failure.class === 'policy');
      if (!jobs.length) return null;
      return { phase: 4, area: 'Process', sev: 'low', effort: 'S', repos: [...new Set(jobs.map((j) => j.repo))], title: 'Make process checks helpful, not just red',
        problem: `${jobs.length} failures came from checks on PR titles, changelogs or generated files.`,
        fix: ['Put the rule in the PR template and a pre-commit hook, so it fails on the laptop first.', 'Let a bot fix what it can and comment the rest.'],
        gain: 'Fewer red runs about formalities.' };
    },
    function deliveryFlow(data) {
      const repos = ((data.signals && data.signals.repos) || []).filter((r) => r.dora && (!r.dora.source || (r.dora.lead_time_h || 0) > 168));
      if (!repos.length) return null;
      const none = repos.filter((r) => !r.dora.source).length;
      return { phase: 4, area: 'Delivery', sev: 'med', effort: 'M', confidence: 0.7, repos: repos.map((r) => r.repo), title: 'Release merged work automatically',
        problem: `${none ? `${none} repositories never release, and ` : ''}merged changes often wait a week or more for a release.`,
        fix: ['Automate releases from main (release-please or semantic-release).', 'Deploy docs and services on merge; release libraries at least weekly.', 'Record deploys in GitHub so delivery can be measured exactly.'],
        gain: 'Changes reach users in hours or days, not weeks.' };
    },
    function agentInstructions(data) {
      const sig = data.signals;
      if (!sig) return null;
      const hits = sig.repos.filter((r) => (r.commits.ai > 0 || r.prs.some((p) => p.agents.length)) && !O.ai.hasInstructions(r));
      if (!hits.length) return null;
      return { phase: 4, area: 'AI', sev: 'med', effort: 'S', repos: hits.map((r) => r.repo), title: 'Tell AI agents how to build and test',
        problem: `${hits.length} repositories accept AI-written changes without an AGENTS.md, so agents guess the build and test commands.`,
        fix: ['Add an organisation template AGENTS.md with setup, build, lint and test commands.', 'Point CLAUDE.md and .github/copilot-instructions.md at it.'],
        snippet: '# AGENTS.md\n## Setup\nmake install\n## Before every commit\nmake lint test\n## Rules\n- Add "Assisted-by: <tool> (<model>)" to AI-assisted commits',
        gain: 'AI changes pass CI more often on the first try.' };
    },
    function aiAttribution(data) {
      const sig = data.signals;
      if (!sig) return null;
      const gated = sig.repos.filter((r) => r.ai_workflows.some((w) => w.kind === 'ai-gate'));
      return { phase: 4, area: 'AI', sev: 'low', effort: 'S', confidence: 0.9, repos: gated.map((r) => r.repo), title: 'Ask everyone to mark AI-assisted commits',
        problem: `${gated.length ? `Only ${short(gated.map((r) => r.repo))} asks for it today, so` : 'Nobody asks for it today, so'} AI use is mostly invisible and tools can't be compared.`,
        fix: ['Agree on one trailer: Assisted-by: <tool> (<model>).', 'Check for it with a commit hook or PR check.', 'Re-run this report monthly to compare tools.'],
        gain: 'Real data on which AI tools help.' };
    },
  ];

  // ================================================================ how it's measured
  O.method = function (data) {
    const meta = data.meta || {};
    const logs = (data.logs && data.logs.totals) || {};
    const tests = (data.tests && data.tests.summary) || {};
    const sig = (data.signals && data.signals.totals) || null;
    const box = O.clear(O.$('#method-body'));
    const section = (title, items, open) => O.el('details', { open: !!open }, O.el('summary', { text: title }), O.el('ul', null, items.filter(Boolean).map((t) => O.el('li', null, t))));
    box.appendChild(section('Where the data comes from', [
      `${O.fmt.int((data.runs || []).length)} CI runs from ${(data.repos || []).length} repositories, ${meta.since || '?'} to ${meta.until || '?'}. Very busy repositories show their newest ${meta.max_runs || 1000} runs.`,
      logs.jobs ? `${logs.jobs} job logs, the latest failed and passed jobs of each repository.` : null,
      tests.repos ? `The test audit reads every job of one recent pull request per repository.` : null,
      sig ? `${sig.prs} pull requests with their commits and reviews, plus releases.` : null,
      'Everything is read from GitHub with read-only requests.',
    ]));
    box.appendChild(section('How the numbers are made', [
      'Run time is from start to finish, capped at 6 hours because re-runs stretch it.',
      'The kind of work in a job comes from step names, the commands they run, and patterns in the log.',
      'Delivery numbers are estimates: releases or deploy runs count as deployments, and quick patch releases or reverts as failed changes.',
      'AI only counts where it is named: an AI account, a "Generated with" note, or a commit line like "Assisted-by: Claude Opus 5.5". A difference between tools is only called when it is bigger than chance.',
    ]));
    box.appendChild(section('Limits', [
      'Only what GitHub shows: no production incidents and no AI use that leaves no trace.',
      'Log samples are recent, not random; numbers from a handful of jobs are hints, not verdicts.',
      'People are never named. Log lines are cleaned of names, emails and tokens.',
    ]));
    const cmd = meta.command || 'ci-observatory --org <your-org>';
    box.appendChild(O.el('details', { open: true }, O.el('summary', { text: 'Run it on your own repositories' }),
      O.el('p', { class: 'muted', style: { marginTop: '8px', fontSize: '14.5px' }, text: 'Works for public and private repositories that your GitHub login can read.' }),
      O.el('div', { class: 'cmd' }, O.el('pre', { text: cmd }), O.el('button', { class: 'btn', type: 'button', text: 'Copy', on: { click: (e) => O.copy(cmd, e.currentTarget) } }))));
    O.clear(O.$('#footer')).appendChild(O.el('span', { text: `Made with ci-observatory ${meta.version || ''}. ${meta.note || ''}` }));
  };
})();
