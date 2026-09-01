/* dash-ag-grid resolves plain-string cellRenderer names through
   window.dashAgGridComponentFunctions (dash.plotly.com/dash-ag-grid/
   javascript-and-the-grid). ClampedCell is the grid half of the shared
   truncation policy (jernerics-l8f): the cell text clamps to one line
   with an ellipsis, the full value rides the title tooltip, and one
   click opens the complete payload in a dismissible popover
   (jernerics-7v6). The limit comes from the column's clampLimit so the
   Python TEXT_LIMIT in components.py stays the single source of truth.
   Renderers must return React elements — DOM nodes crash AG Grid 35
   with React error #31 (see analysis.py _SWATCH_COLUMN). */
(function () {
  var FALLBACK_LIMIT = 120;
  var ELLIPSIS = "…";
  var openPopover = null;

  function clampText(value, limit) {
    var text = String(value === null || value === undefined ? "" : value)
      .replace(/\s+/g, " ")
      .trim();
    if (text.length <= limit) {
      return text;
    }
    return text.slice(0, limit - 1).replace(/\s+$/, "") + ELLIPSIS;
  }

  function closePopover() {
    if (openPopover) {
      openPopover.remove();
      openPopover = null;
    }
  }

  function showPopover(value, anchor) {
    var box = document.createElement("div");
    box.className = "payload-popover";
    box.dataset.value = value;
    var pre = document.createElement("pre");
    pre.textContent = value;
    box.appendChild(pre);
    document.body.appendChild(box);
    openPopover = box;
    var rect = anchor.getBoundingClientRect();
    var left = Math.min(
      Math.max(8, rect.left),
      Math.max(8, window.innerWidth - box.offsetWidth - 8)
    );
    var top = rect.bottom + 6;
    if (top + box.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, rect.top - box.offsetHeight - 6);
    }
    box.style.left = left + "px";
    box.style.top = top + "px";
  }

  document.addEventListener("click", function (event) {
    if (!openPopover) {
      return;
    }
    var path = event.composedPath ? event.composedPath() : [event.target];
    if (path.indexOf(openPopover) === -1) {
      closePopover();
    }
  });

  var funcs = (window.dashAgGridComponentFunctions =
    window.dashAgGridComponentFunctions || {});

  funcs.ClampedCell = function (props) {
    var full = props.value === null || props.value === undefined
      ? ""
      : String(props.value);
    var limit =
      props.colDef && props.colDef.clampLimit
        ? props.colDef.clampLimit
        : FALLBACK_LIMIT;
    return React.createElement(
      "span",
      {
        className: "clamped-cell",
        title: full,
        onClick: function (event) {
          event.stopPropagation();
          var shown = openPopover;
          closePopover();
          if (shown && shown.dataset.value === full) {
            return;
          }
          showPopover(full, event.currentTarget);
        },
      },
      clampText(full, limit)
    );
  };
})();
