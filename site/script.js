// Copy buttons on code blocks. Progressive enhancement, nothing else.
document.querySelectorAll("pre").forEach((pre) => {
  const btn = document.createElement("button");
  btn.className = "copy";
  btn.textContent = "copy";
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pre.innerText);
      btn.textContent = "ok";
    } catch {
      btn.textContent = "nope";
    }
    setTimeout(() => (btn.textContent = "copy"), 1200);
  });
  pre.appendChild(btn);
});

// Sidebar: highlight the section in view.
if ("IntersectionObserver" in window) {
const links = [...document.querySelectorAll(".side a")];
const secs = links
  .map((a) => document.querySelector(a.getAttribute("href")))
  .filter(Boolean);
const io = new IntersectionObserver(
  (es) => {
    es.forEach((e) => {
      if (e.isIntersecting) {
        links.forEach((a) =>
          a.classList.toggle("on", a.getAttribute("href") === "#" + e.target.id),
        );
      }
    });
  },
  { rootMargin: "-40% 0px -55% 0px" },
);
secs.forEach((s) => io.observe(s));
}

const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const hasIO = "IntersectionObserver" in window;

// Reveal-on-scroll: sections ease up once. Content is fully in the DOM
// (SEO/no-JS safe); JS only removes the pre-hidden state on intersect.
// Without IO support the page stays exactly as served — never invisible.
if (!calm && hasIO) {
  const els = [...document.querySelectorAll("main section")];
  els.forEach((s) => s.classList.add("rv"));
  const rvo = new IntersectionObserver(
    (es) => es.forEach((e) => {
      if (e.isIntersecting) {
        e.target.classList.add("in");
        rvo.unobserve(e.target);
      }
    }),
    { threshold: 0.08 },
  );
  els.forEach((s) => rvo.observe(s));
}

// Hero stats count up when visible. Non-numeric values (~110k) stay static.
if (!calm && hasIO) {
  const nums = [...document.querySelectorAll(".stat b")].filter((b) => /^\d+$/.test(b.textContent.trim()));
  if (nums.length) {
    const co = new IntersectionObserver(
      (es) => es.forEach((e) => {
        if (!e.isIntersecting) return;
        const el = e.target;
        co.unobserve(el);
        const end = parseInt(el.textContent.trim(), 10);
        if (!end) return;
        const t0 = performance.now();
        const step = (t) => {
          const k = Math.min(1, (t - t0) / 800);
          el.textContent = String(Math.round(end * (1 - Math.pow(1 - k, 3))));
          if (k < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      }),
      { threshold: 0.4 },
    );
    nums.forEach((b) => co.observe(b));
  }
}

// Terminal typing: types each .term session once, on first view.
// Full text ships in HTML (copy/SEO unaffected); reduced motion skips.
if (!calm && hasIO) {
  document.querySelectorAll(".term pre code").forEach((code) => {
    const full = code.textContent;
    const to = new IntersectionObserver(
      (es) => es.forEach((e) => {
        if (!e.isIntersecting) return;
        to.disconnect();
        let i = 0;
        code.textContent = "";
        const tick = () => {
          i += 3;
          code.textContent = full.slice(0, i);
          if (i < full.length) setTimeout(tick, 14);
          else code.textContent = full;
        };
        tick();
      }),
      { threshold: 0.4 },
    );
    to.observe(code.closest(".term"));
  });
}

// Scroll progress hairline under the topbar.
{
  const bar = document.createElement("div");
  bar.className = "progress";
  bar.setAttribute("aria-hidden", "true");
  document.querySelector(".topbar").appendChild(bar);
  let queued = false;
  const paint = () => {
    queued = false;
    const h = document.documentElement;
    const max = h.scrollHeight - h.clientHeight;
    bar.style.transform = `scaleX(${max > 0 ? h.scrollTop / max : 0})`;
  };
  addEventListener("scroll", () => {
    if (!queued) {
      queued = true;
      requestAnimationFrame(paint);
    }
  }, { passive: true });
  paint();
}
