(function () {
  "use strict";
  document.querySelectorAll("a[target='_blank']").forEach(function (link) {
    link.rel = "noopener noreferrer";
  });
})();
