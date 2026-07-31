(() => {
  const root = document.querySelector("[data-job-id]");
  if (!root) return;
  const jobId = root.dataset.jobId;
  const terminal = new Set(["completed", "completed_with_errors", "failed", "cancelled"]);
  const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  async function refresh() {
    try {
      const response = await fetch(`/admin/api/jobs/${jobId}`, {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) return;
      const data = await response.json();
      const status = document.querySelector("#job-status");
      status.textContent = data.status;
      status.className = `status status-${data.status}`;
      document.querySelector("#job-stage").textContent = data.stage;
      document.querySelector("#incident-progress").textContent = `${data.incidents.completed}/${data.incidents.total}`;
      document.querySelector("#source-progress").textContent = `${data.sources.completed}/${data.sources.total}`;
      document.querySelector("#event-log").innerHTML = data.events.map(event => `<li class="event-${escapeHtml(event.level)}"><time>${escapeHtml(event.created_at.slice(11,19))}</time> ${escapeHtml(event.message)}</li>`).join("");
      if (terminal.has(data.status)) clearInterval(timer);
    } catch (_) { /* polling resumes automatically */ }
  }
  const timer = setInterval(refresh, 2000);
  refresh();
})();
