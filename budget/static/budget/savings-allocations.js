document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-savings-allocation]").forEach((fieldset) => {
    const form = fieldset.closest("form");
    const source = form?.querySelector("#id_account, #id_source");
    const multiple = fieldset.querySelector("[data-savings-multiple]");
    const additionalRows = [...fieldset.querySelectorAll(".savings-additional-row")];
    const destinationGoal = form?.querySelector("#id_destination_goal");
    const destinationAccount = form?.querySelector("#id_destination_account");

    const rowHasValue = (row) =>
      [...row.querySelectorAll("select, input[inputmode='decimal']")].some((input) => input.value);
    if (additionalRows.some(rowHasValue)) multiple.checked = true;

    const updateRows = () => {
      additionalRows.forEach((row) => {
        row.hidden = !multiple.checked;
      });
    };
    const updateFieldset = () => {
      if (!source) {
        fieldset.hidden = false;
        return;
      }
      const selected = source.options[source.selectedIndex];
      fieldset.hidden = !selected || selected.textContent.trim().toLowerCase() !== "savings";
    };
    const updateFulfillment = () => {
      const reallocating = destinationGoal?.value && !destinationAccount?.value;
      fieldset.querySelectorAll(".savings-fulfillment-control").forEach((control) => {
        control.hidden = Boolean(reallocating);
      });
    };

    multiple.addEventListener("change", updateRows);
    source?.addEventListener("change", updateFieldset);
    destinationGoal?.addEventListener("change", updateFulfillment);
    destinationAccount?.addEventListener("change", updateFulfillment);
    updateRows();
    updateFieldset();
    updateFulfillment();
  });
});
