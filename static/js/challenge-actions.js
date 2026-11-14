for (const trigger of document.querySelectorAll("[data-challenge-dialog]")) {
    const dialog = document.getElementById(trigger.dataset.challengeDialog);
    if (!dialog || typeof dialog.showModal !== "function") continue;

    const cancel = dialog.querySelector("[data-cancel-dialog]");
    const fallback = trigger.parentElement.querySelector("[data-registration-fallback]");
    if (fallback) fallback.hidden = true;
    trigger.hidden = false;
    trigger.addEventListener("click", (event) => {
        event.preventDefault();
        dialog.showModal();
        cancel.focus();
    });
    cancel.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => trigger.focus());
}
