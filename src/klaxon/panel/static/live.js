// Klaxon's web panel: the live view (week 7, item 2). It only reads.
// Every 2 seconds it asks this panel whether the run's folder changed (GET /run/<name>/state), and reloads the page
// when it did. It counts down a waiting request's time left, and copies the make approve command with the browser's
// own clipboard. It sends nothing anywhere else, and it never approves: that is typed at a terminal.
"use strict";
(function () {
  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function tick() {
    document.querySelectorAll("[data-expires]").forEach(function (timer) {
      var left = Math.round((Date.parse(timer.getAttribute("data-expires")) - Date.now()) / 1000);
      timer.textContent =
        left > 0 ? "expires in " + Math.floor(left / 60) + ":" + pad(left % 60) : "time is up: no answer means denied";
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(button.getAttribute("data-copy")).then(
        function () {
          button.textContent = "Copied: paste it in your own terminal";
        },
        function () {
          button.textContent = "Could not copy: select the command above";
        }
      );
    });
  });

  if (document.querySelector("[data-expires]")) {
    tick();
    setInterval(tick, 1000);
  }

  var marker = document.querySelector("[data-state]");
  if (marker && marker.getAttribute("data-live") === "true") {
    var seen = marker.getAttribute("data-version");
    setInterval(function () {
      fetch(marker.getAttribute("data-state"), { cache: "no-store", credentials: "same-origin" })
        .then(function (answer) {
          return answer.ok ? answer.json() : null;
        })
        .then(function (state) {
          if (state && (state.version !== seen || state.live !== true)) {
            location.reload();
          }
        })
        .catch(function () {});
    }, 2000);
  }
})();
