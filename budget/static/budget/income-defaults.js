(() => {
  const data = document.querySelector('#income-defaults');
  const source = document.querySelector('#id_source');
  const amount = document.querySelector('#id_amount');
  const paycheck = document.querySelector('#id_is_paycheck');
  const irregular = document.querySelector('#id_is_irregular');
  const script = document.currentScript;
  if (!data || !source || !amount) return;
  let profiles;
  try {
    profiles = JSON.parse(data.textContent);
  } catch (error) {
    return;
  }
  const inputs = [...document.querySelectorAll('input[name^="allocation_"]')];
  // Same amounts the server accepts: 1234.56, 1,234.56 or $1,234.56.
  const cents = (text) => {
    const match = /^\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*$/.exec(text || '');
    if (!match) return null;
    return Number(match[1].replace(/,/g, '')) * 100 + Number((match[2] || '').padEnd(2, '0') || 0);
  };
  const format = (value) => `${Math.floor(value / 100)}.${String(value % 100).padStart(2, '0')}`;

  // Never overwrite numbers the person typed (or a re-rendered form's values).
  let allocationsTouched = inputs.some((input) => (cents(input.value) || 0) > 0);
  const bound = Boolean(script && script.dataset.bound);
  const touched = { paycheck: bound, irregular: bound };
  inputs.forEach((input) => input.addEventListener('input', () => { allocationsTouched = true; }));
  if (paycheck) paycheck.addEventListener('change', () => { touched.paycheck = true; });
  if (irregular) irregular.addEventListener('change', () => { touched.irregular = true; });

  const profile = () => profiles[source.value.trim().toLowerCase()];
  const applyFlags = () => {
    const match = profile();
    if (!match) return;
    if (paycheck && !touched.paycheck) paycheck.checked = match.is_paycheck;
    if (irregular && !touched.irregular) irregular.checked = match.is_irregular;
  };
  const applySplit = () => {
    const match = profile();
    const total = cents(amount.value);
    if (!match || !match.remainder_account || allocationsTouched || !total) return;
    const fixed = Object.entries(match.fixed).map(([id, value]) => [id, cents(value)]);
    const left = total - fixed.reduce((sum, [, value]) => sum + value, 0);
    const split = left < 0 ? { [match.remainder_account]: total } : Object.fromEntries(fixed);
    if (left >= 0) split[match.remainder_account] = left;
    inputs.forEach((input) => {
      const value = split[input.name.replace('allocation_', '')] || 0;
      input.value = value > 0 ? format(value) : '';
    });
  };
  const apply = () => { applyFlags(); applySplit(); };
  source.addEventListener('input', apply);
  source.addEventListener('change', apply);
  amount.addEventListener('input', applySplit);
})();
