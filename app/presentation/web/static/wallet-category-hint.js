/* Fill in the budget category from the wallet that was chosen.
 *
 * Someone who keeps one envelope per kind of spending names the wallet and
 * the category the same, so asking twice asks the same question twice. The
 * wallet is the answer that matters -- it decides which money is committed --
 * and the category is carried on it as data-category.
 *
 * It is only ever a suggestion: a category moves no money, so it stays
 * editable, and once it has been set by hand this stops overwriting it.
 */
(() => {
  const WALLET = 'select[name="funding_wallet_id"]';
  const CATEGORY = 'select[name="budget_category_id"]';

  const categoryFor = (wallet) => {
    // Each allocation row is its own fieldset; the dedicated-wallet form has
    // no rows, so the form itself is the scope.
    const scope = wallet.closest("fieldset") || wallet.closest("form");
    return scope ? scope.querySelector(CATEGORY) : null;
  };

  /* Folded away and required, an empty category cannot be submitted and
   * nothing on screen says why, so that one field is opened back up. Where
   * the category is optional, empty means 未分類 -- a real answer, not a
   * missing one -- so it stays folded and out of the way. */
  const revealIfUnanswerable = (category) => {
    const fold = category.closest("details");
    if (fold && category.required && !category.value) fold.open = true;
  };

  const apply = (wallet) => {
    const category = categoryFor(wallet);
    if (!category || category.dataset.chosenByHand === "1") return;
    const hint = wallet.selectedOptions[0]?.dataset.category;
    if (hint && category.querySelector(`option[value="${hint}"]`)) {
      category.value = hint;
    }
    revealIfUnanswerable(category);
  };

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLSelectElement)) return;
    if (target.matches(CATEGORY)) {
      target.dataset.chosenByHand = "1";
      return;
    }
    if (target.matches(WALLET)) apply(target);
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(WALLET).forEach(apply);
  });
})();
