(function () {
  const timer = document.querySelector("[data-countdown]");
  if (timer) {
    const expires = Date.parse(timer.getAttribute("data-countdown"));
    const text = timer.querySelector("[data-countdown-text]");
    const tick = function () {
      const left = Math.max(0, Math.floor((expires - Date.now()) / 1000));
      if (left === 0) {
        timer.classList.add("is-expired");
        text.textContent = "0:00";
        document.querySelectorAll("[data-checkout-form] button").forEach(function (button) {
          button.disabled = true;
        });
        timer.querySelector("small").textContent = "The reservation has expired. Refresh the page or return to the event to try again.";
        return false;
      }
      const minutes = Math.floor(left / 60);
      const seconds = String(left % 60).padStart(2, "0");
      text.textContent = minutes + ":" + seconds;
      timer.classList.toggle("is-urgent", left <= 300);
      return true;
    };
    if (!Number.isNaN(expires) && tick()) {
      const handle = window.setInterval(function () {
        if (!tick()) {
          window.clearInterval(handle);
        }
      }, 1000);
    }
  }

  const form = document.querySelector("[data-checkout-form]");
  const phone = form && form.querySelector("[data-phone-input]");
  const cardName = form && form.querySelector("[data-card-input]");
  if (phone && cardName) {
    const title = form.querySelector("[data-details-title]");
    const label = form.querySelector("[data-phone-label]");
    const hint = form.querySelector("[data-phone-hint]");
    const selectedMethod = function () {
      return form.querySelector('input[name="method"]:checked');
    };
    const localNumber = function (value) {
      let digits = value.replace(/[\s\-()]/g, "");
      if (digits.startsWith("00")) {
        digits = "+" + digits.slice(2);
      }
      if (digits.startsWith("+260")) {
        digits = "0" + digits.slice(4);
      } else if (digits.startsWith("260") && digits.length === 12) {
        digits = "0" + digits.slice(3);
      }
      return digits;
    };
    const showError = function (input, message) {
      const error = input.closest(".pay-field").querySelector("[data-field-error]");
      error.textContent = message;
      input.classList.toggle("is-invalid", Boolean(message));
    };
    const phoneProblem = function () {
      const method = selectedMethod();
      const prefixes = method.dataset.prefixes.split(",");
      const number = localNumber(phone.value.trim());
      if (!/^0[79]\d{8}$/.test(number)) {
        return "Enter the " + method.dataset.network + " mobile money number to pay from, such as " + prefixes[0] + "1234567.";
      }
      if (!prefixes.some(function (prefix) { return number.startsWith(prefix); })) {
        return "That number is not on " + method.dataset.network + ". " + method.dataset.network + " numbers start with " + prefixes.join(" or ") + ".";
      }
      return "";
    };
    const cardProblem = function () {
      return /^[A-Za-z][A-Za-z' .\-]{1,119}$/.test(cardName.value.trim().replace(/\s+/g, " "))
        ? ""
        : "Enter the name exactly as it appears on the card.";
    };
    const validate = function (input, report) {
      const message = input === phone ? phoneProblem() : cardProblem();
      input.setCustomValidity(message);
      if (report || !message) {
        showError(input, message);
      }
    };
    const sync = function (keepError) {
      const method = selectedMethod();
      if (!method) {
        return;
      }
      const kind = method.dataset.kind;
      form.querySelectorAll("[data-pay-field]").forEach(function (field) {
        const active = field.dataset.payField === kind;
        field.hidden = !active;
        field.querySelectorAll("input").forEach(function (input) {
          input.disabled = !active;
        });
      });
      if (kind === "mobile") {
        const prefixes = method.dataset.prefixes.split(",");
        title.textContent = "Enter your " + method.dataset.wallet + " number";
        label.textContent = method.dataset.wallet + " number";
        hint.textContent = method.dataset.network + " numbers start with " + prefixes.join(" or ") + ". Use the number registered to your wallet.";
        phone.placeholder = prefixes[0] + " 123 4567";
        validate(phone, phone.value.trim() !== "" && !keepError);
      } else {
        title.textContent = "Enter the name on your card";
        validate(cardName, false);
      }
    };
    form.querySelectorAll('input[name="method"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        sync(false);
      });
    });
    [phone, cardName].forEach(function (input) {
      input.addEventListener("input", function () {
        validate(input, input.classList.contains("is-invalid"));
      });
      input.addEventListener("blur", function () {
        if (input.value.trim() !== "") {
          validate(input, true);
        }
      });
      input.addEventListener("invalid", function (event) {
        event.preventDefault();
        validate(input, true);
        input.focus();
      });
    });
    sync(true);
  }

  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-confirm-click]");
    if (button && !window.confirm(button.getAttribute("data-confirm-click"))) {
      event.preventDefault();
    }
  });

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form.matches("[data-checkout-form]") || event.defaultPrevented) {
      return;
    }
    const submitter = event.submitter;
    if (submitter && submitter.classList.contains("checkout-pay")) {
      submitter.classList.add("is-loading");
      submitter.setAttribute("aria-busy", "true");
      submitter.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span> Processing payment…';
    }
  });
})();
