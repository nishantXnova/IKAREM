/* BAZAAR wire: live "sold" ticker over /ws/ticker. Plain JS, no deps. */
(function () {
  "use strict";
  var wire = document.getElementById("wire");
  var list = document.getElementById("wirelist");
  if (!wire && !list) return;
  var ws;
  try {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(proto + "//" + location.host + "/ws/ticker");
  } catch (e) {
    return;
  }
  ws.onopen = function () {
    try { ws.send("hello from the square"); } catch (e) {}
    var beat = setInterval(function () {
      if (ws.readyState !== 1) clearInterval(beat);
      else { try { ws.send("beat"); } catch (e) {} }
    }, 25000);
  };
  ws.onmessage = function (ev) {
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    var text = "Sold " + (msg.sold || "goods") + " (" + (msg.order || "?") + ")";
    if (wire) wire.textContent = text;
    if (list) {
      var li = document.createElement("li");
      li.textContent = text;
      list.insertBefore(li, list.firstChild);
      while (list.children.length > 6) list.removeChild(list.lastChild);
    }
  };
})();
