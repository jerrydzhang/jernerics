/* Exceptions page behaviors: grouping mode toggle, working selection
   (count, group cascade, clear), and the ?sweep= deep link that
   pre-expands and scrolls to one sweep's group (prototype failures
   page script, ported to survive Dash re-renders). */
(function () {
  "use strict";

  /* dcc.Input renders the user className on a wrapper div, not on the
     <input>, so checkboxes are classified via closest() and each sweep
     id is read off the input's name= (dcc.Input has no custom data-*
     attributes). */
  function sweepBoxes(root) {
    return Array.from((root || document).querySelectorAll(".sel-sweep input"));
  }

  function selKind(box) {
    if (box.closest(".sel-group")) {
      return "sel-group";
    }
    if (box.closest(".sel-sweep")) {
      return "sel-sweep";
    }
    return null;
  }

  function selectedSweeps() {
    return sweepBoxes()
      .filter(function (box) {
        return box.checked;
      })
      .map(function (box) {
        return box.name;
      });
  }

  function updateCount() {
    var target = document.getElementById("exc-selection-count");
    if (!target) {
      return;
    }
    var count = selectedSweeps().length;
    target.textContent =
      count + (count === 1 ? " sweep selected" : " sweeps selected");
  }

  function clearSelection() {
    document
      .querySelectorAll("#exc-groupsets input[type=checkbox]")
      .forEach(function (box) {
        box.checked = false;
      });
    updateCount();
  }

  function setMode(mode) {
    document.querySelectorAll("#exc-mode-seg .gmode").forEach(function (seg) {
      seg.classList.toggle("on", seg.dataset.mode === mode);
    });
    document.querySelectorAll(".groupset").forEach(function (set) {
      set.hidden = set.dataset.mode !== mode;
    });
  }

  document.addEventListener("click", function (event) {
    var mode = event.target.closest
      ? event.target.closest("#exc-mode-seg .gmode")
      : null;
    if (mode) {
      setMode(mode.dataset.mode);
      return;
    }
    if (event.target.id === "exc-clear") {
      clearSelection();
    }
  });

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!box.closest) {
      return;
    }
    var kind = selKind(box);
    if (kind === "sel-group") {
      var group = box.closest(".failgroup");
      if (group) {
        sweepBoxes(group).forEach(function (sweep) {
          sweep.checked = box.checked;
        });
      }
      updateCount();
    } else if (kind === "sel-sweep") {
      updateCount();
    }
  });

  /* The deep link runs once per full page load: the first time the
     rollup mounts, open the target sweep's chain, collapse its
     siblings, scroll it into view, and outline it. */
  var applied = false;

  function focusSweep() {
    if (applied) {
      return;
    }
    var root = document.getElementById("exc-groupsets");
    if (!root || !root.querySelector(".groupset")) {
      return;
    }
    applied = true;
    var id = new URLSearchParams(window.location.search).get("sweep");
    if (!id) {
      return;
    }
    var target = document.getElementById("sweep-" + id);
    if (!target) {
      return;
    }
    var chain = [];
    var node = target;
    while (node) {
      if (node.tagName === "DETAILS") {
        node.open = true;
        if (node !== target) {
          chain.unshift(node);
        }
      }
      node = node.parentElement;
    }
    var wrapper = chain.length ? chain[0].parentElement : target.parentElement;
    wrapper.querySelectorAll(":scope > details").forEach(function (details) {
      if (details !== target && !details.contains(target)) {
        details.open = false;
      }
    });
    requestAnimationFrame(function () {
      target.scrollIntoView({ block: "center" });
      target.style.outline = "2px solid #2563eb";
    });
  }

  if (window.MutationObserver) {
    new MutationObserver(focusSweep).observe(document.body, {
      childList: true,
      subtree: true,
    });
  }
  focusSweep();
})();
