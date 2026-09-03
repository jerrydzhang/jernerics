/* Truncation baseline (jernerics-l8f): a clipped grid cell exposes its
   full text through a native title, so no clipped value is unreachable.
   Fully visible (wrapped) values get no tooltip. */
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
