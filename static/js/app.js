(() => {
  const root = document.documentElement;
  const stored = localStorage.getItem("reading-theme");
  const preferred = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  const applyTheme = (theme) => {
    root.dataset.theme = theme;
    localStorage.setItem("reading-theme", theme);
    const icon = document.querySelector("[data-theme-icon]");
    if (icon) icon.textContent = theme === "light" ? "☀" : "☾";
  };
  applyTheme(stored || preferred);
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => applyTheme(root.dataset.theme === "light" ? "dark" : "light"));

  document.querySelector("[data-back-button]")?.addEventListener("click", () => {
    if (history.length > 1) history.back();
    else window.location.href = "/";
  });

  const accountMenu = document.querySelector(".account-menu");
  if (accountMenu) {
    document.addEventListener("pointerdown", (event) => {
      if (accountMenu.open && !accountMenu.contains(event.target)) accountMenu.open = false;
    }, true);
    document.addEventListener("keydown", (event) => {
      if ((event.key === "Escape" || event.key === "Esc") && accountMenu.open) {
        accountMenu.open = false;
        accountMenu.querySelector("summary")?.focus();
      }
    });
  }

})();

document.querySelectorAll("[data-hardcover-test]").forEach((button) => {
  button.addEventListener("click", async () => {
    const form = button.closest("form");
    const input = document.getElementById(button.dataset.tokenInput);
    const result = form.querySelector("[data-hardcover-result]");
    if (!input || !input.value.trim()) {
      result.textContent = "Enter a token first.";
      return;
    }
    const body = new FormData();
    body.append("api_token", input.value.trim());
    body.append("csrfmiddlewaretoken", form.querySelector("[name=csrfmiddlewaretoken]").value);
    button.disabled = true;
    result.textContent = "Testing connection…";
    try {
      const response = await fetch(button.dataset.testUrl, {method: "POST", body, credentials: "same-origin"});
      const payload = await response.json();
      result.textContent = payload.message;
    } catch (error) {
      result.textContent = "The connection test could not be completed.";
    } finally {
      button.disabled = false;
    }
  });
});

