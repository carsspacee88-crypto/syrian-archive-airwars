(function () {
  "use strict";

  var mapNode = document.getElementById("map");
  var statusNode = document.getElementById("map-status-text");
  var shownNode = document.getElementById("map-shown-count");
  var summaryNode = document.getElementById("map-summary");
  var summaryToggle = document.getElementById("map-summary-toggle");
  var searchForm = document.getElementById("map-search");
  var searchInput = document.getElementById("map-search-input");
  if (!mapNode) return;

  function localNumber(value) {
    return Number(value || 0).toLocaleString("ar");
  }

  function setStatus(message) {
    if (statusNode) statusNode.textContent = message;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function normalize(value) {
    return String(value || "")
      .normalize("NFKD")
      .replace(/[\u064B-\u065F\u0670]/g, "")
      .replace(/[إأآٱ]/g, "ا")
      .replace(/ى/g, "ي")
      .replace(/ة/g, "ه")
      .toLowerCase();
  }

  function fallback(message) {
    mapNode.innerHTML = '<div class="map-fallback"><div><strong>تعذر تحميل الخريطة التفاعلية.</strong><p>' +
      escapeHtml(message) + '</p><p><a href="index.html">العودة إلى الحوادث</a></p></div></div>';
    setStatus("الخريطة غير متاحة حاليًا");
  }

  function getPoints() {
    if (Array.isArray(window.SYRIAN_ARCHIVE_MAP_POINTS)) {
      return Promise.resolve(window.SYRIAN_ARCHIVE_MAP_POINTS);
    }
    return fetch("data/map-points.json", { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("تعذر قراءة ملف نقاط الخريطة.");
      return response.json();
    });
  }

  function popupHtml(point) {
    var status = point.status ? '<small>حالة السجل: ' + escapeHtml(point.status) + '</small>' : "";
    return '<div dir="rtl"><strong>الحالة ' + escapeHtml(point.number) + '</strong>' +
      '<small dir="ltr">' + escapeHtml(point.code || point.internal_id) + '</small>' +
      '<span>' + escapeHtml(point.location || "الموقع غير مسمى") + '</span>' +
      '<small>' + escapeHtml(point.date || "التاريخ غير متاح") + '</small>' + status +
      '<a href="' + escapeHtml(point.path) + '">فتح صفحة الحالة</a></div>';
  }

  function buildMap(points) {
    if (!window.L) {
      fallback("لم تُحمّل مكتبة Leaflet. قد يكون الاتصال بمصدر المكتبة محجوبًا.");
      return;
    }

    mapNode.replaceChildren();
    var canvasRenderer = L.canvas({ padding: 0.65, tolerance: 5 });
    var map = L.map("map", {
      center: [35.15, 38.25],
      zoom: 6,
      minZoom: 4,
      maxZoom: 18,
      renderer: canvasRenderer,
      preferCanvas: true,
      zoomControl: true,
      worldCopyJump: false,
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      updateWhenIdle: true,
      keepBuffer: 3,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a>',
    }).addTo(map);

    var markers = [];
    var searchable = [];
    var bounds = L.latLngBounds();
    var index = 0;
    var chunkSize = 500;
    var total = points.length;

    function markerRadius() {
      var zoom = map.getZoom();
      if (zoom >= 13) return 6;
      if (zoom >= 10) return 4.5;
      if (zoom >= 8) return 3.5;
      return 2.6;
    }

    function schedule(callback) {
      if (window.requestIdleCallback) {
        window.requestIdleCallback(callback, { timeout: 80 });
      } else {
        window.requestAnimationFrame(callback);
      }
    }

    function finish() {
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 8 });
      if (shownNode) shownNode.textContent = localNumber(total);
      setStatus("تم رسم كل النقاط ذات الإحداثيات: " + localNumber(total) + " من " + localNumber(points.length) + ".");
    }

    function addChunk() {
      var end = Math.min(index + chunkSize, total);
      var radius = markerRadius();
      for (; index < end; index += 1) {
        var point = points[index];
        var lat = Number(point.lat);
        var lon = Number(point.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        var marker = L.circleMarker([lat, lon], {
          renderer: canvasRenderer,
          radius: radius,
          weight: 0.8,
          color: "#612919",
          fillColor: "#ef7953",
          fillOpacity: 0.68,
        });
        marker.bindPopup(popupHtml(point), { maxWidth: 300 });
        marker.addTo(map);
        markers.push(marker);
        searchable.push({
          marker: marker,
          text: normalize([point.number, point.code, point.internal_id, point.location, point.date].join(" ")),
        });
        bounds.extend([lat, lon]);
      }
      if (shownNode) shownNode.textContent = localNumber(index);
      setStatus("جارٍ رسم النقاط: " + localNumber(index) + " / " + localNumber(total));
      if (index < total) schedule(addChunk);
      else finish();
    }

    map.on("zoomend", function () {
      var radius = markerRadius();
      markers.forEach(function (marker) { marker.setRadius(radius); });
    });

    if (searchForm) {
      searchForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var query = normalize(searchInput && searchInput.value.trim());
        if (!query) {
          if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 8 });
          setStatus("تُعرض جميع النقاط. اكتب رمزًا أو موقعًا للانتقال إلى نقطة محددة.");
          return;
        }
        var match = searchable.find(function (item) { return item.text.includes(query); });
        if (!match) {
          setStatus("لم تُعثر على نقطة مطابقة، ولم تُخفَ أي نقطة من الخريطة.");
          return;
        }
        map.setView(match.marker.getLatLng(), Math.max(map.getZoom(), 12), { animate: true });
        match.marker.openPopup();
        setStatus("تم الانتقال إلى أول نتيجة مطابقة. تبقى جميع النقاط معروضة.");
      });
    }

    setStatus("جارٍ تجهيز " + localNumber(total) + " نقطة…");
    schedule(addChunk);
  }

  if (summaryToggle && summaryNode) {
    summaryToggle.addEventListener("click", function () {
      summaryNode.hidden = !summaryNode.hidden;
      summaryToggle.setAttribute("aria-expanded", summaryNode.hidden ? "false" : "true");
    });
  }

  getPoints().then(function (points) {
    if (!Array.isArray(points) || !points.length) {
      fallback("لا توجد نقاط صالحة في ملف البيانات الحديث.");
      return;
    }
    buildMap(points);
  }).catch(function (error) {
    fallback(error && error.message ? error.message : "حدث خطأ غير معروف.");
  });
})();
