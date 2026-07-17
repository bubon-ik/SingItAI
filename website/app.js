// SingIt landing motion. GSAP + ScrollTrigger, fully gated behind
// prefers-reduced-motion. Without JS or GSAP the page is static and complete.

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

  // 3D tilt on the approval card: pointer position drives --tx/--ty.
  var bezel = document.querySelector('.approval-bezel');
  if (bezel && !reduce && window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
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

  // Hero entrance: copy rises, card settles into its tilt.
  gsap.from('[data-hero-reveal] > *', {
    y: 44, opacity: 0, duration: 1.1, ease: lux, stagger: 0.09, delay: 0.15
  });
  gsap.from('[data-hero-card]', {
    y: 70, opacity: 0, rotate: 5, duration: 1.4, ease: lux, delay: 0.35
  });

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

  // Steps: heavy fade-up as each card enters, active number while in focus band.
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

  // Batched grids (bad options, bento): staggered rise.
  gsap.utils.toArray('[data-batch]').forEach(function (grid) {
    gsap.from(grid.children, {
      y: 48, opacity: 0, duration: 0.9, ease: lux, stagger: 0.1,
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
  }
})();