document.addEventListener("DOMContentLoaded", () => {
  const mode = document.getElementById("id_announcement_mode");
  const custom = document.getElementById("id_announcement");
  if (mode && custom) {
    const container = custom.closest("p");
    const updateAnnouncementField = () => {
      const show = mode.value === "custom";
      container.hidden = !show;
      custom.disabled = !show;
    };
    mode.addEventListener("change", updateAnnouncementField);
    updateAnnouncementField();
  }

  const enabled = document.getElementById("id_announcement_enabled");
  if (enabled && custom && !mode) {
    const container = custom.closest("p");
    const updateGroupAnnouncementField = () => { container.hidden = !enabled.checked; };
    enabled.addEventListener("change", updateGroupAnnouncementField);
    updateGroupAnnouncementField();
  }

  const submissionForm = document.querySelector("[data-submission-form]");
  submissionForm?.querySelectorAll("[data-theme-response]").forEach((response) => {
    const themeId = response.dataset.themeResponse;
    const claim = submissionForm.querySelector(`input[name="themes"][value="${themeId}"]`);
    const container = response.closest("p");
    if (!claim || !container) return;
    const option = claim.closest("#id_themes > div");
    if (option) {
      option.classList.add("theme-claim-option");
      container.classList.add("theme-response");
      option.append(container);
    }
    const updateThemeResponse = () => {
      container.hidden = !claim.checked;
      response.disabled = !claim.checked;
    };
    claim.addEventListener("change", updateThemeResponse);
    updateThemeResponse();
  });

  document.querySelectorAll("[data-close-details]").forEach((button) => {
    button.addEventListener("click", () => button.closest("details")?.removeAttribute("open"));
  });

  const directory = document.querySelector("[data-account-directory]");
  const accountTable = document.querySelector("[data-account-table]");
  if (directory && accountTable) {
    const search = directory.querySelector("[data-account-search]");
    const statusControls = [...directory.querySelectorAll("[data-account-status]")];
    const rows = [...accountTable.querySelectorAll("[data-account-row]")];
    const resultSummary = document.querySelector("[data-account-results]");
    const filterEmpty = accountTable.querySelector("[data-filter-empty]");
    const body = accountTable.tBodies[0];
    let sortKey = "account";
    let sortDirection = "asc";

    const applyAccountDirectory = () => {
      const query = search.value.trim().toLocaleLowerCase();
      const selectedStatus = statusControls.find((control) => control.checked)?.value || "all";
      rows.sort((left, right) => {
        const comparison = left.dataset[sortKey].localeCompare(right.dataset[sortKey], undefined, {sensitivity: "base"});
        return sortDirection === "asc" ? comparison : -comparison;
      }).forEach((row) => body.insertBefore(row, filterEmpty));

      let visibleCount = 0;
      rows.forEach((row) => {
        const identityMatches = !query || row.dataset.search.includes(query);
        const statusMatches = selectedStatus === "all" || row.dataset.status === selectedStatus;
        row.hidden = !(identityMatches && statusMatches);
        if (!row.hidden) visibleCount += 1;
      });
      filterEmpty.hidden = visibleCount !== 0 || rows.length === 0;
      resultSummary.textContent = `${visibleCount} of ${rows.length} accounts shown`;
    };

    search.addEventListener("input", applyAccountDirectory);
    statusControls.forEach((control) => control.addEventListener("change", applyAccountDirectory));
    accountTable.querySelectorAll("[data-account-sort]").forEach((button) => {
      button.addEventListener("click", () => {
        const nextKey = button.dataset.accountSort;
        sortDirection = sortKey === nextKey && sortDirection === "asc" ? "desc" : "asc";
        sortKey = nextKey;
        accountTable.querySelectorAll("th[aria-sort]").forEach((heading) => heading.setAttribute("aria-sort", "none"));
        button.closest("th").setAttribute("aria-sort", sortDirection === "asc" ? "ascending" : "descending");
        applyAccountDirectory();
      });
    });
    applyAccountDirectory();
  }
});

