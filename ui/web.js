// Runs inside the user's browser tab (atticus.py --web, through AppleScript): read the page, click or fill by number.
// One expression, called as (src)(cmd, n, text). Numbers come from the last 'read' (data-atticus on each control).
(function (cmd, n, text) {
  const clean = s => (s || '').trim().replace(/\s+/g, ' ');
  const secret = e => e.type === 'password';   // a password never leaves the page, not even as a label
  const name = e => clean(e.getAttribute('aria-label') || e.innerText || (secret(e) ? '' : e.value) || e.placeholder || e.title || e.name || (secret(e) ? 'password' : '')).slice(0, 60);
  const isBox = e => e.isContentEditable || e.tagName === 'TEXTAREA' || (e.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|image|file)$/.test(e.type));
  if (cmd === 'ready') {
    const busy = [...document.querySelectorAll('[aria-busy="true"]')].some(e => { const r=e.getBoundingClientRect();return r.width&&r.height&&getComputedStyle(e).visibility!=='hidden'; });
    let hash=0;for(const c of (document.body?.innerText||'').slice(0,3000))hash=(hash*31+c.charCodeAt(0))|0;
    return JSON.stringify({ready:document.readyState!=='loading'&&!!document.body&&!busy,signature:location.href+'|'+hash});
  }
  if (cmd === 'read') {
    document.querySelectorAll('[data-atticus]').forEach(e => e.removeAttribute('data-atticus'));
    const seen = [];
    for (const e of document.querySelectorAll('a[href],button,input:not([type=hidden]),textarea,select,[contenteditable=true],[contenteditable=""],[role=button],[role=link],[role=textbox],[role=tab],[role=menuitem],[role=checkbox],[role=option],[role=combobox]')) {
      const r = e.getBoundingClientRect(), s = getComputedStyle(e);
      if (!r.width || !r.height || s.visibility === 'hidden' || e.disabled) continue;
      const label = name(e), box = isBox(e);
      if (!label && !box && e.tagName !== 'SELECT') continue;
      const kind = box ? 'box' : e.tagName === 'SELECT' ? 'menu' : e.type === 'checkbox' || e.getAttribute('role') === 'checkbox' ? 'checkbox' : e.tagName === 'A' ? 'link' : 'button';
      seen.push({ e, kind, label, box });
    }
    // Boxes and buttons first, links after: a long page's links must not crowd out its form. 150 keeps the reply small.
    const out = [...seen.filter(c => c.kind !== 'link'), ...seen.filter(c => c.kind === 'link')].slice(0, 150).map(({ e, kind, label, box }, i) => {
      e.setAttribute('data-atticus', i + 1);
      return `${i + 1} ${kind}: ${label}${box && e.value && !secret(e) ? ` = "${String(e.value).slice(0, 40)}"` : ''}`;
    });
    const body = (document.body.innerText || '').replace(/\n\s*\n+/g, '\n').slice(0, 3000);
    return `${location.href}\n${document.title}\n\n${body}\n\nControls:\n${out.join('\n')}`;
  }
  const e = document.querySelector(`[data-atticus="${n}"]`);
  if (!e) return `No control ${n}. Read the page again.`;
  e.scrollIntoView({ block: 'center' }); e.focus();
  // name: everything that says what a click does (full label, link, form that posts), for the Allow check; plus the tab it is in
  if (cmd === 'name') return JSON.stringify({ what: clean([e.getAttribute('aria-label'), e.innerText, secret(e) ? '' : e.value, e.title, e.href, e.form && e.form.method === 'post' && (e.type === 'submit' || e.tagName === 'BUTTON') ? 'submits a form ' + e.form.action : ''].filter(Boolean).join(' ')), short: name(e), url: location.href });
  if (text && cmd === 'click' && location.href !== text) return 'The tab changed since the check. Read the page again.';
  if (cmd === 'click') {   // the full press, for sites that listen to mousedown/up rather than click
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) e.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
    e.click();
    return `Clicked ${n}: ${name(e)}`;
  }
  if (cmd === 'fill') {
    if (e.tagName === 'SELECT') {
      const o = [...e.options].find(o => clean(o.text).toLowerCase().includes(text.toLowerCase()));
      if (!o) return `No option "${text}" in ${n}.`;
      e.value = o.value;
    } else if (e.isContentEditable) {
      document.execCommand('selectAll'); document.execCommand('insertText', false, text);
    } else if (isBox(e)) {   // the native setter, so React and other frameworks see the change
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(e), 'value').set.call(e, text);
    } else return `${n} is not a box.`;
    e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true }));
    return `Filled ${n}: ${name(e)}` + (secret(e) ? '' : ` = "${(e.value ?? e.innerText).slice(0, 60)}"`);
  }
  return 'Use read, click or fill.';
})
