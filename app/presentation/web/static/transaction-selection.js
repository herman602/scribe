(() => {
  "use strict";

  const formFor = (control) => {
    const formId =
      control.dataset.selectionToggle || control.dataset.bulkConfirm;
    return formId ? document.getElementById(formId) : control.closest("form");
  };

  const updateForm = (form) => {
    const active = form.classList.contains("selection-active");
    const count = form.querySelectorAll(
      ".selection-checkbox:checked",
    ).length;
    const controls = document.querySelectorAll(
      `[data-bulk-confirm="${form.id}"]`,
    );
    controls.forEach((control) => {
      control.hidden = !active || count === 0;
      control.textContent = `確認 ${count} 筆交易`;
    });
  };

  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-selection-toggle]");
    if (!toggle) {
      return;
    }
    const form = formFor(toggle);
    if (!form) {
      return;
    }
    const active = !form.classList.contains("selection-active");
    form.classList.toggle("selection-active", active);
    toggle.setAttribute("aria-pressed", String(active));
    toggle.textContent = active ? "取消選取" : "選取交易";
    if (!active) {
      form.querySelectorAll(".selection-checkbox:checked").forEach((input) => {
        input.checked = false;
      });
    }
    updateForm(form);
  });

  document.addEventListener("change", (event) => {
    if (!event.target.matches(".selection-checkbox")) {
      return;
    }
    const form = event.target.closest("[data-transaction-selection]");
    if (form) {
      updateForm(form);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-transaction-selection]");
    if (
      form &&
      form.querySelectorAll(".selection-checkbox:checked").length === 0
    ) {
      event.preventDefault();
    }
  });
})();
