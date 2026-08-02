(() => {
  "use strict";
  const form = document.querySelector("[data-textual-search]");
  if (!form) return;
  const target = document.querySelector("[data-search-results]");
  const count = document.querySelector("[data-search-count]");
  let documents = null;
  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
  const fold = value => String(value ?? "").normalize("NFKD").toLocaleLowerCase().replace(/\s+/g, " ").trim();
  const load = async () => {
    if (documents) return documents;
    const response = await fetch(form.dataset.indexUrl, {cache: "force-cache"});
    if (!response.ok) throw new Error(`search_index_http_${response.status}`);
    documents = (await response.json()).documents || [];
    return documents;
  };
  const render = rows => {
    count.textContent = `${rows.length.toLocaleString("ar")} نتيجة`;
    target.innerHTML = rows.slice(0, 150).map(row => {
      const kind = row.kind === "source" ? "مصدر خارجي" : "حادثة";
      return `<a class="search-result" href="${escapeHtml(form.dataset.root + row.path)}"><strong class="ltr">${escapeHtml(row.code || row.incident_id)}</strong><span> — ${kind} · ${escapeHtml(row.date || "تاريخ غير متاح")} · ${escapeHtml(row.location || "موقع/ناشر غير متاح")}</span><small>${escapeHtml(row.snippet || "")}</small></a>`;
    }).join("") || '<p class="missing">لا توجد نتائج مطابقة.</p>';
  };
  form.addEventListener("submit", async event => {
    event.preventDefault();
    target.innerHTML = '<p class="missing">جارٍ البحث…</p>';
    try {
      const data = new FormData(form);
      const query = fold(data.get("q"));
      const status = String(data.get("source_status") || "");
      const coordinates = String(data.get("coordinates") || "");
      const date = String(data.get("date") || "").trim();
      const rows = (await load()).filter(row => {
        if (query && !row.search_text.includes(query)) return false;
        if (status && !(row.source_statuses || []).includes(status)) return false;
        if (coordinates && row.coordinate_status !== coordinates) return false;
        if (date && !String(row.date || "").startsWith(date)) return false;
        return true;
      });
      render(rows);
    } catch (error) {
      target.innerHTML = `<p class="missing">تعذر تحميل فهرس البحث: ${escapeHtml(error.message)}</p>`;
    }
  });
})();
