// Cadence progressive enhancement: toggle check-ins without a reload.
// Every form also submits normally, so the app works fully with JS off.
(function () {
  var csrf = document.querySelector('meta[name="csrf-token"]');
  var token = csrf ? csrf.getAttribute("content") : "";
  if (!token || !window.fetch) return;

  function recount() {
    var rows = document.querySelectorAll(".today-row");
    if (!rows.length) return;
    var done = document.querySelectorAll(".today-row.done").length;
    var bar = document.querySelector(".pbar i");
    var label = document.querySelector(".progress span");
    var pct = Math.round((done / rows.length) * 100);
    if (bar) bar.style.width = pct + "%";
    if (label) label.textContent = done + " of " + rows.length + " done · " + pct + "%";
  }

  document.querySelectorAll("form.toggle-form").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var hid = form.getAttribute("data-habit");
      var day = form.getAttribute("data-day");
      fetch("/api/habits/" + encodeURIComponent(hid) + "/toggle", {
        method: "POST",
        headers: { "content-type": "application/json", "x-csrf-token": token },
        body: JSON.stringify({ day: day }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("toggle failed");
          return r.json();
        })
        .then(function (res) {
          var row = form.closest(".today-row");
          if (row) {
            row.classList.toggle("done", res.done);
            row.classList.toggle("todo", !res.done);
            var btn = row.querySelector(".toggle");
            if (btn) {
              var name = btn.getAttribute("aria-label") || "";
              btn.setAttribute(
                "aria-label",
                res.done ? name.replace("Mark done", "Done") : name.replace("Done", "Mark done")
              );
            }
          }
          var big = form.querySelector("button.btn");
          if (big) {
            big.textContent = res.done ? "✓ Done today" : "Mark today done";
            big.classList.toggle("done", res.done);
          }
          var dot = form.closest("li, .panel, main");
          if (dot) {
            var target = dot.querySelector('.wdot[title="' + day + '"]');
            if (target) {
              if (res.done) {
                target.classList.add("done");
                target.classList.remove("miss");
              } else {
                target.classList.remove("done");
                if (!target.classList.contains("today")) target.classList.add("miss");
              }
            }
          }
          recount();
        })
        .catch(function () {
          form.submit(); // fall back to a full POST on any failure
        });
    });
  });
})();
