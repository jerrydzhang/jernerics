// dash-ag-grid evaluates {"function": "..."} props as JS expressions
// whose scope includes everything on window.dashAgGridFunctions (see
// dash.plotly.com/dash-ag-grid/javascript-and-the-grid). Row ids are
// plain expression strings (workspace._SWEEP_ROW_ID etc.) so the
// component's own selectedRows handling can rewrite them — function
// objects break it — and no registered helpers remain.
// One typed comparator shared by every sortable grid (render.py builds
// the colDef references): numeric and ns kinds compare as numbers,
// everything else as text, and missing values ("—", empty, null) stay
// last in both directions. `field` names the raw row field when the
// cell shows derived display text.
function renderTypedSort(kind, field) {
  return function (valueA, valueB, nodeA, nodeB, isInverted) {
    const read = (value, node) =>
      field ? (node && node.data ? node.data[field] : null) : value;
    const empty = (v) => v === null || v === undefined || v === "" || v === "—";
    const a = read(valueA, nodeA);
    const b = read(valueB, nodeB);
    if (empty(a) !== empty(b)) return empty(a) ? 1 : -1;
    if (empty(a)) return 0;
    const dir = isInverted ? -1 : 1;
    if (kind === "numeric" || kind === "ns") return (Number(a) - Number(b)) * dir;
    return String(a).localeCompare(String(b)) * dir;
  };
}

// Value formatters over raw cells: the missing marker, and "74m ago"-
// style recency from a nanosecond stamp (computed at view time, so it
// ages honestly between re-renders).
function renderMissing(x) {
  return x === null || x === undefined || x === "" ? "—" : x;
}

function renderRelative(x) {
  if (x === null || x === undefined) return "—";
  const seconds = Math.max(0, (Date.now() - x) / 1e9);
  if (seconds < 90) return Math.floor(seconds) + "s ago";
  if (seconds < 5400) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 172800) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

// Cell renderers are React function components (props = the ag-grid
// cell params): they must return elements from createElement — a
// string renders as literal text and a DOM node cannot reconcile.
// React escapes text children, so labels need no manual escaping.
function sweepLink(href, label) {
  return React.createElement(
    "a",
    { href: href || "#", className: "sweep-link" },
    label
  );
}

function renderLinkCell(params) {
  const data = (params && params.data) || {};
  return sweepLink(data.link_href, data.link_label);
}

// Cell renderer for the index's Edit-members column; the row carries
// its target href so the column stays a plain data column.
function renderEditCell(params) {
  const data = (params && params.data) || {};
  return sweepLink(data.edit_href, "Edit members");
}

window.dashAgGridFunctions = window.dashAgGridFunctions || {};
window.dashAgGridFunctions.renderTypedSort = renderTypedSort;
window.dashAgGridFunctions.renderMissing = renderMissing;
window.dashAgGridFunctions.renderRelative = renderRelative;
window.dashAgGridFunctions.renderLinkCell = renderLinkCell;
window.dashAgGridFunctions.renderEditCell = renderEditCell;
