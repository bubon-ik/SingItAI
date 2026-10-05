// SingIt landing motion. GSAP + ScrollTrigger, fully gated behind
// prefers-reduced-motion and page visibility. Without JS or GSAP the
// page is static and complete.

(function () {
  // Allowance card demo: "Revoke in wallet" shows what one signature does,
  // and pressing it again puts the example back.
  var card = document.querySelector('.allow-card');
  var revoke = document.querySelector('[data-allow-revoke]');
  if (card && revoke) {
    var parts = {
      state: card.querySelector('[data-allow-state]'),
      left: card.querySelector('[data-allow-left]'),
      of: card.querySelector('[data-allow-of]'),
      foot: card.querySelector('[data-allow-foot]')
    };
    var active = {
      state: parts.state.textContent, left: parts.left.textContent,
      of: parts.of.textContent, foot: parts.foot.textContent, button: revoke.textContent
    };
    revoke.addEventListener('click', function () {
      var revoked = card.classList.toggle('is-revoked');
      parts.state.textContent = revoked ? 'Revoked' : active.state;
      parts.left.textContent = revoked ? '$0.00' : active.left;
      parts.of.textContent = revoked ? 'Your agent can no longer spend' : active.of;
      parts.foot.textContent = revoked ? 'Your USDC never left your wallet. Approve again whenever you like.' : active.foot;
      revoke.textContent = revoked ? 'Show the example again' : active.button;
    });
  }

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce || typeof gsap === 'undefined') return;

  // Init motion only while the page is actually visible. In hidden or
  // headless contexts rAF is frozen; creating from-tweens there would leave
  // the page stuck at opacity 0. If we load hidden, wait for visibility.
  if (document.visibilityState === 'visible') {
    initMotion();
  } else {
    document.addEventListener('visibilitychange', function onVis() {
      if (document.visibilityState === 'visible') {
        document.removeEventListener('visibilitychange', onVis);
        initMotion();
      }
    });
  }

  function initMotion() {
    gsap.registerPlugin(ScrollTrigger);
    var ease = 'power3.out';

    // Watchdog: if rAF is frozen (throttled webview, headless capture),
    // tweens would hold their from-state forever. Timers still run there,
    // so after 2.5s of no ticker progress we drop to the static page.
    var f0 = gsap.ticker.frame;
    setTimeout(function () {
      if (gsap.ticker.frame - f0 >= 5) return;
      ScrollTrigger.getAll().forEach(function (st) { st.kill(); });
      gsap.globalTimeline.getChildren(true, true, false).forEach(function (t) { t.kill(); });
      gsap.set(['[data-hero-reveal] > *', '[data-hero-card]', '.allow-rows > div',
        '.allow-activity li', '[data-batch] > *', '[data-reveal]', '.hero h1 .w'],
        { clearProps: 'all' });
    }, 2500);

    // Hero headline: word-by-word mask reveal.
    var h1 = document.querySelector('.hero h1');
    if (h1) {
      wrapWords(h1);
      var words = h1.querySelectorAll('.w');
      gsap.set(words, { yPercent: 112 });
      gsap.to(words, { yPercent: 0, duration: 1, ease: 'power4.out', stagger: 0.05, delay: 0.1 });
    }
    gsap.from('[data-hero-reveal] > :not(h1)', {
      y: 24, opacity: 0, duration: 0.9, ease: ease, stagger: 0.08, delay: 0.3
    });

    // The allowance arrives with the statement; today's purchases tick in.
    gsap.from('[data-hero-card]', { y: 24, opacity: 0, duration: 0.9, ease: ease, delay: 0.35 });
    gsap.from('.allow-activity li', { opacity: 0, duration: 0.4, ease: ease, stagger: 0.12, delay: 0.8 });

    // Batched grids: staggered rise.
    gsap.utils.toArray('[data-batch]').forEach(function (grid) {
      gsap.from(grid.children, {
        y: 24, opacity: 0, duration: 0.7, ease: ease, stagger: 0.08,
        scrollTrigger: { trigger: grid, start: 'top 85%' }
      });
    });

    // Single reveals.
    gsap.utils.toArray('[data-reveal]').forEach(function (el) {
      gsap.from(el, {
        y: 24, opacity: 0, duration: 0.8, ease: ease,
        scrollTrigger: { trigger: el, start: 'top 88%' }
      });
    });
  }

  // Wrap each word of the headline in an overflow mask + sliding inner span.
  function wrapWords(root) {
    Array.prototype.slice.call(root.childNodes).forEach(function (node) {
      if (node.nodeType === 3) {
        var frag = document.createDocumentFragment();
        node.textContent.split(/(\s+)/).forEach(function (part) {
          if (!part) return;
          if (/^\s+$/.test(part)) { frag.appendChild(document.createTextNode(part)); return; }
          var mask = document.createElement('span');
          mask.className = 'w-mask';
          var inner = document.createElement('span');
          inner.className = 'w';
          inner.textContent = part;
          mask.appendChild(inner);
          frag.appendChild(mask);
        });
        root.replaceChild(frag, node);
      } else if (node.nodeType === 1 && node.tagName !== 'BR') {
        wrapWords(node);
      }
    });
  }
})();
