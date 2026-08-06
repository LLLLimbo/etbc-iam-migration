"use strict";

const utcTimestamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

document.querySelectorAll('input[name="snapshotAt"]').forEach((input) => {
  if (!input.value) input.value = utcTimestamp();
});

document.querySelectorAll("[data-generate-batch]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.closest("label")?.querySelector('input[name="batchId"]');
    if (!input) return;
    const date = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    const entropy = globalThis.crypto.randomUUID().split("-")[0];
    input.value = `etbc-${date}-${entropy}`;
    input.focus();
  });
});

document.querySelectorAll("[data-operation-form]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!form.reportValidity()) {
      event.preventDefault();
      return;
    }
    const button = event.submitter;
    if (!(button instanceof HTMLButtonElement)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = button.dataset.busyLabel || "正在执行…";
  });
});
