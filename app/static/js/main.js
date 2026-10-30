document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  const message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
    return;
  }
  if (form.dataset.submitted === "1") {
    event.preventDefault();
    return;
  }
  form.dataset.submitted = "1";
});

window.addEventListener("pageshow", function (event) {
  if (event.persisted) {
    document.querySelectorAll("form[data-submitted]").forEach(function (form) {
      delete form.dataset.submitted;
    });
  }
});

document.addEventListener("click", function (event) {
  const toggle = event.target.closest("[data-password-toggle]");
  if (!toggle) {
    return;
  }
  const input = toggle.parentElement.querySelector("input");
  if (!input) {
    return;
  }
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  toggle.setAttribute("aria-pressed", show ? "true" : "false");
  toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  const icon = toggle.querySelector("i");
  if (icon) {
    icon.className = show ? "bi bi-eye-slash" : "bi bi-eye";
  }
});

document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-action]");
  if (!button || !button.form) {
    return;
  }
  let field = button.form.querySelector("input[name='action']");
  if (!field) {
    field = document.createElement("input");
    field.type = "hidden";
    field.name = "action";
    button.form.appendChild(field);
  }
  field.value = button.dataset.action;
});

(function () {
  function formatSize(bytes) {
    return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  function canEditFiles() {
    try {
      return typeof DataTransfer === "function" && new DataTransfer().items !== undefined;
    } catch (error) {
      return false;
    }
  }

  function iconButton(icon, label, attribute) {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute(attribute, "");
    button.setAttribute("aria-label", label);
    button.title = label;
    button.innerHTML = '<i class="bi bi-' + icon + '" aria-hidden="true"></i>';
    return button;
  }

  function setupPicker(box) {
    const input = box.querySelector(".upload-input");
    const previews = box.querySelector(".upload-previews");
    const errorBox = box.querySelector(".upload-error");
    if (!input || !previews) {
      return;
    }
    const max = parseInt(box.dataset.maxFiles || "1", 10);
    const maxBytes = parseInt(box.dataset.maxBytes || "0", 10);
    const types = (box.dataset.types || "").split(",").filter(Boolean);
    const editable = canEditFiles();
    let files = [];
    let urls = [];

    function render() {
      urls.forEach(function (url) { URL.revokeObjectURL(url); });
      urls = [];
      previews.innerHTML = "";
      files.forEach(function (file, index) {
        const figure = document.createElement("figure");
        figure.className = "upload-thumb";
        if (file.type.indexOf("image/") === 0) {
          const image = document.createElement("img");
          const url = URL.createObjectURL(file);
          urls.push(url);
          image.src = url;
          image.alt = "Preview of " + file.name;
          figure.appendChild(image);
        } else {
          const icon = document.createElement("div");
          icon.className = "upload-file";
          icon.innerHTML = '<i class="bi bi-file-earmark-pdf" aria-hidden="true"></i>';
          figure.appendChild(icon);
        }
        const caption = document.createElement("figcaption");
        caption.textContent = file.name + " · " + formatSize(file.size);
        figure.appendChild(caption);
        const actions = document.createElement("div");
        actions.className = "upload-thumb-actions";
        if (max === 1) {
          const replace = iconButton("arrow-repeat", "Replace " + file.name, "data-upload-replace");
          replace.addEventListener("click", function () { input.click(); });
          actions.appendChild(replace);
        }
        const remove = iconButton("x-lg", "Remove " + file.name, "data-upload-remove");
        remove.addEventListener("click", function () {
          if (editable) {
            files.splice(index, 1);
          } else {
            files = [];
            input.value = "";
          }
          errorBox.textContent = "";
          sync();
        });
        actions.appendChild(remove);
        figure.appendChild(actions);
        previews.appendChild(figure);
      });
    }

    function sync() {
      if (editable) {
        const transfer = new DataTransfer();
        files.forEach(function (file) { transfer.items.add(file); });
        input.files = transfer.files;
      }
      render();
    }

    input.addEventListener("change", function () {
      const errors = [];
      const valid = Array.from(input.files).filter(function (file) {
        if (types.length && types.indexOf(file.type) === -1) {
          errors.push(file.name + " is not a supported file type.");
          return false;
        }
        if (maxBytes && file.size > maxBytes) {
          errors.push(file.name + " is larger than " + formatSize(maxBytes) + ".");
          return false;
        }
        return true;
      });
      if (max === 1) {
        files = valid.length ? valid.slice(0, 1) : (editable ? files : []);
      } else {
        const combined = (editable ? files : []).concat(valid);
        if (combined.length > max) {
          errors.push("You can upload up to " + max + " images; only the first " + max + " were kept.");
        }
        files = combined.slice(0, max);
      }
      errorBox.textContent = errors.join(" ");
      sync();
    });
  }

  function applyImageFallback(image) {
    if (image.dataset.fallback === "remove") {
      const item = image.closest("[data-gallery-item]");
      if (item) {
        const gallery = item.closest(".portfolio-gallery");
        const grid = item.parentElement;
        item.remove();
        if (gallery && grid && !grid.querySelector("[data-gallery-item]")) {
          grid.remove();
          const empty = gallery.querySelector("[data-gallery-empty]");
          if (empty) {
            empty.hidden = false;
          }
        }
        return;
      }
    }
    const holder = image.closest(".has-image");
    if (holder) {
      holder.classList.remove("has-image");
    }
    const zoomLink = image.closest("a[data-lightbox]");
    image.remove();
    if (zoomLink) {
      zoomLink.replaceWith.apply(zoomLink, Array.from(zoomLink.childNodes));
    }
  }

  document.addEventListener("error", function (event) {
    const image = event.target;
    if (image instanceof HTMLImageElement && image.hasAttribute("data-fallback")) {
      applyImageFallback(image);
    }
  }, true);

  function setupLightbox() {
    const modalElement = document.getElementById("imageLightbox");
    if (!modalElement || !window.bootstrap) {
      return;
    }
    const modal = bootstrap.Modal.getOrCreateInstance(modalElement);
    const image = modalElement.querySelector(".lightbox-image");
    const caption = modalElement.querySelector(".lightbox-caption");
    const counter = modalElement.querySelector(".lightbox-counter");
    const navButtons = modalElement.querySelectorAll("[data-lightbox-step]");
    let items = [];
    let current = 0;

    function show(index) {
      current = (index + items.length) % items.length;
      const link = items[current];
      image.src = link.getAttribute("href");
      image.alt = link.dataset.caption || "";
      caption.textContent = link.dataset.caption || "";
      counter.textContent = items.length > 1 ? (current + 1) + " of " + items.length : "";
      navButtons.forEach(function (button) { button.hidden = items.length < 2; });
    }

    document.addEventListener("click", function (event) {
      const link = event.target.closest("a[data-lightbox]");
      if (!link) {
        return;
      }
      event.preventDefault();
      items = Array.from(document.querySelectorAll('a[data-lightbox="' + CSS.escape(link.dataset.lightbox) + '"]'));
      show(items.indexOf(link));
      modal.show();
    });
    navButtons.forEach(function (button) {
      button.addEventListener("click", function () { show(current + parseInt(button.dataset.lightboxStep, 10)); });
    });
    modalElement.addEventListener("keydown", function (event) {
      if (items.length > 1 && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
        show(current + (event.key === "ArrowRight" ? 1 : -1));
      }
    });
    modalElement.addEventListener("hidden.bs.modal", function () { image.removeAttribute("src"); });
  }

  function init() {
    document.querySelectorAll("[data-image-picker]").forEach(setupPicker);
    document.querySelectorAll("img[data-fallback]").forEach(function (image) {
      if (image.complete && image.naturalWidth === 0) {
        applyImageFallback(image);
      }
    });
    setupLightbox();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
