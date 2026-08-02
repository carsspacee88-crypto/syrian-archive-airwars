(() => {
  "use strict";
  const canvas = document.getElementById("map-canvas");
  const form = document.getElementById("map-search");
  const input = document.getElementById("map-search-input");
  const status = document.getElementById("map-status-text");
  const detail = document.getElementById("map-detail");
  if (!canvas) return;
  const context = canvas.getContext("2d", {alpha: true});
  const points = Array.isArray(window.AIRWARS_TEXTUAL_MAP_POINTS) ? window.AIRWARS_TEXTUAL_MAP_POINTS : [];
  const bounds = {minLat: 31, maxLat: 38, minLon: 34, maxLon: 43};
  let rendered = [];
  const fold = value => String(value || "").normalize("NFKD").replace(/[\u064B-\u065F\u0670]/g, "").replace(/[إأآٱ]/g, "ا").replace(/ى/g, "ي").replace(/ة/g, "ه").toLowerCase();
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const project = point => {
    const padX = Math.max(28, canvas.clientWidth * .035);
    const padY = Math.max(90, canvas.clientHeight * .08);
    return {
      x: padX + ((point.lon - bounds.minLon) / (bounds.maxLon - bounds.minLon)) * (canvas.clientWidth - padX * 2),
      y: padY + ((bounds.maxLat - point.lat) / (bounds.maxLat - bounds.minLat)) * (canvas.clientHeight - padY * 2),
    };
  };
  const draw = () => {
    const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.floor(canvas.clientWidth * ratio);
    canvas.height = Math.floor(canvas.clientHeight * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    context.strokeStyle = "rgba(255,255,255,.09)";
    context.lineWidth = 1;
    for (let lon = 34; lon <= 43; lon += 1) {
      const a = project({lat: bounds.minLat, lon}); const b = project({lat: bounds.maxLat, lon});
      context.beginPath(); context.moveTo(a.x,a.y); context.lineTo(b.x,b.y); context.stroke();
    }
    for (let lat = 31; lat <= 38; lat += 1) {
      const a = project({lat, lon: bounds.minLon}); const b = project({lat, lon: bounds.maxLon});
      context.beginPath(); context.moveTo(a.x,a.y); context.lineTo(b.x,b.y); context.stroke();
    }
    context.fillStyle = "rgba(239,132,92,.62)";
    context.strokeStyle = "rgba(89,31,22,.72)";
    rendered = points.map(point => ({point, ...project(point)}));
    for (const item of rendered) {
      context.beginPath(); context.arc(item.x, item.y, 2.5, 0, Math.PI * 2); context.fill(); context.stroke();
    }
    if (status) status.textContent = `تم رسم ${points.length.toLocaleString("ar")} نقطة صالحة محليًا دون الاعتماد على Airwars.`;
  };
  const show = item => {
    if (!item || !detail) return;
    const point = item.point;
    detail.innerHTML = `<strong>${escapeHtml(point.code || point.incident_id)}</strong><small class="ltr">${escapeHtml(point.incident_id)}</small><span>${escapeHtml(point.location || "موقع غير مسمى")}</span><small>${escapeHtml(point.date || "تاريخ غير متاح")}</small><a href="${escapeHtml(point.path)}">فتح صفحة الحادثة</a>`;
    detail.hidden = false;
  };
  canvas.addEventListener("click", event => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left; const y = event.clientY - rect.top;
    let nearest = null; let distance = 12 * 12;
    for (const item of rendered) {
      const candidate = (item.x - x) ** 2 + (item.y - y) ** 2;
      if (candidate < distance) { nearest = item; distance = candidate; }
    }
    if (nearest) show(nearest);
  });
  form?.addEventListener("submit", event => {
    event.preventDefault();
    const query = fold(input?.value.trim());
    if (!query) { if (status) status.textContent = "اكتب رمز الحادثة أو الموقع للانتقال إلى أول نتيجة."; return; }
    const match = rendered.find(item => fold([item.point.code,item.point.incident_id,item.point.location,item.point.date].join(" ")).includes(query));
    if (!match) { if (status) status.textContent = "لا توجد نقطة مطابقة؛ بقيت جميع النقاط معروضة."; return; }
    show(match);
    if (status) status.textContent = "عُرضت أول نقطة مطابقة؛ لم تُخفَ أي نقاط.";
  });
  window.addEventListener("resize", draw, {passive: true});
  draw();
})();
