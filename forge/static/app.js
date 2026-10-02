/* FORGE shop scripts: live crew chat over /ws/hall. Plain JS, no deps. */
(function () {
  "use strict";
  var cfg = window.FORGE_CHAT;
  if (!cfg) return;
  var form = document.getElementById("chatform");
  var box = document.getElementById("chatbox");
  var talk = document.getElementById("talk");
  if (!form || !box || !talk) return;

  function line(name, body, when) {
    var li = document.createElement("li");
    var b = document.createElement("b");
    b.textContent = name;
    var meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = when ? " " + when : "";
    var br = document.createElement("br");
    var span = document.createElement("span");
    span.textContent = body;
    li.appendChild(b);
    li.appendChild(meta);
    li.appendChild(br);
    li.appendChild(span);
    talk.appendChild(li);
  }

  var ws;
  try {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/hall");
  } catch (e) {
    return;
  }
  ws.onopen = function () {
    ws.send(JSON.stringify({ project: cfg.project, name: cfg.name }));
  };
  ws.onmessage = function (ev) {
    try {
      var msg = JSON.parse(ev.data);
      if (msg.sys) {
        var li = document.createElement("li");
        li.className = "muted";
        li.textContent = msg.sys;
        talk.appendChild(li);
      } else {
        line(msg.name || "crew", msg.body || "", "live");
      }
    } catch (e) {
      /* ignore malformed frames */
    }
  };
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = box.value.trim();
    if (!text || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ body: text }));
    line(cfg.name + " (you)", text, "");
    box.value = "";
  });
})();
