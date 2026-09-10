(function () {
  var box = document.getElementById('progress');
  if (!box || box.dataset.running !== '1') return;
  var text = document.getElementById('progress-text');

  // reloading would throw away anything half-typed (a cik in the add box, a
  // search). if there's typed input just say it finished and let me reload.
  function typedInput() {
    if (document.querySelector('.ticker-drawer[open]')) return true;
    var fields = document.querySelectorAll(
      'input[type="text"], input[type="search"], textarea');
    for (var i = 0; i < fields.length; i++) {
      if (fields[i].value) return true;
    }
    return false;
  }

  function poll() {
    fetch('/api/progress', {cache: 'no-store'})
      .then(function (r) { return r.json(); })
      .then(function (job) {
        if (!job.running) {
          if (typedInput()) {
            text.textContent = job.job_error
              ? 'Check failed: ' + job.job_error + '. You can run another check.'
              : 'Check finished. Reload the page to see the results.';
            return;
          }
          window.location.reload();
          return;
        }
        text.textContent = 'Checking ' + (job.current || '') +
          ' — ' + job.done + ' of ' + job.total;
        setTimeout(poll, 500);
      })
      .catch(function () { setTimeout(poll, 2000); });
  }

  setTimeout(poll, 500);
})();

// the filter tabs and search box on the fund list. the rows are all in the
// page already, this only hides the ones that don't match.
(function () {
  var tabs = document.querySelectorAll('[data-filter]');
  var search = document.getElementById('search');
  var count = document.getElementById('row-count');
  if (!tabs.length || !search) return;
  var rows = document.querySelectorAll('tbody tr[data-state]');
  var state = 'all';

  function apply() {
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var ok = (state === 'all' || row.dataset.state.split(' ').indexOf(state) !== -1)
            && (!q || row.dataset.search.indexOf(q) !== -1);
      row.hidden = !ok;
      if (ok) shown++;
    }
    if (count) {
      var word = rows.length === 1 ? ' fund' : ' funds';
      count.textContent = shown === rows.length
        ? rows.length + word
        : shown + ' of ' + rows.length + word;
    }
  }

  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener('click', function () {
      state = this.dataset.filter;
      for (var j = 0; j < tabs.length; j++) {
        tabs[j].setAttribute('aria-pressed', String(tabs[j] === this));
      }
      apply();
    });
  }
  search.addEventListener('input', apply);

  // "/" jumps to the search box, like the kbd hint next to it says
  document.addEventListener('keydown', function (event) {
    var tag = (document.activeElement || {}).tagName || '';
    if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(tag)) {
      event.preventDefault();
      search.focus();
    }
  });
})();

(function () {
  var frame = document.getElementById('filing');
  if (!frame) return;

  var count = document.getElementById('match-count');
  var prev = document.getElementById('match-prev');
  var next = document.getElementById('match-next');
  var input = document.getElementById('date-input');
  var filingTile = document.getElementById('filing-tile');
  var formTile = document.getElementById('form-tile');

  // the filing frame can't report focus (opaque origin, :focus-within never
  // reaches it) so the parent sets the class on any message from the frame
  // and hands it back when the form takes focus.
  function focusTile(active, idle) {
    if (active) active.classList.add('focused');
    if (idle) idle.classList.remove('focused');
  }

  if (formTile) {
    formTile.addEventListener('focusin', function () {
      focusTile(formTile, filingTile);
    });
  }

  var total = 0;        // every date in the filing
  var greenCount = 0;   // how many are likely
  var walk = [];        // indices prev/next step through
  var at = -1;          // position within walk

  function label() {
    if (total === 0) { count.textContent = 'no dates'; return; }
    var tail = greenCount ? greenCount + ' likely · ' + total + ' dates'
                          : total + ' dates';
    count.textContent = (at < 0 ? '' : (at + 1) + ' / ') + tail;
  }

  function step(to) {
    if (!walk.length) return;
    at = (to + walk.length) % walk.length;
    frame.contentWindow.postMessage({type: 'goto', index: walk[at]}, '*');
    label();
  }

  window.addEventListener('message', function (event) {
    // frame is sandboxed without allow-same-origin so its origin is opaque
    // and can't be compared to a url. check the source instead.
    if (event.source !== frame.contentWindow) return;
    var data = event.data || {};
    focusTile(filingTile, formTile);

    if (data.type === 'count') {
      total = data.total;
      greenCount = (data.green || []).length;
      walk = greenCount ? data.green.slice() : [];
      if (!walk.length) { for (var i = 0; i < total; i++) walk.push(i); }
      at = -1;
      label();
    }

    if (data.type === 'at') {
      var pos = walk.indexOf(data.index);
      if (pos !== -1) at = pos;
      label();
    }

    // clicking a highlight fills the date field. I had to see it in context
    // to click it, and nothing is ever pre-filled.
    if (data.type === 'pick' && input) input.value = data.iso;
  });

  prev.addEventListener('click', function () { step(at - 1); });
  next.addEventListener('click', function () { step(at + 1); });
  label();
})();

