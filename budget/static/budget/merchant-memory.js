(() => {
  const data = document.querySelector('#merchant-map');
  const merchant = document.querySelector('#id_merchant');
  const category = document.querySelector('#id_category');
  const account = document.querySelector('#id_account');
  const script = document.currentScript;
  if (!data || !merchant || !category || !account) return;
  let known;
  try {
    known = JSON.parse(data.textContent);
  } catch (error) {
    return;
  }

  // A re-rendered (invalid) form keeps what the user chose; only fresh forms auto-fill.
  const bound = Boolean(script && script.dataset.bound);
  const touched = { category: bound, account: bound };
  category.addEventListener('change', () => { touched.category = true; });
  account.addEventListener('change', () => { touched.account = true; });

  const choose = (select, value) => {
    const option = [...select.options].find((item) => item.value === String(value));
    if (!option) return;
    select.value = option.value;
  };
  const fill = () => {
    const match = known[merchant.value.trim().toLowerCase()];
    if (!match) return;
    if (!touched.account) choose(account, match.account);
    if (!touched.category) choose(category, match.category);
  };
  merchant.addEventListener('input', fill);
  merchant.addEventListener('change', fill);
})();
