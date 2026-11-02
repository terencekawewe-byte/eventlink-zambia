(function () {
  "use strict";

  var root = document.getElementById("gate-scan");
  if (!root) return;

  var verifyUrl = root.getAttribute("data-verify-url");
  var form = document.getElementById("manual-form");
  var codeInput = document.getElementById("manual-code");
  var manualSubmit = document.getElementById("manual-submit");
  var csrfToken = form.querySelector("input[name='csrf_token']").value;
  var resultPanel = document.getElementById("scan-result");
  var cameraPanel = document.getElementById("camera-panel");
  var startButton = document.getElementById("camera-start");
  var stopButton = document.getElementById("camera-stop");
  var cameraSelect = document.getElementById("camera-select");
  var message = document.getElementById("camera-message");
  var idle = document.getElementById("reader-idle");
  var busyOverlay = document.getElementById("scan-busy");
  var recent = document.getElementById("recent-scans");
  var nextLink = resultPanel.querySelector("[data-scan-next]");

  var ICONS = { valid: "check-circle-fill", already_used: "exclamation-triangle-fill", invalid: "x-octagon-fill" };
  var CAMERA_KEY = "eventlink.gate.camera";
  var scanner = null;
  var running = false;
  var busy = false;

  function showMessage(text, tone) {
    message.textContent = text;
    message.className = "gate-camera-message" + (tone ? " gate-camera-message-" + tone : "");
    message.hidden = !text;
  }

  function setBusy(state) {
    busy = state;
    busyOverlay.hidden = !state;
    manualSubmit.disabled = state;
    codeInput.readOnly = state;
  }

  function field(name) {
    return resultPanel.querySelector("[data-result='" + name + "']");
  }

  function showRows(name, visible) {
    resultPanel.querySelectorAll("[data-result-row='" + name + "']").forEach(function (row) {
      row.hidden = !visible;
    });
  }

  function renderOutcome(outcome) {
    resultPanel.className = "gate-result gate-result-" + outcome.result;
    field("icon").className = "bi bi-" + (ICONS[outcome.result] || "question-circle");
    field("label").textContent = outcome.label.toUpperCase();
    field("message").textContent = outcome.message;
    field("reason").textContent = outcome.reason || "";
    showRows("reason", Boolean(outcome.reason));
    var ticket = outcome.ticket;
    showRows("ticket", Boolean(ticket));
    showRows("used", Boolean(ticket && ticket.redeemed_at));
    if (ticket) {
      field("code").textContent = ticket.code;
      field("event").textContent = ticket.event;
      field("category").textContent = ticket.category;
      field("holder").textContent = ticket.holder;
      field("redeemed").textContent = ticket.redeemed_at || "";
      field("used-label").textContent = outcome.result === "valid" ? "Entry recorded" : "First scanned";
    }
    field("checked").textContent = outcome.checked_at;
    cameraPanel.hidden = true;
    resultPanel.hidden = false;
    resultPanel.focus({ preventScroll: true });
    resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    if (navigator.vibrate) {
      navigator.vibrate(outcome.result === "valid" ? 150 : [250, 120, 250]);
    }
  }

  function updateStats(stats) {
    Object.keys(stats).forEach(function (key) {
      var cell = root.querySelector("[data-stat='" + key + "']");
      if (cell) cell.textContent = stats[key];
    });
  }

  function addRecent(outcome, code) {
    var list = recent.querySelector("ul.scan-rows");
    if (!list) {
      recent.textContent = "";
      list = document.createElement("ul");
      list.className = "list-rows scan-rows";
      recent.appendChild(list);
    }
    var item = document.createElement("li");
    var main = document.createElement("div");
    main.className = "row-main";
    var strong = document.createElement("strong");
    strong.className = "scan-code";
    strong.textContent = outcome.ticket ? outcome.ticket.code : code.slice(0, 40);
    var meta = document.createElement("span");
    var parts = [outcome.checked_at];
    if (outcome.ticket) parts.push(outcome.ticket.category);
    if (outcome.reason) parts.push(outcome.reason);
    meta.textContent = parts.join(" · ");
    main.appendChild(strong);
    main.appendChild(meta);
    var badge = document.createElement("span");
    badge.className = "status status-" + outcome.result;
    var icon = document.createElement("i");
    icon.className = "bi bi-" + (ICONS[outcome.result] || "circle-fill");
    icon.setAttribute("aria-hidden", "true");
    badge.appendChild(icon);
    badge.appendChild(document.createTextNode(" " + outcome.label));
    item.appendChild(main);
    item.appendChild(badge);
    list.insertBefore(item, list.firstChild);
    while (list.children.length > 5) list.removeChild(list.lastChild);
  }

  function pauseScanner() {
    if (!running || !scanner) return;
    try {
      scanner.pause(true);
    } catch (error) {
      /* Already paused. */
    }
  }

  function resumeScanner() {
    if (!running || !scanner) return;
    try {
      scanner.resume();
    } catch (error) {
      /* Not paused. */
    }
  }

  function verify(code, method) {
    if (busy) return;
    code = (code || "").trim();
    if (!code) {
      codeInput.focus();
      return;
    }
    setBusy(true);
    pauseScanner();
    var body = new FormData();
    body.append("csrf_token", csrfToken);
    body.append("code", code);
    body.append("method", method);
    fetch(verifyUrl, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken, Accept: "application/json", "X-Requested-With": "fetch" }
    })
      .then(function (response) {
        var type = response.headers.get("Content-Type") || "";
        if (!response.ok || type.indexOf("application/json") === -1) {
          var error = new Error("session");
          error.status = response.status;
          throw error;
        }
        return response.json();
      })
      .then(function (data) {
        setBusy(false);
        showMessage("");
        renderOutcome(data.outcome);
        updateStats(data.stats);
        addRecent(data.outcome, code);
        codeInput.value = "";
      })
      .catch(function (error) {
        setBusy(false);
        if (error && error.message === "session") {
          showMessage("This page has expired or your session ended. Reload the page and log in again. The ticket was not checked.", "danger");
        } else {
          showMessage("Could not reach EventLink. Check the Wi-Fi connection and try again. The ticket was not checked.", "danger");
        }
        cameraPanel.hidden = false;
        resumeScanner();
      });
  }

  function scanNext(event) {
    if (event) event.preventDefault();
    resultPanel.hidden = true;
    cameraPanel.hidden = false;
    codeInput.value = "";
    showMessage("");
    if (running) {
      resumeScanner();
      cameraPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      codeInput.focus();
    }
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    event.stopPropagation();
    verify(codeInput.value, "manual");
  });
  nextLink.addEventListener("click", scanNext);

  function cameraError(error) {
    var name = error && error.name ? error.name : String(error || "");
    if (/NotAllowed|Permission/i.test(name) || /permission/i.test(String(error))) {
      return "Camera access was blocked. Allow the camera for this site in the browser settings, then tap Start camera again. You can also type the ticket code below.";
    }
    if (/NotFound|Overconstrained/i.test(name)) {
      return "No usable camera was found on this device. Type the ticket code below instead.";
    }
    if (/NotReadable|TrackStart/i.test(name)) {
      return "The camera is being used by another app. Close the other app and tap Start camera again.";
    }
    return "The camera could not be started. Type the ticket code below instead.";
  }

  function onDecoded(text) {
    if (busy || resultPanel.hidden === false) return;
    verify(text, "camera");
  }

  function startWith(cameraId) {
    if (!scanner) {
      scanner = new Html5Qrcode("qr-reader", {
        verbose: false,
        formatsToSupport: [Html5QrcodeSupportedFormats.QR_CODE]
      });
    }
    var config = { fps: 10, qrbox: function (width, height) {
      var size = Math.floor(Math.min(width, height) * 0.7);
      return { width: Math.max(size, 160), height: Math.max(size, 160) };
    } };
    var source = cameraId ? cameraId : { facingMode: "environment" };
    startButton.disabled = true;
    showMessage("Starting camera…");
    return scanner.start(source, config, onDecoded, function () {}).then(function () {
      running = true;
      idle.hidden = true;
      startButton.hidden = true;
      stopButton.hidden = false;
      showMessage("");
      if (cameraId) {
        try { window.localStorage.setItem(CAMERA_KEY, cameraId); } catch (error) { /* Private mode. */ }
      }
    }).catch(function (error) {
      running = false;
      startButton.disabled = false;
      showMessage(cameraError(error), "danger");
    });
  }

  function stopCamera() {
    if (!scanner || !running) return Promise.resolve();
    return scanner.stop().then(function () {
      running = false;
      idle.hidden = false;
      startButton.hidden = false;
      startButton.disabled = false;
      stopButton.hidden = true;
    }).catch(function () {
      running = false;
    });
  }

  function preferredCamera(cameras) {
    var saved = null;
    try { saved = window.localStorage.getItem(CAMERA_KEY); } catch (error) { /* Private mode. */ }
    var match = cameras.filter(function (camera) { return camera.id === saved; })[0];
    if (match) return match.id;
    var rear = cameras.filter(function (camera) { return /back|rear|environment/i.test(camera.label || ""); });
    return (rear[rear.length - 1] || cameras[cameras.length - 1]).id;
  }

  function fillCameraList(cameras, selected) {
    cameraSelect.textContent = "";
    cameras.forEach(function (camera, index) {
      var option = document.createElement("option");
      option.value = camera.id;
      option.textContent = camera.label || "Camera " + (index + 1);
      option.selected = camera.id === selected;
      cameraSelect.appendChild(option);
    });
    cameraSelect.hidden = cameras.length < 2;
  }

  startButton.addEventListener("click", function () {
    startButton.disabled = true;
    showMessage("Waiting for camera permission…");
    Html5Qrcode.getCameras().then(function (cameras) {
      if (!cameras || !cameras.length) {
        startButton.disabled = false;
        showMessage("No camera was found on this device. Type the ticket code below instead.", "danger");
        return;
      }
      var selected = preferredCamera(cameras);
      fillCameraList(cameras, selected);
      startWith(selected);
    }).catch(function (error) {
      startButton.disabled = false;
      showMessage(cameraError(error), "danger");
    });
  });

  stopButton.addEventListener("click", stopCamera);

  cameraSelect.addEventListener("change", function () {
    var chosen = cameraSelect.value;
    stopCamera().then(function () { startWith(chosen); });
  });

  window.addEventListener("pagehide", stopCamera);

  if (!window.isSecureContext) {
    showMessage("The camera only works when EventLink is opened over HTTPS (or on the computer running it). Ask the organiser for the secure https:// address, or type the ticket code below.", "warning");
  } else if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showMessage("This browser cannot use the camera. Try Chrome on Android or Safari on iPhone, or type the ticket code below.", "warning");
  } else if (typeof Html5Qrcode === "undefined") {
    showMessage("The scanner did not load. Reload the page, or type the ticket code below.", "warning");
  } else {
    startButton.disabled = false;
  }

  if (!resultPanel.hidden) {
    resultPanel.focus({ preventScroll: true });
  }
})();