document.querySelectorAll("[data-catalog-tools]").forEach((tools) => {
  const submissionForm = document.querySelector("[data-submission-form]");
  const status = tools.querySelector("[data-catalog-status]");
  const results = tools.querySelector("[data-catalog-results]");
  const entryMode = document.querySelector("[data-entry-mode]");
  const csrf = submissionForm.querySelector("[name=csrfmiddlewaretoken]").value;

  const request = async (action, values = {}) => {
    const body = new FormData();
    body.append("action", action);
    body.append("csrfmiddlewaretoken", csrf);
    Object.entries(values).forEach(([key, value]) => body.append(key, value));
    const response = await fetch(tools.dataset.catalogUrl, {method: "POST", body, credentials: "same-origin"});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.message || "Hardcover lookup failed.");
    return payload;
  };

  const clearResults = () => { results.replaceChildren(); };

  const formatValue = (format, audioSeconds) => {
    const value = (format || "").toLowerCase();
    if (audioSeconds || value.includes("audio")) return "audio";
    if (value.includes("paperback")) return "paperback";
    if (value.includes("hardcover") || value.includes("hardbound")) return "hardcover";
    if (value.includes("ebook") || value.includes("e-book") || value.includes("kindle")) return "ebook";
    if (value.includes("manga")) return "manga";
    return "other";
  };

  const applyEdition = (edition) => {
    document.getElementById("id_catalog_selection").value = edition.catalog_selection || "";
    document.getElementById("id_title").value = edition.title || "";
    document.getElementById("id_author").value = edition.author || "";
    document.getElementById("id_book_format").value = formatValue(edition.format, edition.audio_seconds);
    document.getElementById("id_submitted_pages").value = edition.pages || "";
    document.getElementById("id_submitted_pages").readOnly = true;
    document.getElementById("id_reference_url").value = edition.source_url || "";
    const scoring = edition.scoring_format && edition.format?.toLowerCase().includes("audio") ? ` Scored using the ${edition.scoring_format} page count.` : "";
    status.textContent = `Edition selected.${scoring}`;
    entryMode.textContent = `${edition.verification_label}: page count locked and automatically verified.`;
    clearResults();
    document.getElementById("id_title").focus();
  };

  const makeCard = (title, details, buttonLabel, onClick) => {
    const card = document.createElement("div");
    card.className = "card";
    const heading = document.createElement("h3");
    heading.textContent = title || "Untitled";
    const copy = document.createElement("p");
    copy.textContent = details;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button";
    button.textContent = buttonLabel;
    button.addEventListener("click", onClick);
    card.append(heading, copy, button);
    return card;
  };

  const showEditions = async (bookId) => {
    status.textContent = "Loading editions…";
    clearResults();
    try {
      const payload = await request("editions", {book_id: bookId});
      if (!payload.editions.length) {
        status.textContent = "No editions were returned. Enter the book manually below.";
        return;
      }
      status.textContent = "Select the edition and format you used.";
      payload.editions.forEach((edition) => {
        const details = [edition.format, edition.pages ? `${edition.pages} pages` : "Page count unavailable", edition.isbn_13 || edition.isbn_10, edition.release_date].filter(Boolean).join(" · ");
        results.append(makeCard(edition.title || "Edition", details, "Use Edition", async () => {
          status.textContent = "Loading edition details…";
          try {
            const selected = await request("edition", {edition_id: edition.edition_id});
            if (selected.manual_required) {
              clearToManual();
              status.textContent = selected.message;
            } else applyEdition(selected.result);
          } catch (error) {
            status.textContent = `${error.message} You can still enter the book manually.`;
          }
        }));
      });
    } catch (error) {
      status.textContent = `${error.message} You can still enter the book manually.`;
    }
  };

  tools.querySelector("[data-catalog-search]").addEventListener("click", async () => {
    const query = document.getElementById("catalog-search").value.trim();
    if (!query) { status.textContent = "Enter a title, author, or ISBN first."; return; }
    status.textContent = "Searching Hardcover…";
    clearResults();
    try {
      const payload = await request("search", {query});
      if (!payload.results.length) { status.textContent = "No matching books were found. Try manual entry below."; return; }
      status.textContent = payload.cached ? "Showing cached results." : "Select a book to view its editions.";
      payload.results.forEach((book) => {
        const details = [book.author, book.default_pages ? `${book.default_pages} default pages` : "", book.subtitle].filter(Boolean).join(" · ");
        results.append(makeCard(book.title, details, "View Editions", () => showEditions(book.book_id)));
      });
    } catch (error) {
      status.textContent = `${error.message} You can still enter the book manually.`;
    }
  });

  tools.querySelector("[data-catalog-link]").addEventListener("click", async () => {
    const url = document.getElementById("catalog-link").value.trim();
    if (!url) { status.textContent = "Paste a Hardcover book or edition link first."; return; }
    status.textContent = "Importing Hardcover link…";
    clearResults();
    try {
      const payload = await request("link", {url});
      if (payload.result.edition_required) await showEditions(payload.result.book_id);
      else {
        const selected = await request("edition", {edition_id: payload.result.edition_id});
        if (selected.manual_required) {
          clearToManual();
          status.textContent = selected.message;
        } else applyEdition(selected.result);
      }
    } catch (error) {
      status.textContent = `${error.message} You can still enter the book manually.`;
    }
  });

  const clearToManual = () => {
    document.getElementById("id_catalog_selection").value = "";
    ["id_title", "id_author", "id_submitted_pages", "id_reference_url"].forEach((id) => { document.getElementById(id).value = ""; });
    document.getElementById("id_book_format").value = "";
    document.getElementById("id_submitted_pages").readOnly = false;
    document.getElementById("catalog-search").value = "";
    document.getElementById("catalog-link").value = "";
    clearResults();
    entryMode.textContent = "Manual Entry";
    document.getElementById("id_title").focus();
  };

  document.querySelector("[data-clear-book]").addEventListener("click", () => {
    clearToManual();
    status.textContent = "Hardcover selection cleared. Enter the book details manually below.";
  });
});
