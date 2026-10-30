document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-print]");
  if (!button) {
    return;
  }
  window.print();
});
