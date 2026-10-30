document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-add-ticket]");
  if (!button) {
    return;
  }
  const list = document.getElementById("ticket-rows");
  if (!list) {
    return;
  }
  const row = list.querySelector(".ticket-row");
  if (!row) {
    return;
  }
  const copy = row.cloneNode(true);
  copy.querySelectorAll("input").forEach(function (input) {
    if (input.name === "ticket_max") {
      input.value = "10";
    } else {
      input.value = "";
    }
  });
  list.appendChild(copy);
});

(function () {
  const list = document.querySelector("[data-artist-list]");
  const template = document.getElementById("artist-template");
  if (!list || !template) {
    return;
  }
  const MAX_ARTISTS = 20;
  const MAX_BYTES = 2 * 1024 * 1024;
  const TYPES = ["image/jpeg", "image/png", "image/webp"];
  const counter = document.querySelector("[data-artist-count]");
  const empty = document.querySelector("[data-artist-empty]");
  const addButton = document.querySelector("[data-add-artist]");

  function refresh() {
    const total = list.querySelectorAll("[data-artist-card]").length;
    if (counter) {
      counter.textContent = total + " added";
    }
    if (empty) {
      empty.hidden = total > 0;
    }
    if (addButton) {
      addButton.disabled = total >= MAX_ARTISTS;
    }
  }

  function newKey() {
    let key;
    do {
      key = "n" + Math.random().toString(36).slice(2, 10);
    } while (list.querySelector('[data-artist-key][value="' + key + '"]'));
    return key;
  }

  function initials(name) {
    return name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map(function (word) {
      return word.charAt(0);
    }).join("").toUpperCase();
  }

  addButton.addEventListener("click", function () {
    if (list.querySelectorAll("[data-artist-card]").length >= MAX_ARTISTS) {
      return;
    }
    const key = newKey();
    const html = template.innerHTML.split("__KEY__").join(key);
    const holder = document.createElement("div");
    holder.innerHTML = html.trim();
    const card = holder.firstElementChild;
    list.appendChild(card);
    refresh();
    const name = card.querySelector("[data-artist-name]");
    if (name) {
      name.focus();
    }
  });

  list.addEventListener("click", function (event) {
    const button = event.target.closest("[data-remove-artist]");
    if (!button) {
      return;
    }
    const card = button.closest("[data-artist-card]");
    const name = card.querySelector("[data-artist-name]");
    const label = name && name.value.trim() ? name.value.trim() : "this artist";
    if (!window.confirm("Remove " + label + " from this event? The change is saved when you save or submit the event.")) {
      return;
    }
    const preview = card.querySelector("[data-artist-preview] img[data-object-url]");
    if (preview) {
      URL.revokeObjectURL(preview.src);
    }
    card.remove();
    refresh();
  });

  list.addEventListener("input", function (event) {
    if (!event.target.matches("[data-artist-name]")) {
      return;
    }
    const card = event.target.closest("[data-artist-card]");
    const holder = card.querySelector("[data-artist-preview] .artist-initials");
    if (holder) {
      holder.textContent = initials(event.target.value);
    }
  });

  list.addEventListener("change", function (event) {
    const input = event.target;
    if (!input.matches("[data-artist-photo]")) {
      return;
    }
    const card = input.closest("[data-artist-card]");
    const errorBox = card.querySelector("[data-artist-error]");
    const preview = card.querySelector("[data-artist-preview]");
    const text = card.querySelector("[data-artist-photo-text]");
    errorBox.textContent = "";
    const file = input.files && input.files[0];
    if (!file) {
      return;
    }
    if (TYPES.indexOf(file.type) === -1) {
      errorBox.textContent = file.name + " is not a JPEG, PNG or WebP image.";
      input.value = "";
      return;
    }
    if (file.size > MAX_BYTES) {
      errorBox.textContent = file.name + " is larger than 2 MB.";
      input.value = "";
      return;
    }
    let image = preview.querySelector("img");
    if (image && image.dataset.objectUrl) {
      URL.revokeObjectURL(image.src);
    }
    if (!image) {
      image = document.createElement("img");
      image.alt = "";
      preview.insertBefore(image, preview.firstChild);
    }
    image.src = URL.createObjectURL(file);
    image.dataset.objectUrl = "1";
    preview.classList.add("has-image");
    if (text) {
      text.textContent = "Change photo";
    }
  });

  refresh();
})();

document.addEventListener("change", function (event) {
  const selected = event.target.closest("input[name='ticket_type_id']");
  if (!selected) {
    return;
  }
  const quantity = document.getElementById("quantity");
  if (!quantity) {
    return;
  }
  quantity.max = selected.dataset.max || "10";
  if (Number(quantity.value) > Number(quantity.max)) {
    quantity.value = quantity.max;
  }
});
