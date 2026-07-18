// SingIt landing motion. GSAP + ScrollTrigger, fully gated behind
// prefers-reduced-motion and page visibility. Without JS or GSAP the
// page is static and complete.

(function () {
  // Approval card demo state (works with or without GSAP).
  var card = document.querySelector('.approval-card');
  var approve = document.querySelector('.btn-approve');
  var reject = document.querySelector('.btn-reject');
  if (card && approve && reject) {
    approve.addEventListener('click', function () {
      card.classList.remove('is-rejected'); card.classList.add('is-approved');
    });
    reject.addEventListener('click', function () {
      card.classList.remove('is-approved'); card.classList.add('is-rejected');
    });
  }

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var finePointer = window.matchMedia('(hover: hover) and (pointer: fine)').matches;

  // 3D tilt on the approval card: pointer position drives --tx/--ty.
  var bezel = document.querySelector('.approval-bezel');
  if (bezel && !reduce && finePointer) {
    bezel.addEventListener('pointermove', function (e) {
      var r = bezel.getBoundingClientRect();
      var x = (e.clientX - r.left) / r.width - 0.5;
      var y = (e.clientY - r.top) / r.height - 0.5;
      bezel.style.setProperty('--tx', (x * 7).toFixed(2) + 'deg');
      bezel.style.setProperty('--ty', (y * -7).toFixed(2) + 'deg');
    });
    bezel.addEventListener('pointerleave', function () {
      bezel.style.setProperty('--tx', '0deg');
      bezel.style.setProperty('--ty', '0deg');
    });
  }

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
    var lux = 'power3.out';

    // Watchdog: if rAF is frozen (throttled webview, headless capture),
    // tweens would hold their from-state forever. Timers still run there,
    // so after 2.5s of no ticker progress we drop to the static page.
    var f0 = gsap.ticker.frame;
    setTimeout(function () {
      if (gsap.ticker.frame - f0 >= 5) return;
      ScrollTrigger.getAll().forEach(function (st) { st.kill(); });
      gsap.globalTimeline.getChildren(true, true, false).forEach(function (t) { t.kill(); });
      document.documentElement.classList.add('motion-dead');
      gsap.set(['.nav-pill', '[data-hero-reveal] > *', '[data-hero-card]',
        '.approval-rows > div', '.terminal > *', '[data-step]',
        '[data-batch] > *', '[data-reveal]', '.cta-watermark',
        '.orb-a', '.orb-b', '.orb-c', '.marquee-track',
        '.hero h1 .w', '[data-scrub] .w'], { clearProps: 'all' });
    }, 2500);

    // Nav pill drops in, then compresses once the page is scrolled.
    gsap.from('.nav-pill', { y: -90, opacity: 0, duration: 1, ease: lux });
    ScrollTrigger.create({
      start: 'top -90',
      toggleClass: { targets: '.nav-pill', className: 'is-scrolled' }
    });

    // Hero headline: word-by-word mask reveal (storytelling entrance).
    var h1 = document.querySelector('.hero h1');
    if (h1) {
      wrapWords(h1);
      var h1words = h1.querySelectorAll('.w');
      gsap.set(h1words, { yPercent: 112 });
      gsap.to(h1words, {
        yPercent: 0, duration: 1.05, ease: 'power4.out', stagger: 0.055, delay: 0.15
      });
    }
    gsap.from('[data-hero-reveal] > :not(h1)', {
      y: 40, opacity: 0, duration: 1.1, ease: lux, stagger: 0.09, delay: 0.3
    });
    gsap.from('[data-hero-card]', {
      y: 70, opacity: 0, rotate: 5, duration: 1.4, ease: lux, delay: 0.4
    });
    // The policy request assembles row by row inside the card.
    gsap.from('.approval-rows > div', {
      opacity: 0, x: 18, duration: 0.6, ease: lux, stagger: 0.09, delay: 1
    });

    // Ambient orbs drift at different speeds while scrolling (depth).
    gsap.to('.orb-a', { y: 160, ease: 'none', scrollTrigger: { start: 0, end: 'max', scrub: 1.2 } });
    gsap.to('.orb-b', { y: -220, ease: 'none', scrollTrigger: { start: 0, end: 'max', scrub: 1.2 } });
    gsap.to('.orb-c', { y: 120, ease: 'none', scrollTrigger: { start: 0, end: 'max', scrub: 1.2 } });

    // Magnetic pull on pill buttons (feedback, fine pointers only).
    if (finePointer) {
      document.querySelectorAll('.hero-cta .btn, .cta .btn, .nav-pill .btn').forEach(function (btn) {
        var qx = gsap.quickTo(btn, 'x', { duration: 0.4, ease: 'power3' });
        var qy = gsap.quickTo(btn, 'y', { duration: 0.4, ease: 'power3' });
        var qs = gsap.quickTo(btn, 'scale', { duration: 0.25, ease: 'power3' });
        btn.addEventListener('pointermove', function (e) {
          var r = btn.getBoundingClientRect();
          qx((e.clientX - r.left - r.width / 2) * 0.18);
          qy((e.clientY - r.top - r.height / 2) * 0.3);
        });
        btn.addEventListener('pointerleave', function () { qx(0); qy(0); qs(1); });
        btn.addEventListener('pointerdown', function () { qs(0.97); });
        btn.addEventListener('pointerup', function () { qs(1); });
      });
    }

    // Scrubbing text reveal: words resolve from 0.13 to 1 as the user scrolls.
    var scrub = document.querySelector('[data-scrub]');
    if (scrub) {
      var words = scrub.textContent.trim().split(/\s+/);
      scrub.innerHTML = words.map(function (w) { return '<span class="w">' + w + '</span>'; }).join(' ');
      gsap.to(scrub.querySelectorAll('.w'), {
        opacity: 1, stagger: 0.6, ease: 'none',
        scrollTrigger: { trigger: scrub, start: 'top 78%', end: 'bottom 40%', scrub: true }
      });
    }

    // Pinned split: policy artifact holds while the steps scroll past.
    ScrollTrigger.matchMedia({
      '(min-width: 961px)': function () {
        ScrollTrigger.create({
          trigger: '.how-grid',
          start: 'top 96px',
          end: 'bottom bottom',
          pin: '[data-pin]',
          pinSpacing: false
        });
      }
    });

    // The policy artifact assembles as it enters (storytelling).
    gsap.from('.terminal > *', {
      y: 26, opacity: 0, duration: 0.8, ease: lux, stagger: 0.14,
      scrollTrigger: { trigger: '.terminal', start: 'top 78%' }
    });

    // Steps: heavy fade-up, active number while in the focus band.
    gsap.utils.toArray('[data-step]').forEach(function (el) {
      gsap.from(el, {
        y: 56, opacity: 0, duration: 0.9, ease: lux,
        scrollTrigger: { trigger: el, start: 'top 88%' }
      });
      ScrollTrigger.create({
        trigger: el,
        start: 'top 62%',
        end: 'bottom 38%',
        toggleClass: { targets: el, className: 'is-active' }
      });
    });

    // Batched grids (bad options, bento): staggered rise with slight scale.
    gsap.utils.toArray('[data-batch]').forEach(function (grid) {
      gsap.from(grid.children, {
        y: 48, opacity: 0, scale: 0.96, transformOrigin: '50% 100%',
        duration: 0.9, ease: lux, stagger: 0.1,
        scrollTrigger: { trigger: grid, start: 'top 85%' }
      });
    });

    // Single reveals.
    gsap.utils.toArray('[data-reveal]').forEach(function (el) {
      gsap.from(el, {
        y: 40, opacity: 0, duration: 1, ease: lux,
        scrollTrigger: { trigger: el, start: 'top 88%' }
      });
    });

    // Kinetic marquee: GSAP-driven, accelerates with scroll velocity.
    var track = document.querySelector('.marquee-track');
    if (track) {
      document.querySelector('.marquee').classList.add('js-marquee');
      var marqueeTween = gsap.to(track, { xPercent: -50, ease: 'none', duration: 40, repeat: -1 });
      ScrollTrigger.create({
        onUpdate: function (self) {
          var boost = Math.min(Math.abs(self.getVelocity()) / 260, 5);
          if (boost > 1) {
            marqueeTween.timeScale(boost);
            gsap.to(marqueeTween, { timeScale: 1, duration: 1.2, overwrite: true, ease: 'power2.out' });
          }
        }
      });
    }

    // CTA watermark slowly rotates through the section (depth).
    gsap.fromTo('.cta-watermark', { rotate: -10 }, {
      rotate: 8, ease: 'none',
      scrollTrigger: { trigger: '.cta', start: 'top bottom', end: 'bottom top', scrub: 1 }
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
