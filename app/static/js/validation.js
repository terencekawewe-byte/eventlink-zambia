const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function applyCustomRules(form) {
  form.querySelectorAll("input[type='email']").forEach(function (email) {
    const value = email.value.trim();
    email.setCustomValidity(value && !EMAIL_PATTERN.test(value) ? "Enter a valid email address, such as name@example.com." : "");
  });
  const password = form.querySelector("[data-password]");
  if (password) {
    const value = password.value;
    let message = "";
    if (value && (value.length < 8 || value.length > 128)) {
      message = "Password must be 8 to 128 characters.";
    } else if (value && !/[A-Z]/.test(value)) {
      message = "Password must include an uppercase letter.";
    } else if (value && !/[a-z]/.test(value)) {
      message = "Password must include a lowercase letter.";
    } else if (value && !/\d/.test(value)) {
      message = "Password must include a number.";
    }
    password.setCustomValidity(message);
  }
  const confirm = form.querySelector("[data-confirm-password]");
  if (confirm) {
    const mismatch = password && confirm.value && password.value !== confirm.value;
    confirm.setCustomValidity(mismatch ? "Passwords do not match." : "");
  }
  const phone = form.querySelector("[data-phone]");
  if (phone) {
    const value = phone.value.replace(/[\s()-]/g, "");
    const valid = /^(\+260|0)[79]\d{8}$/.test(value) || /^260[79]\d{8}$/.test(value);
    phone.setCustomValidity(valid || value === "" ? "" : "Enter a valid Zambian phone number, such as +260971234567 or 0971234567.");
  }
}

function messageFor(field) {
  const validity = field.validity;
  if (validity.customError) {
    return field.validationMessage;
  }
  if (validity.valueMissing) {
    return field.type === "checkbox" ? "Please tick this box to continue." : "Please fill in this field.";
  }
  if (validity.typeMismatch && field.type === "email") {
    return "Enter a valid email address.";
  }
  if (validity.tooShort) {
    return "Use at least " + field.minLength + " characters.";
  }
  return field.validationMessage;
}

function updateFeedback(field) {
  const form = field.form;
  if (!form || !field.name) {
    return;
  }
  let box = form.querySelector('[data-feedback-for="' + CSS.escape(field.name) + '"]');
  if (field.checkValidity()) {
    field.removeAttribute("aria-invalid");
    if (box) {
      box.remove();
    }
    return;
  }
  if (!box) {
    box = document.createElement("div");
    box.className = "invalid-feedback d-block";
    box.dataset.feedbackFor = field.name;
    const check = field.closest(".form-check");
    if (check) {
      check.appendChild(box);
    } else {
      (field.closest(".password-field") || field).insertAdjacentElement("afterend", box);
    }
  }
  box.textContent = messageFor(field);
  field.setAttribute("aria-invalid", "true");
}

function checkedFields(form) {
  return Array.from(form.elements).filter(function (field) {
    return field.willValidate;
  });
}

document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || !form.classList.contains("needs-check")) {
    return;
  }
  applyCustomRules(form);
  const fields = checkedFields(form);
  fields.forEach(updateFeedback);
  form.classList.add("was-validated");
  if (!form.checkValidity()) {
    event.preventDefault();
    event.stopPropagation();
    form.dataset.submitted = "0";
    const first = fields.find(function (field) {
      return !field.checkValidity();
    });
    if (first) {
      first.scrollIntoView({ behavior: "smooth", block: "center" });
      first.focus({ preventScroll: true });
    }
  }
}, true);

function revalidate(event) {
  const field = event.target;
  const form = field.form;
  if (!form || !form.classList.contains("needs-check") || !form.classList.contains("was-validated")) {
    return;
  }
  applyCustomRules(form);
  checkedFields(form).forEach(updateFeedback);
}

document.addEventListener("input", revalidate);
document.addEventListener("change", revalidate);