// One small drawer for every ticker arrow. Forms also work as ordinary pages.
(function () {
  if (!window.HTMLDialogElement) return;
  var drawer = document.createElement('dialog');
  drawer.className = 'ticker-drawer';
  drawer.setAttribute('aria-label', 'Edit tickers');
  document.body.appendChild(drawer);
  var opener, generation = 0;

  function close() { generation++; drawer.close(); }
  drawer.addEventListener('close', function () {
    document.documentElement.classList.remove('ticker-drawer-open');
    if (opener) opener.focus();
  });
  drawer.addEventListener('cancel', function () { generation++; });
  drawer.addEventListener('click', function (event) {
    if (event.target.closest('[data-ticker-close]')) {
      event.preventDefault(); close();
    } else if (event.target === drawer) {
      var rect = drawer.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right) close();
    }
  });

  function show(html) {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var panel = doc.querySelector('[data-ticker-panel]');
    if (!panel) throw new Error('Ticker panel unavailable');
    drawer.replaceChildren(panel);
    var row = opener && opener.closest('tr');
    if (row) {
      row.querySelector('[data-ticker-value]').textContent = panel.dataset.tickers || '—';
      row.dataset.search = row.dataset.searchBase + ' ' + panel.dataset.tickers.toLowerCase();
      var search = document.getElementById('search');
      if (search) search.dispatchEvent(new Event('input'));
    }
    var field = drawer.querySelector('#new-ticker');
    if (field) field.focus();
  }
  function report() {
    var error = document.createElement('p');
    error.className = 'result bad';
    error.setAttribute('role', 'alert');
    error.textContent = 'Could not confirm the change. Close and reopen the drawer to check the saved tickers before retrying.';
    drawer.appendChild(error);
  }
  document.addEventListener('click', function (event) {
    var link = event.target.closest('[data-ticker-open]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    opener = link;
    var current = ++generation;
    drawer.innerHTML = '<p role="status">Loading tickers…</p><button type="button" data-ticker-close>Close</button>';
    document.documentElement.classList.add('ticker-drawer-open');
    drawer.showModal();
    fetch(link.href, {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('Load failed');
      return r.text();
    }).then(function (html) { if (current === generation) show(html); })
      .catch(function () { if (current === generation) report(); });
  });
  var initial = new URLSearchParams(window.location.search).get('open_tickers');
  if (initial && /^\d+$/.test(initial)) {
    var initialLink = document.querySelector('[data-ticker-open][href="/fund/' + initial + '/tickers"]');
    if (initialLink) initialLink.click();
    var cleanURL = new URL(window.location.href);
    cleanURL.searchParams.delete('open_tickers');
    window.history.replaceState(null, '', cleanURL);
  }
  drawer.addEventListener('submit', function (event) {
    event.preventDefault();
    var form = event.target;
    var data = new URLSearchParams(new FormData(form));
    var submitter = event.submitter || form.querySelector('button[name="intent"]');
    if (submitter) data.set(submitter.name, submitter.value);
    var current = generation;
    drawer.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    fetch(form.action, {method: 'POST', body: data, cache: 'no-store'})
      .then(function (r) { return r.text(); })
      .then(function (html) { if (current === generation) show(html); })
      .catch(function () { if (current === generation) report(); });
  });
})();

