// "Print / PDF" buttons: <button data-print>.
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-print]")) window.print();
});
