(() => {
  const toggle = document.querySelector('[data-nav-toggle]');
  const sidebar = document.querySelector('[data-sidebar]');
  if (!toggle || !sidebar) return;
  const setOpen = (open) => {
    document.body.classList.toggle('nav-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  };
  toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
  sidebar.addEventListener('click', (event) => {
    if (event.target.closest('a')) setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setOpen(false);
  });
})();

(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  document.querySelectorAll('[data-gallery]').forEach((gallery) => {
    const track = gallery.querySelector('[data-gallery-track]');
    const slides = Array.from(gallery.querySelectorAll('[data-gallery-slide]'));
    if (!track || slides.length < 2) return;

    const controls = document.createElement('div');
    controls.className = 'gallery-controls';

    const previous = document.createElement('button');
    previous.className = 'gallery-button';
    previous.type = 'button';
    previous.setAttribute('aria-label', 'Previous screenshot');
    previous.textContent = '←';

    const dots = document.createElement('div');
    dots.className = 'gallery-dots';
    dots.setAttribute('aria-label', 'Choose a screenshot');

    const counter = document.createElement('span');
    counter.className = 'gallery-counter';
    counter.setAttribute('aria-live', 'polite');
    counter.setAttribute('aria-atomic', 'true');

    const next = document.createElement('button');
    next.className = 'gallery-button';
    next.type = 'button';
    next.setAttribute('aria-label', 'Next screenshot');
    next.textContent = '→';

    controls.append(previous, dots, counter, next);
    gallery.append(controls);

    let activeIndex = 0;
    const dotButtons = slides.map((slide, index) => {
      const dot = document.createElement('button');
      const caption = slide.dataset.caption || `Screenshot ${index + 1}`;
      dot.className = 'gallery-dot';
      dot.type = 'button';
      dot.setAttribute('aria-label', `Go to ${caption}`);
      dots.append(dot);
      return dot;
    });

    const setActive = (index) => {
      activeIndex = index;
      dotButtons.forEach((dot, dotIndex) => {
        if (dotIndex === index) dot.setAttribute('aria-current', 'true');
        else dot.removeAttribute('aria-current');
      });
      previous.disabled = index === 0;
      next.disabled = index === slides.length - 1;
      counter.textContent = `${index + 1} / ${slides.length}`;
    };

    const showSlide = (index) => {
      setActive(index);
      slides[index].scrollIntoView({
        behavior: reducedMotion.matches ? 'auto' : 'smooth',
        block: 'nearest',
        inline: 'center',
      });
    };

    dotButtons.forEach((dot, index) => {
      dot.addEventListener('click', () => showSlide(index));
    });
    previous.addEventListener('click', () => showSlide(Math.max(0, activeIndex - 1)));
    next.addEventListener('click', () => showSlide(Math.min(slides.length - 1, activeIndex + 1)));

    const ratios = new Map(slides.map((slide) => [slide, 0]));
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => ratios.set(entry.target, entry.intersectionRatio));
      let visibleIndex = activeIndex;
      let visibleRatio = 0;
      slides.forEach((slide, index) => {
        const ratio = ratios.get(slide);
        if (ratio > visibleRatio) {
          visibleIndex = index;
          visibleRatio = ratio;
        }
      });
      if (visibleRatio > 0) setActive(visibleIndex);
    }, { root: track, threshold: [0.5, 0.75, 1] });

    slides.forEach((slide) => observer.observe(slide));
    setActive(0);
  });
})();

(() => {
  // A lightbox on top of the gallery, not instead of its links. Without this
  // script every slide is still an anchor to the full-size file, which is what
  // the page did before and what it falls back to.
  const slideLinks = Array.from(document.querySelectorAll('[data-gallery-slide] a'));
  if (!slideLinks.length) return;

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let opener = null;
  let group = [];
  let index = 0;

  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.hidden = true;

  const figure = document.createElement('figure');
  figure.className = 'lightbox-figure';
  const image = document.createElement('img');
  const caption = document.createElement('figcaption');
  figure.append(image, caption);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'lightbox-close';
  close.setAttribute('aria-label', 'Close');
  close.textContent = '✕';

  const previous = document.createElement('button');
  previous.type = 'button';
  previous.className = 'lightbox-step lightbox-previous';
  previous.setAttribute('aria-label', 'Previous screenshot');
  previous.textContent = '←';

  const next = document.createElement('button');
  next.type = 'button';
  next.className = 'lightbox-step lightbox-next';
  next.setAttribute('aria-label', 'Next screenshot');
  next.textContent = '→';

  overlay.append(close, previous, figure, next);
  document.body.append(overlay);

  const show = (position) => {
    index = (position + group.length) % group.length;
    const link = group[index];
    const slide = link.closest('[data-gallery-slide]');
    image.src = link.getAttribute('href');
    image.alt = link.querySelector('img')?.alt || '';
    caption.textContent = slide?.dataset.caption || '';
    overlay.setAttribute('aria-label', caption.textContent || 'Screenshot');
    const single = group.length < 2;
    previous.hidden = single;
    next.hidden = single;
  };

  const open = (link) => {
    const gallery = link.closest('[data-gallery]');
    group = gallery
      ? Array.from(gallery.querySelectorAll('[data-gallery-slide] a'))
      : [link];
    opener = link;
    show(group.indexOf(link));
    overlay.hidden = false;
    // The page behind must not scroll away under the overlay.
    document.body.classList.add('lightbox-open');
    close.focus({ preventScroll: true });
  };

  const dismiss = () => {
    if (overlay.hidden) return;
    overlay.hidden = true;
    document.body.classList.remove('lightbox-open');
    image.removeAttribute('src');
    // Back to the picture that was clicked, not to the top of the document.
    opener?.focus({ preventScroll: reducedMotion.matches });
    opener = null;
  };

  slideLinks.forEach((link) => {
    link.addEventListener('click', (event) => {
      // Let a modified click do what the browser would: open in a new tab.
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      open(link);
    });
  });

  close.addEventListener('click', dismiss);
  previous.addEventListener('click', () => show(index - 1));
  next.addEventListener('click', () => show(index + 1));
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) dismiss();
  });

  document.addEventListener('keydown', (event) => {
    if (overlay.hidden) return;
    if (event.key === 'Escape') dismiss();
    else if (event.key === 'ArrowLeft' && group.length > 1) show(index - 1);
    else if (event.key === 'ArrowRight' && group.length > 1) show(index + 1);
    else if (event.key === 'Tab') {
      // Keep the tab ring inside the dialog while it is open.
      const stops = [close, previous, next].filter((button) => !button.hidden);
      const at = stops.indexOf(document.activeElement);
      const step = event.shiftKey ? -1 : 1;
      event.preventDefault();
      stops[(at + step + stops.length) % stops.length].focus();
    }
  });
})();
