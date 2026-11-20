(() => {
  document.querySelectorAll('[data-bill-dropdown]').forEach((box) => {
    const summary = box.querySelector('[data-summary]');
    const update = () => {
      const count = box.querySelectorAll('input[type="checkbox"]:checked').length;
      summary.textContent = `Choose bills (${count} chosen)`;
    };
    box.addEventListener('change', update);
    update();
  });
})();