// The research tree. Every row is a real link; with script the click fetches
// the same page's fragment and drops it under the row instead of navigating.
(function () {
  var tree = document.getElementById('tree');
  if (!tree) return;
  var frame = document.getElementById('research-frame');
  var title = document.getElementById('research-title');
  var external = document.getElementById('research-external');
  var viewerTile = document.getElementById('viewer-tile');
  var search = document.getElementById('research-search');

  function childrenOf(link) {
    var line = link.closest('.tree-line');
    var next = line && line.nextElementSibling;
    return next && next.hasAttribute('data-tree-children') ? next : null;
  }

  function setArrow(link, open) {
    var c = link.querySelector('.tree-c');
    if (c && /[▸▾]/.test(c.textContent)) c.textContent = open ? '▾' : '▸';
    link.setAttribute('aria-expanded', String(open));
  }

  // fetch a fragment into the row's children box. resolves with the box.
  function expand(link) {
    var box = childrenOf(link);
    if (!box) return Promise.resolve(null);
    if (link.getAttribute('aria-expanded') === 'true') {
      box.hidden = true; setArrow(link, false);
      return Promise.resolve(box);
    }
    if (box.dataset.loaded === '1') {
      box.hidden = false; setArrow(link, true);
      return Promise.resolve(box);
    }
    box.innerHTML = '<p class="tree-note" role="status">Loading…</p>';
    box.hidden = false; setArrow(link, true);
    return fetch(link.dataset.treeExpand, {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.text();
    }).then(function (html) {
      box.innerHTML = html;
      box.dataset.loaded = '1';
      var auto = box.querySelector('[data-open]');
      if (auto) {
        var doc = box.querySelector('a[data-doc][href="' + auto.dataset.open + '"]');
        if (doc) openDocument(doc);
      }
      return box;
    }).catch(function (err) {
      box.innerHTML = '<p class="tree-note bad" role="alert">Could not load: ' + err.message +
        ' <a href="' + link.getAttribute('href') + '">Retry</a></p>';
      setArrow(link, false); // so the next click retries instead of just collapsing this
      return box;
    });
  }

  function select(link) {
    var current = tree.querySelector('a.selected');
    if (current) current.classList.remove('selected');
    link.classList.add('selected');
  }

  function openDocument(link) {
    select(link);
    if (viewerTile) viewerTile.classList.add('loading');
    title.textContent = 'Loading ' + (link.dataset.title || '') + '…';
    frame.src = link.getAttribute('href');
    external.href = 'https://www.sec.gov' +
      (link.getAttribute('href') || '').replace(/^\/research\/(\d+)\/(\d{10})-(\d{2})-(\d{6})\//,
        '/Archives/edgar/data/$1/$2$3$4/');
    external.hidden = false;
    location.hash = link.getAttribute('href').replace(/^\/research\//, '');
  }

  frame.addEventListener('load', function () {
    if (viewerTile) viewerTile.classList.remove('loading');
    var selected = tree.querySelector('a.selected[data-doc]');
    if (selected) title.textContent = selected.dataset.title || '';
  });

  tree.addEventListener('click', function (event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    var more = event.target.closest('[data-tree-more]');
    if (more) {
      event.preventDefault();
      var group = more.closest('.tree-group');
      group.querySelectorAll('[data-tree-rest]').forEach(function (el) { el.hidden = false; });
      more.closest('.tree-line').remove();
      return;
    }
    var doc = event.target.closest('a[data-doc]');
    if (doc) { event.preventDefault(); openDocument(doc); return; }
    var fold = event.target.closest('a[data-tree-expand]');
    if (fold) {
      event.preventDefault();
      expand(fold).then(function () {
        if (fold.getAttribute('aria-expanded') !== 'true') return;
        var isFiling = /^\/research\/\d+\/\d{10}-\d{2}-\d{6}$/.test(fold.getAttribute('href'));
        var place = fold.getAttribute('href').replace(/^\/research\//, '');
        // a filing expand is the new place. a fund expand only records itself
        // when no document is open, so browsing roots doesn't lose the document.
        // skip if expanding already auto-opened this filing's one document -
        // that hash is longer and more specific, don't clobber it back down
        if (location.hash.indexOf('#' + place + '/') === 0) return;
        if (isFiling || !tree.querySelector('a.selected')) {
          location.hash = place;
        }
      });
    }
  });

  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      tree.querySelectorAll('[data-tree-root]').forEach(function (root) {
        root.classList.toggle('filtered', !!q && root.dataset.search.indexOf(q) === -1);
      });
    });
    document.addEventListener('keydown', function (event) {
      var tag = (document.activeElement || {}).tagName || '';
      if (event.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(tag)) {
        event.preventDefault(); search.focus();
      }
    });
  }

  // #cik, #cik/accession or #cik/accession/document reopens the same place.
  // the server already rendered the fund open when the url carried it, so
  // only the parts the hash adds are fetched.
  function restore() {
    var m = /^#(\d+)(?:\/(\d{10}-\d{2}-\d{6}))?(?:\/([A-Za-z0-9._-]+))?$/.exec(location.hash);
    if (!m) return;
    var root = tree.querySelector('[data-tree-root][data-cik="' + m[1] + '"]');
    if (!root) return;
    var fundLink = root.querySelector('a[data-tree-expand]');
    var step = fundLink.getAttribute('aria-expanded') === 'true'
      ? Promise.resolve(childrenOf(fundLink)) : expand(fundLink);
    if (!m[2]) return;
    step.then(function (box) {
      if (!box) return;
      var filing = box.querySelector('a[data-tree-expand][href$="/' + m[2] + '"]');
      if (!filing) return;
      var rest = filing.closest('[data-tree-rest]');
      if (rest) rest.hidden = false;
      filing.closest('.tree-group').open = true;
      var next = filing.getAttribute('aria-expanded') === 'true'
        ? Promise.resolve(childrenOf(filing)) : expand(filing);
      next.then(function (docs) {
        if (!docs) return;
        if (m[3]) {
          var doc = docs.querySelector('a[data-doc][href$="/' + m[3] + '"]');
          if (doc && frame.src.indexOf(doc.getAttribute('href')) === -1) openDocument(doc);
          return;
        }
        // the hash named a filing but no document, open the one the filing auto-opens
        var auto = docs.querySelector('[data-open]');
        var target = auto && docs.querySelector('a[data-doc][href="' + auto.dataset.open + '"]');
        if (target) openDocument(target);
      });
    });
  }
  restore();

  // a direct filing page renders the document open server-side, but the frame
  // starts empty - open it the same way expand() does when the hash triggers it
  if (!location.hash && frame.src === 'about:blank') {
    var served = tree.querySelector('[data-tree-children][data-loaded] [data-open]');
    var doc = served && tree.querySelector('a[data-doc][href="' + served.dataset.open + '"]');
    if (doc) openDocument(doc);
  }
})();
