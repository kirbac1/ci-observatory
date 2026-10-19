/* CI Observatory web app: start an analysis, follow it live, open the report. No libraries. Every server string is
   inserted as text, never as HTML. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const el = (tag, props, ...kids) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'text') node.textContent = v;
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
      else node.setAttribute(k, v);
    }
    for (const kid of kids) if (kid != null) node.append(kid);
    return node;
  };
  const ERRORS = {
    denied: 'Sign-in was cancelled.',
    expired: 'That sign-in link expired. Please try again.',
    github: 'GitHub did not complete the sign-in. Please try again.',
    'login-off': 'Signing in is not set up on this server.',
  };

  let me = null;
  let source = null;
  let timer = null;
  let current = null;

  function message(text, opts) {
    const box = $('message');
    box.replaceChildren();
    if (!text) { box.hidden = true; return; }
    box.append(text);
    if (opts && opts.login && me && me.login_enabled && !me.signed_in) box.append(' ', el('a', { href: loginHref(), text: 'Sign in with GitHub →' }));
    if (opts && opts.install_url) box.append(' ', el('a', { href: opts.install_url, target: '_blank', rel: 'noopener', text: 'Install the app →' }));
    box.hidden = false;
  }

  function loginHref() {
    const repo = $('repo').value.trim();
    // After signing in, come back here and start the analysis that was typed in.
    const next = repo ? '/?repo=' + encodeURIComponent(repo) + '&days=' + $('days').value + '&run=1' : '/';
    return '/login?next=' + encodeURIComponent(next);
  }

  function renderAuth() {
    const box = $('auth');
    box.replaceChildren();
    if (me.signed_in) {
      if (me.avatar) box.append(el('img', { src: me.avatar + (me.avatar.includes('?') ? '&' : '?') + 's=52', alt: '' }));
      box.append(el('span', { class: 'who', text: '@' + me.login }));
      box.append(el('button', { class: 'btn small', type: 'button', text: 'Sign out', on: { click: signOut } }));
    } else if (me.login_enabled) {
      box.append(el('a', { class: 'btn small', href: loginHref(), text: 'Sign in with GitHub', on: { mouseenter: (e) => { e.currentTarget.href = loginHref(); } } }));
    }
    const hint = $('hint');
    hint.replaceChildren();
    if (me.signed_in) {
      hint.append('Signed in: public and private repositories you can read. A private one needs the app installed on its owner');
      if (me.install_url) hint.append(' (', el('a', { href: me.install_url, target: '_blank', rel: 'noopener', text: 'install' }), ')');
      hint.append('.');
    } else if (me.anonymous) {
      hint.append('Public repositories work without signing in.');
      if (me.login_enabled) hint.append(' ', el('a', { href: loginHref(), text: 'Sign in with GitHub' }), ' for private ones and fuller samples.');
    } else if (me.login_enabled) {
      hint.append(el('a', { href: loginHref(), text: 'Sign in with GitHub' }), ' to analyse a repository.');
    }
  }

  async function signOut() {
    await fetch('/logout', { method: 'POST' }).catch(() => null);
    location.href = '/';
  }

  async function renderRecent() {
    const rows = await fetch('/api/recent').then((r) => r.json()).catch(() => []);
    const list = $('recent');
    list.replaceChildren(...rows.map((row) => el('li', null, el('a', { href: row.report_url },
      el('b', { text: row.repo }), el('span', { text: `${row.days} days · ${ago(row.finished)}` })))));
    $('recent-wrap').hidden = rows.length === 0;
  }

  function ago(seconds) {
    const mins = Math.max(0, Math.round((Date.now() / 1000 - seconds) / 60));
    return mins < 1 ? 'just now' : mins < 60 ? `${mins} min ago` : `${Math.round(mins / 60)} h ago`;
  }

  function clock(view) {
    clearInterval(timer);
    const tick = () => {
      const from = view.started || view.created;
      const to = view.finished || Date.now() / 1000;
      const s = Math.max(0, Math.round(to - from));
      $('job-time').textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    };
    tick();
    if (!view.finished) timer = setInterval(tick, 1000);
  }

  function step(n, final) {
    for (const li of $('steps').children) {
      const k = Number(li.dataset.step);
      li.className = final === 'done' || k < n ? 'done' : k === n && !final ? 'now' : '';
    }
  }

  function show(view) {
    current = view;
    $('job').hidden = false;
    $('job-title').textContent = view.repo + (view.private ? ' · private' : '');
    const states = { queued: view.position ? `Waiting · number ${view.position} in line` : 'Waiting', running: 'Analysing', done: 'Done', failed: 'Stopped', cancelled: 'Cancelled' };
    $('job-state').textContent = states[view.status] || view.status;
    $('cancel').hidden = !['queued', 'running'].includes(view.status) || !view.mine;
    const open = $('open');
    open.hidden = view.status !== 'done';
    if (view.report_url) open.href = view.report_url;
    const err = $('job-error');
    err.hidden = !view.error;
    err.textContent = view.error || '';
    if (view.status === 'queued') step(0);
    if (view.status === 'done') step(5, 'done');
    clock(view);
  }

  function follow(view) {
    if (source) source.close();
    $('log').textContent = '';
    show(view);
    if (view.status === 'done') { go(view); return; }
    source = new EventSource(view.events_url);
    source.addEventListener('line', (e) => {
      const line = JSON.parse(e.data);
      const log = $('log');
      const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.append(line + '\n');
      if (atEnd) log.scrollTop = log.scrollHeight;
      const m = /^\[(\d)\/4\]/.exec(line);
      if (m) step(Number(m[1]));
    });
    source.addEventListener('status', (e) => {
      const next = JSON.parse(e.data);
      show(Object.assign(next, { mine: view.mine }));
      if (['done', 'failed', 'cancelled'].includes(next.status)) {
        source.close();
        source = null;
        $('go').disabled = false;
        if (next.status === 'done') go(next);
        renderRecent();
      }
    });
    source.onerror = () => {
      // The browser reconnects by itself; if the analysis is gone, say so once.
      if (source && source.readyState === EventSource.CLOSED) $('job-error').hidden = false;
    };
  }

  function go(view) {
    $('go').disabled = false;
    $('job-state').textContent = 'Done · opening the report';
    setTimeout(() => { location.href = view.report_url; }, 900);
  }

  async function start(ev) {
    if (ev) ev.preventDefault();
    message('');
    const repo = $('repo').value.trim();
    if (!repo) { message('Type a repository, such as cli/cli.'); $('repo').focus(); return; }
    $('go').disabled = true;
    let res, body;
    try {
      res = await fetch('/api/analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ repo, days: Number($('days').value) }) });
      body = await res.json();
    } catch (e) {
      $('go').disabled = false;
      message('The server could not be reached. Check your connection and try again.');
      return;
    }
    if (!res.ok) {
      $('go').disabled = false;
      message(body.error || body.detail || 'Something went wrong.', body);
      return;
    }
    history.replaceState(null, '', '/?repo=' + encodeURIComponent(body.repo) + '&days=' + body.days);
    follow(Object.assign(body, { mine: res.status === 202 }));
  }

  async function cancel() {
    if (!current) return;
    await fetch('/api/analyses/' + current.id, { method: 'DELETE' }).catch(() => null);
  }

  async function init() {
    me = await fetch('/api/me').then((r) => r.json()).catch(() => ({ login_enabled: false, anonymous: true }));
    const params = new URLSearchParams(location.search);
    if (params.get('repo')) $('repo').value = params.get('repo');
    if (['7', '14', '30', '60', '90'].includes(params.get('days'))) $('days').value = params.get('days');
    renderAuth();
    renderRecent();
    if (ERRORS[params.get('error')]) message(ERRORS[params.get('error')]);
    $('form').addEventListener('submit', start);
    $('cancel').addEventListener('click', cancel);
    for (const b of $('examples').querySelectorAll('button')) b.addEventListener('click', () => { $('repo').value = b.dataset.repo; start(); });
    if (params.get('repo') && params.get('run') === '1') start();
  }

  init();
})();
