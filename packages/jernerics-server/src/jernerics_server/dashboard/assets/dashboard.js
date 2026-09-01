/* Narrow viewports start with the Browse scope panel collapsed
   (jernerics-bo0): a <details> cannot be closed from CSS, so each
   freshly mounted panel loses its open attribute once, only while the
   stacked breakpoint is active. A panel the user opened stays open. */
(function () {
  if (!window.matchMedia || !window.MutationObserver) {
    return;
  }
  var narrow = window.matchMedia("(max-width: 960px)");

  function close(panel) {
    if (panel.dataset.autocollapsed) {
      return;
    }
    panel.removeAttribute("open");
    panel.dataset.autocollapsed = "1";
  }

  function collapse(scope) {
    if (!narrow.matches || !scope) {
      return;
    }
    if (scope.querySelectorAll) {
      var panels = scope.querySelectorAll("details.scope-browser[open]");
      for (var index = 0; index < panels.length; index += 1) {
        close(panels[index]);
      }
    }
    if (scope.matches && scope.matches("details.scope-browser[open]")) {
      close(scope);
    }
  }

  function collapseAdded(records) {
    for (var index = 0; index < records.length; index += 1) {
      var added = records[index].addedNodes;
      for (var node = 0; node < added.length; node += 1) {
        collapse(added[node]);
      }
    }
  }

  if (narrow.addEventListener) {
    narrow.addEventListener("change", function () {
      collapse(document);
    });
  } else if (narrow.addListener) {
    narrow.addListener(function () {
      collapse(document);
    });
  }
  new MutationObserver(collapseAdded).observe(document.body, {
    childList: true,
    subtree: true,
  });
  collapse(document);
})();

/* Truncation baseline (jernerics-l8f): a clipped grid cell or fact-row
   value exposes its full text through a native title, so no clipped
   value is unreachable. Fully visible (wrapped) values get no tooltip. */
(function () {
  if (!window.addEventListener) {
    return;
  }
  var SELECTOR = ".ag-cell, .fact-row span";
  document.addEventListener("mouseover", function (event) {
    var target = event.target.closest ? event.target.closest(SELECTOR) : null;
    if (!target) {
      return;
    }
    if (target.scrollWidth > target.clientWidth + 1) {
      target.setAttribute("title", target.textContent);
    } else if (target.hasAttribute("title")) {
      target.removeAttribute("title");
    }
  });
})();
