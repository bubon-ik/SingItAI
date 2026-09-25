// Pixel starfield background. Vanilla port of the background-pixel-stars
// React component: 16-bit palette, 5px star pixels, twinkle, periodic
// regeneration, and pixelated shooting stars, capped at 16 FPS.

(function () {
  var canvas = document.getElementById('pixel-stars');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  if (!ctx) return;

  var STAR_COLORS = ['#FFFFFF', '#FFFFAA', '#AAAAFF', '#FFAAAA', '#AAFFAA', '#FFAAFF', '#AAFFFF'];

  var starDensity = 0.00004;
  var twinkleProbability = 0.7;
  var minTwinkleSpeed = 2;
  var maxTwinkleSpeed = 4;
  var pixelSize = 5;
  var starRegenerationInterval = 5000;
  var percentToRegenerate = 0.15;

  var shootingStarPixelSize = 2;
  var targetFps = 16;
  var frameInterval = 1000 / targetFps;

  var stars = [];
  var shootingStars = [];
  var lastRenderTime = 0;

  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function makeStar() {
    return {
      x: Math.floor(Math.random() * (canvas.width / pixelSize)) * pixelSize,
      y: Math.floor(Math.random() * (canvas.height / pixelSize)) * pixelSize,
      color: STAR_COLORS[Math.floor(Math.random() * STAR_COLORS.length)],
      baseOpacity: Math.random() * 0.5 + 0.5,
      currentOpacity: 0,
      twinkle: Math.random() < twinkleProbability,
      twinkleSpeed: minTwinkleSpeed + Math.random() * (maxTwinkleSpeed - minTwinkleSpeed),
      twinkleDirection: -1,
      twinkleTimer: 0
    };
  }

  function initStars() {
    stars = [];
    var numStars = Math.floor(canvas.width * canvas.height * starDensity);
    for (var i = 0; i < numStars; i++) {
      var s = makeStar();
      s.currentOpacity = s.baseOpacity;
      stars.push(s);
    }
  }

  function regenerateStars() {
    if (!stars.length) return;
    var n = Math.max(1, Math.floor(stars.length * percentToRegenerate));
    for (var i = 0; i < n; i++) {
      var idx = Math.floor(Math.random() * stars.length);
      var s = makeStar();
      s.currentOpacity = s.baseOpacity;
      stars[idx] = s;
    }
  }

  function createShootingStar() {
    shootingStars.push({
      x: Math.random() * window.innerWidth,
      y: 0,
      angle: 45 + Math.random() * 90,
      speed: Math.random() * 5 + 8,
      distance: 0,
      trail: []
    });
    setTimeout(createShootingStar, Math.random() * 4000 + 2000);
  }

  function drawStars() {
    stars.forEach(function (star) {
      ctx.fillStyle = star.color;
      ctx.globalAlpha = star.currentOpacity;
      ctx.fillRect(star.x, star.y, pixelSize, pixelSize);

      if (star.twinkle) {
        star.twinkleTimer += 1 / targetFps;
        if (star.twinkleTimer >= star.twinkleSpeed) {
          star.twinkleTimer = 0;
          star.twinkleDirection *= -1;
        }
        var progress = star.twinkleTimer / star.twinkleSpeed;
        if (progress < 0.5) {
          star.currentOpacity = star.twinkleDirection < 0 ? star.baseOpacity : star.baseOpacity * 0.3;
        } else {
          star.currentOpacity = star.twinkleDirection < 0 ? star.baseOpacity * 0.3 : star.baseOpacity;
        }
      }
    });
    ctx.globalAlpha = 1;
  }

  function animate(timestamp) {
    requestAnimationFrame(animate);
    if (timestamp - lastRenderTime < frameInterval) return;
    lastRenderTime = timestamp;

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawStars();

    shootingStars = shootingStars
      .map(function (star) {
        var rad = (star.angle * Math.PI) / 180;
        if (star.distance % 8 < star.speed) {
          star.trail.push({ x: star.x, y: star.y, opacity: 1 });
        }
        star.trail = star.trail
          .map(function (p) { return { x: p.x, y: p.y, opacity: p.opacity - 0.1 }; })
          .filter(function (p) { return p.opacity > 0; });
        star.x += star.speed * Math.cos(rad);
        star.y += star.speed * Math.sin(rad);
        star.distance += star.speed;
        return star;
      })
      .filter(function (star) {
        return star.x >= -30 && star.x <= window.innerWidth + 30 &&
               star.y >= -30 && star.y <= window.innerHeight + 30;
      });

    shootingStars.forEach(function (star) {
      star.trail.forEach(function (p) {
        ctx.fillStyle = 'rgba(180, 242, 255, ' + p.opacity.toFixed(2) + ')';
        ctx.fillRect(p.x, p.y, shootingStarPixelSize, shootingStarPixelSize);
      });
      ctx.fillStyle = '#ffffff';
      for (var y = 0; y < 2; y++) {
        for (var x = 0; x < 4; x++) {
          if ((x === 0 && y === 1) || (x === 3 && y === 0)) continue;
          ctx.fillRect(
            star.x + x * shootingStarPixelSize,
            star.y + y * shootingStarPixelSize,
            shootingStarPixelSize,
            shootingStarPixelSize
          );
        }
      }
    });
  }

  function resize() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    initStars();
    if (reduce) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      drawStars();
    }
  }

  window.addEventListener('resize', resize);
  resize();

  if (!reduce) {
    requestAnimationFrame(animate);
    setTimeout(createShootingStar, Math.random() * 4000 + 2000);
    setInterval(regenerateStars, starRegenerationInterval);
  }
})();
