// Quick-bid pills fill the amount field; the board polls so the "cost to take
// #1" number does not go stale while somebody is deciding.
(function () {
  var amount = document.querySelector('input[name="amount"]');
  document.querySelectorAll('.pill[data-bid]').forEach(function (pill) {
    pill.addEventListener('click', function () {
      if (!amount) return;
      amount.value = pill.getAttribute('data-bid');
      amount.focus();
    });
  });

  // A cover image is somebody else's URL, so it can rot. Swap dead ones for
  // the plain lettered tile instead of showing a broken-image icon.
  document.querySelectorAll('img[data-initial]').forEach(function (img) {
    img.addEventListener('error', function () {
      var tile = document.createElement('span');
      tile.className = img.className + ' thumb-blank';
      tile.textContent = img.getAttribute('data-initial') || '?';
      img.replaceWith(tile);
    });
  });

  var ticks = document.querySelectorAll('[data-tick]');
  if (!ticks.length) return;
  var fmt = function (n) { return '$' + Number(n).toLocaleString('en-US'); };

  setInterval(function () {
    fetch('/api/board', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        ticks.forEach(function (el) {
          var key = el.getAttribute('data-tick');
          var value = data.stats[key];
          if (value === undefined) return;
          var next = key === 'listings' ? String(value) : fmt(value);
          if (el.textContent.trim() !== next) {
            el.textContent = next;
            el.animate(
              [{ color: '#ff3fa4' }, { color: '' }],
              { duration: 1200, easing: 'ease-out' }
            );
          }
        });
      })
      .catch(function () {});
  }, 20000);
})();
