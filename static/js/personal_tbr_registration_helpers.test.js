const assert = require("node:assert/strict");
const {resolveSelection} = require("./personal_tbr_registration_helpers.js");

const empty = () => ({occupied: false, available: true, title: "", author: ""});
const book = (title, author) => ({occupied: true, available: true, title, author});

let rows = Array.from({length: 9}, empty);
assert.deepEqual(resolveSelection(rows, "global-add", null, {title: "One", author: "A"}), {kind: "add", index: 0});
rows = [book("Existing", "A"), ...Array.from({length: 8}, empty)];
assert.deepEqual(resolveSelection(rows, "global-add", null, {title: "Two", author: "B"}), {kind: "add", index: 1});
rows = [book("One", "A"), book("Two", "B"), book("Three", "C"), ...Array.from({length: 6}, empty)];
assert.deepEqual(resolveSelection(rows, "global-add", null, {title: "Four", author: "D"}), {kind: "add", index: 3});
assert.equal(rows[2].title, "Three");
assert.deepEqual(resolveSelection(rows, "global-add", null, {title: "Five", author: "E"}), {kind: "add", index: 3});
rows = Array.from({length: 9}, empty);
for (let index = 0; index < 3; index += 1) {
  const selected = {title: `Repeated ${index}`, author: "Reader"};
  const destination = resolveSelection(rows, "global-add", null, selected);
  assert.equal(destination.index, index);
  rows[destination.index] = book(selected.title, selected.author);
}
assert.deepEqual(rows.slice(0, 3).map((row) => row.title), ["Repeated 0", "Repeated 1", "Repeated 2"]);
rows = [book("One", "A"), book("Two", "B"), book("Three", "C"), ...Array.from({length: 6}, empty)];
assert.equal(resolveSelection(Array.from({length: 9}, (_, index) => book(`Book ${index}`, "A")), "global-add", null, {title: "Ten", author: "A"}).error, "maximum");
assert.equal(resolveSelection(rows, "global-add", null, {title: "one!", author: "a"}).error, "duplicate");
assert.deepEqual(resolveSelection(rows, "row-replace", 1, {title: "Replacement", author: "Writer"}), {kind: "replace", index: 1});
assert.equal(rows[0].title, "One");
assert.equal(rows[2].title, "Three");
assert.equal(resolveSelection(rows.map((row, index) => index === 1 ? {...row, available: false} : row), "row-replace", 1, {title: "Replacement", author: "Writer"}).error, "stale-target");
assert.equal(resolveSelection(rows, "row-replace", null, {title: "Replacement", author: "Writer"}).error, "stale-target");
assert.deepEqual(resolveSelection(rows, "row-replace", 1, {title: "Two", author: "B"}), {kind: "replace", index: 1});
assert.deepEqual(resolveSelection(rows, "global-add", null, {title: "Shared", author: "Different"}), {kind: "add", index: 3});

const linkedEdition = {title: "Linked Edition", author: "Catalog Author"};
assert.deepEqual(resolveSelection(rows, "global-add", null, linkedEdition), {kind: "add", index: 3});
assert.deepEqual(resolveSelection(rows, "row-replace", 1, linkedEdition), {kind: "replace", index: 1});
assert.equal(rows[0].title, "One");
assert.equal(rows[2].title, "Three");

console.log("Personal TBR selection helper tests passed.");
