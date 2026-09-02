(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.NorthboundTbrSelection = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function normalize(value) {
    return (value || "")
      .normalize("NFKC")
      .toLocaleLowerCase()
      .replace(/[\p{P}\p{S}]+/gu, " ")
      .trim()
      .replace(/\s+/g, " ");
  }

  function resolveSelection(rows, mode, targetIndex, selected) {
    const identity = `${normalize(selected.title)}\u0000${normalize(selected.author)}`;
    const replacingIndex = mode === "row-replace" ? targetIndex : null;
    const duplicateIndex = rows.findIndex((row, index) => (
      row.occupied
      && index !== replacingIndex
      && `${normalize(row.title)}\u0000${normalize(row.author)}` === identity
    ));
    if (duplicateIndex !== -1) {
      return {error: "duplicate", message: "That title and author are already on your Personal TBR."};
    }
    if (mode === "row-replace") {
      if (!Number.isInteger(targetIndex) || !rows[targetIndex] || !rows[targetIndex].available) {
        return {error: "stale-target", message: "That book entry is no longer available. Choose the row again or add the edition as a new book."};
      }
      return {kind: "replace", index: targetIndex};
    }
    if (rows.filter((row) => row.occupied).length >= 9) {
      return {error: "maximum", message: "Your Personal TBR already contains the maximum 9 books."};
    }
    const nextIndex = rows.findIndex((row) => !row.occupied);
    if (nextIndex === -1) {
      return {error: "maximum", message: "Your Personal TBR already contains the maximum 9 books."};
    }
    return {kind: "add", index: nextIndex};
  }

  return {normalize, resolveSelection};
});
