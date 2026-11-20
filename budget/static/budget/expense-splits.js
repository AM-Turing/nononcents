(() => {
  const toggle = document.querySelector('#id_split_transaction');
  const fields = document.querySelector('[data-split-fields]');
  const primary = document.querySelector('.primary-category-field');
  if (!toggle || !fields || !primary) return;

  const rows = fields.querySelector('[data-split-rows]');
  const total = fields.querySelector('#id_splits-TOTAL_FORMS');
  const template = fields.querySelector('[data-empty-split]');
  const sync = () => {
    fields.hidden = !toggle.checked;
    primary.hidden = toggle.checked;
    primary.querySelector('select').required = !toggle.checked;
  };
  toggle.addEventListener('change', sync);
  sync();

  // "Left to assign", worked in whole cents so nothing is rounded.
  const left = fields.querySelector('[data-split-left]');
  const totalField = document.querySelector('#id_amount');
  const cents = (value) => {
    const text = String(value || '').replace(/[$,\s]/g, '');
    if (!/^\d*\.?\d{0,2}$/.test(text) || text === '' || text === '.') return null;
    const [whole, fraction = ''] = text.split('.');
    return Number(whole || '0') * 100 + Number((fraction + '00').slice(0, 2));
  };
  const dollars = (value) => {
    const abs = Math.abs(value);
    return `$${Math.floor(abs / 100).toLocaleString('en-US')}.${String(abs % 100).padStart(2, '0')}`;
  };
  const remaining = () => {
    const amount = cents(totalField && totalField.value);
    if (amount === null) {
      left.textContent = 'Enter the total amount above to see what is left to assign.';
    } else {
      const assigned = [...rows.querySelectorAll('input[name$="-amount"]')]
        .reduce((sum, input) => sum + (cents(input.value) || 0), 0);
      const diff = amount - assigned;
      left.textContent = diff === 0 ? 'Everything is assigned.'
        : diff > 0 ? `Left to assign: ${dollars(diff)}` : `Over by ${dollars(diff)}`;
    }
    const few = rows.children.length <= 2;
    fields.querySelectorAll('[data-remove-split]').forEach((button) => {
      button.disabled = few;
      button.title = few ? 'A split needs at least two categories. Untick Split to go back to one.' : '';
    });
  };
  rows.addEventListener('input', remaining);
  if (totalField) totalField.addEventListener('input', remaining);
  remaining();

  fields.querySelector('[data-add-split]').addEventListener('click', () => {
    if (Number(total.value) >= 10) return;
    const index = total.value;
    rows.insertAdjacentHTML('beforeend', template.innerHTML.replaceAll('__prefix__', index));
    total.value = Number(index) + 1;
    remaining();
    const added = rows.lastElementChild.querySelector('select, input');
    if (added) added.focus();
  });
  fields.addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-split]');
    if (!button || rows.children.length <= 2) return;
    button.closest('.split-row').remove();
    [...rows.children].forEach((row, index) => {
      row.querySelectorAll('select, input, label').forEach((element) => {
        if (element.name) element.name = element.name.replace(/splits-\d+-/, `splits-${index}-`);
        if (element.id) element.id = element.id.replace(/id_splits-\d+-/, `id_splits-${index}-`);
        if (element.htmlFor) element.htmlFor = element.htmlFor.replace(/id_splits-\d+-/, `id_splits-${index}-`);
      });
    });
    total.value = rows.children.length;
    remaining();
  });
})();
