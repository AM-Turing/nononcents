// Remember the chosen appearance in this browser when a Light / Dark / System button is used.
document.addEventListener("click", (event) => {
  const button = event.target.closest("button[name='appearance']");
  if (!button) return;
  try {
    localStorage.setItem("appearance", button.value);
  } catch (e) {}
});
