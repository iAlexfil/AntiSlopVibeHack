(() => {
  'use strict';

  const canvas = document.getElementById('heroCanvas');
  const hero = canvas && canvas.closest('.hero');
  if (!canvas || !hero) return;

  const context = canvas.getContext('2d', { alpha: true, desynchronized: true });
  if (!context) return;

  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
  const columns = 35;
  const rows = 29;
  const stride = 5;
  const vertices = new Float32Array(columns * rows * stride);
  const edgeOpacity = new Float32Array(columns * rows);
  const frameInterval = 1000 / 30;
  const pointer = { x: 0, y: 0, targetX: 0, targetY: 0 };
  let width = 1;
  let height = 1;
  let scale = 1;
  let mobile = false;
  let time = 2.1;
  let frame = 0;
  let lastFrame = 0;
  let visible = true;
  let suspended = false;
  let atmosphere;
  let horizontalMask;
  let verticalMask;

  canvas.style.pointerEvents = 'none';

  for (let row = 0; row < rows; row += 1) {
    const v = row / (rows - 1);
    for (let column = 0; column < columns; column += 1) {
      const u = column / (columns - 1);
      edgeOpacity[row * columns + column] = Math.pow(
        Math.sin(u * Math.PI) * Math.sin(v * Math.PI), 0.45
      );
    }
  }

  function projectSurface() {
    const yaw = -0.34 + pointer.x * 0.025;
    const cosine = Math.cos(yaw);
    const sine = Math.sin(yaw);
    const pitch = 0.62 + pointer.y * 0.015;
    const cosinePitch = Math.cos(pitch);
    const sinePitch = Math.sin(pitch);
    const centerX = width * (mobile ? 0.65 : 0.76) + pointer.x * 9;
    const centerY = height * (mobile ? 0.78 : 0.56) + pointer.y * 7;

    for (let row = 0; row < rows; row += 1) {
      const v = row / (rows - 1);
      const z = (v - 0.5) * 5.6;
      for (let column = 0; column < columns; column += 1) {
        const u = column / (columns - 1);
        const x = (u - 0.5) * 8;
        const radial = Math.sqrt((x - 0.3) ** 2 + (z + 0.2) ** 2);
        const crest = Math.exp(-((x - 0.45) ** 2 / 4.3 + (z + 0.5) ** 2 / 3.5));
        const wave = Math.sin(x * 1.04 + z * 0.48 - time * 0.65) * 0.48;
        const ripple = Math.sin(radial * 2.3 - time * 0.85) * 0.13;
        const y = crest * 1.65 + wave + ripple + Math.cos(z * 1.25 + time * 0.38) * 0.24;
        const rotatedX = x * cosine + z * sine;
        const rotatedZ = z * cosine - x * sine;
        const projectedY = rotatedZ * sinePitch - y * cosinePitch;
        const depth = rotatedZ * cosinePitch + y * sinePitch;
        const perspective = 7.5 / (9 + depth);
        const index = (row * columns + column) * stride;
        vertices[index] = centerX + rotatedX * scale * perspective;
        vertices[index + 1] = centerY + projectedY * scale * perspective;
        vertices[index + 2] = perspective;
        vertices[index + 3] = y;
        vertices[index + 4] = edgeOpacity[row * columns + column];
      }
    }
  }

  function drawThreads() {
    context.lineWidth = 0.65;

    // The two sets of threads share the same surface, rather than connecting
    // arbitrary particles. Their different brightness makes its depth legible.
    for (let column = 1; column < columns - 1; column += 1) {
      const first = column * stride;
      const last = ((rows - 1) * columns + column) * stride;
      const fade = context.createLinearGradient(
        vertices[first], vertices[first + 1], vertices[last], vertices[last + 1]
      );
      const opacity = Math.sin(column / (columns - 1) * Math.PI) * 0.17;
      fade.addColorStop(0, 'rgba(120, 215, 235, 0)');
      fade.addColorStop(0.25, `rgba(120, 215, 235, ${opacity})`);
      fade.addColorStop(0.7, `rgba(120, 215, 235, ${opacity * 0.8})`);
      fade.addColorStop(1, 'rgba(120, 215, 235, 0)');
      context.strokeStyle = fade;
      context.beginPath();
      for (let row = 0; row < rows; row += 1) {
        const index = (row * columns + column) * stride;
        if (row === 0) context.moveTo(vertices[index], vertices[index + 1]);
        else context.lineTo(vertices[index], vertices[index + 1]);
      }
      context.stroke();
    }

    const scanPosition = (time * 0.07) % 1;
    for (let row = 1; row < rows - 1; row += 1) {
      const v = row / (rows - 1);
      const distance = Math.abs(v - scanPosition);
      const scan = Math.exp(-(distance ** 2) / 0.004);
      const opacity = Math.pow(Math.sin(v * Math.PI), 0.65) * (0.2 + scan * 0.38);
      const first = row * columns * stride;
      const last = (row * columns + columns - 1) * stride;
      const fade = context.createLinearGradient(
        vertices[first], vertices[first + 1], vertices[last], vertices[last + 1]
      );
      fade.addColorStop(0, 'rgba(157, 233, 244, 0)');
      fade.addColorStop(0.2, `rgba(157, 233, 244, ${opacity * 0.55})`);
      fade.addColorStop(0.58, `rgba(157, 233, 244, ${opacity})`);
      fade.addColorStop(0.84, `rgba(157, 233, 244, ${opacity * 0.7})`);
      fade.addColorStop(1, 'rgba(157, 233, 244, 0)');
      context.strokeStyle = fade;
      context.lineWidth = 0.75 + scan * 0.55;
      context.beginPath();
      for (let column = 0; column < columns; column += 1) {
        const index = (row * columns + column) * stride;
        if (column === 0) context.moveTo(vertices[index], vertices[index + 1]);
        else context.lineTo(vertices[index], vertices[index + 1]);
      }
      context.stroke();
    }
  }

  function drawPoints() {
    // Five paths keep drawing bounded; no per-particle gradients or blurs.
    for (let band = 0; band < 5; band += 1) {
      context.fillStyle = `rgba(191, 240, 248, ${0.13 + band * 0.13})`;
      context.beginPath();
      for (let index = 0; index < vertices.length; index += stride) {
        const edge = vertices[index + 4];
        const brightness = edge * (0.55 + Math.max(0, vertices[index + 3]) * 0.23);
        const selectedBand = Math.min(4, Math.floor(brightness * 4.8));
        if (edge < 0.045 || selectedBand !== band) continue;
        const radius = (0.42 + band * 0.1) * vertices[index + 2];
        context.moveTo(vertices[index] + radius, vertices[index + 1]);
        context.arc(vertices[index], vertices[index + 1], radius, 0, Math.PI * 2);
      }
      context.fill();
    }
  }

  function paint() {
    context.clearRect(0, 0, width, height);
    context.fillStyle = atmosphere;
    context.fillRect(0, 0, width, height);
    projectSurface();
    drawThreads();
    drawPoints();

    // Fade behind the headline and into the section edges. The actual page
    // background stays in CSS, so no rectangular canvas boundary appears.
    context.globalCompositeOperation = 'destination-in';
    context.fillStyle = horizontalMask;
    context.fillRect(0, 0, width, height);
    context.fillStyle = verticalMask;
    context.fillRect(0, 0, width, height);
    context.globalCompositeOperation = 'source-over';
  }

  function resize() {
    width = Math.max(1, hero.clientWidth);
    height = Math.max(1, hero.clientHeight);
    mobile = width < 700;
    scale = mobile
      ? Math.min(width * 0.19, height * 0.12)
      : Math.min(width * 0.112, height * 0.185);
    const pixelRatio = Math.min(
      window.devicePixelRatio || 1,
      1.5,
      Math.sqrt(3500000 / (width * height))
    );
    canvas.width = Math.max(1, Math.round(width * pixelRatio));
    canvas.height = Math.max(1, Math.round(height * pixelRatio));
    context.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);

    const glowX = width * (mobile ? 0.72 : 0.79);
    const glowY = height * (mobile ? 0.78 : 0.53);
    const radius = Math.max(width * 0.38, height * 0.48);
    atmosphere = context.createRadialGradient(glowX, glowY, 0, glowX, glowY, radius);
    atmosphere.addColorStop(0, 'rgba(61, 125, 147, 0.13)');
    atmosphere.addColorStop(0.45, 'rgba(40, 89, 115, 0.055)');
    atmosphere.addColorStop(1, 'rgba(40, 89, 115, 0)');

    horizontalMask = context.createLinearGradient(0, 0, width, 0);
    horizontalMask.addColorStop(0, 'rgba(0, 0, 0, 0)');
    horizontalMask.addColorStop(mobile ? 0.12 : 0.29, 'rgba(0, 0, 0, 0)');
    horizontalMask.addColorStop(mobile ? 0.45 : 0.58, 'rgba(0, 0, 0, 1)');
    horizontalMask.addColorStop(1, 'rgba(0, 0, 0, 1)');
    verticalMask = context.createLinearGradient(0, 0, 0, height);
    verticalMask.addColorStop(0, 'rgba(0, 0, 0, 0)');
    verticalMask.addColorStop(mobile ? 0.5 : 0.14, 'rgba(0, 0, 0, 0)');
    verticalMask.addColorStop(mobile ? 0.66 : 0.32, 'rgba(0, 0, 0, 1)');
    verticalMask.addColorStop(0.84, 'rgba(0, 0, 0, 1)');
    verticalMask.addColorStop(1, 'rgba(0, 0, 0, 0)');
    paint();
  }

  function animate(timestamp) {
    frame = 0;
    if (!canAnimate()) return;
    if (!lastFrame) lastFrame = timestamp - frameInterval;
    const elapsed = timestamp - lastFrame;
    if (elapsed >= frameInterval - 0.5) {
      const delta = Math.min(elapsed / 1000, 0.08);
      time += delta;
      const ease = 1 - Math.exp(-delta * 3.5);
      pointer.x += (pointer.targetX - pointer.x) * ease;
      pointer.y += (pointer.targetY - pointer.y) * ease;
      paint();
      lastFrame = timestamp - (elapsed % frameInterval);
    }
    frame = window.requestAnimationFrame(animate);
  }

  function canAnimate() {
    return visible && !document.hidden && !suspended && !motionPreference.matches;
  }

  function syncAnimation() {
    if (frame) window.cancelAnimationFrame(frame);
    frame = 0;
    lastFrame = 0;
    if (canAnimate()) frame = window.requestAnimationFrame(animate);
    else if (motionPreference.matches) {
      pointer.x = 0;
      pointer.y = 0;
      paint();
    }
  }

  hero.addEventListener('pointermove', (event) => {
    if (event.pointerType === 'touch' || motionPreference.matches) return;
    const bounds = hero.getBoundingClientRect();
    pointer.targetX = Math.max(-1, Math.min(1, (event.clientX - bounds.left) / width * 2 - 1));
    pointer.targetY = Math.max(-1, Math.min(1, (event.clientY - bounds.top) / height * 2 - 1));
  }, { passive: true });
  hero.addEventListener('pointerleave', () => {
    pointer.targetX = 0;
    pointer.targetY = 0;
  }, { passive: true });

  if ('ResizeObserver' in window) new ResizeObserver(resize).observe(hero);
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('visibilitychange', syncAnimation);
  window.addEventListener('pagehide', () => {
    suspended = true;
    syncAnimation();
  });
  window.addEventListener('pageshow', () => {
    suspended = false;
    syncAnimation();
  });
  if (motionPreference.addEventListener) {
    motionPreference.addEventListener('change', syncAnimation);
  } else {
    motionPreference.addListener(syncAnimation);
  }
  if ('IntersectionObserver' in window) {
    new IntersectionObserver((entries) => {
      visible = entries[0].isIntersecting;
      syncAnimation();
    }, { rootMargin: '80px' }).observe(hero);
  }

  resize();
  syncAnimation();
})();
