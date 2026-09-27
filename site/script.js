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
