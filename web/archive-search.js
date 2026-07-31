(function () {
  "use strict";

  var form = document.querySelector("[data-archive-search]");
  if (!form) return;

  var queryInput = form.querySelector("[name='q']");
  var statusInput = form.querySelector("[name='status']");
  var coordinatesInput = form.querySelector("[name='coordinates']");
  var output = document.querySelector("[data-search-results]");
  var cache = null;

  function normalize(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u064B-\u065F\u0670]/g, "")
      .replace(/[إأآٱ]/g, "ا")
      .replace(/ى/g, "ي")
      .replace(/ة/g, "ه")
      .toLowerCase();
  }

  function searchableText(record) {
    return normalize([
      record.number, record.code, record.airwars_id, record.date,
      record.location_ar, record.location_original, record.region_ar,
      record.district_ar, record.governorate_ar, record.military_ar,
      record.military_original, record.strike_type_ar,
      record.strike_type_original,
    ].join(" "));
  }

  function hasCoordinates(record) {
    return record.latitude !== null && record.latitude !== "" &&
      record.longitude !== null && record.longitude !== "" &&
      Number.isFinite(Number(record.latitude)) && Number.isFinite(Number(record.longitude));
  }

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  function render(records, totalMatches, root) {
    output.replaceChildren();
    output.appendChild(element("p", "search-summary", "عُثر على " + totalMatches.toLocaleString("ar") + " نتيجة. يُعرض أول " + records.length.toLocaleString("ar") + "."));
    var grid = element("div", "search-results-grid");
    records.forEach(function (record) {
      var card = element("article", "search-result-card");
      var link = element("a", "search-result-link");
      link.href = root + (record.path || ("cases/" + String(record.sequence).padStart(4, "0") + "/"));
      link.appendChild(element("span", "case-number", record.number || String(record.sequence).padStart(4, "0")));
      link.appendChild(element("strong", "ltr", record.code || "من دون رمز عام"));
      link.appendChild(element("span", "", record.location_ar || record.location_original || "الموقع غير مدخل"));
      link.appendChild(element("small", "", [record.date, record.region_ar].filter(Boolean).join(" · ")));
      card.appendChild(link);
      grid.appendChild(card);
    });
    output.appendChild(grid);
    output.hidden = false;
  }

  function loadData() {
    if (cache) return Promise.resolve(cache);
    return fetch(form.dataset.summaryUrl, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        cache = payload.cases || [];
        return cache;
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var query = normalize(queryInput.value.trim());
    var status = statusInput.value;
    var coordinateFilter = coordinatesInput.value;
    output.hidden = false;
    output.replaceChildren(element("p", "search-summary", "جارٍ تحميل فهرس البيانات…"));
    loadData().then(function (records) {
      var matches = records.filter(function (record) {
        if (query && !searchableText(record).includes(query)) return false;
        if (status && record.completion !== status) return false;
        if (coordinateFilter === "yes" && !hasCoordinates(record)) return false;
        if (coordinateFilter === "no" && hasCoordinates(record)) return false;
        return true;
      });
      render(matches.slice(0, 250), matches.length, form.dataset.caseRoot || "");
    }).catch(function () {
      output.replaceChildren(element("p", "empty-state", "تعذر تحميل فهرس البحث حاليًا. تبقى صفحات الفهرس والتصفح اليدوي متاحة."));
    });
  });
})();
