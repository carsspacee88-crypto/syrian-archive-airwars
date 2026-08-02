(() => {
  "use strict";

  const statusLabels = {
    queued: "في الطابور", running: "قيد التنفيذ", pause_requested: "جارٍ الإيقاف",
    paused: "متوقفة مؤقتًا", cancel_requested: "جارٍ الإلغاء", cancelled: "ملغاة",
    completed: "مكتملة", completed_with_gaps: "مكتملة مع استرداد مؤجل",
    completed_with_errors: "مكتملة مع خطأ تشغيلي", failed: "متعثرة"
  };
  const stageLabels = {
    queued: "الطابور", preparing: "التهيئة", manifest: "بناء البيان", incidents: "الحوادث",
    sources: "المصادر", report: "التقرير", complete: "مكتملة", cancelled: "ملغاة",
    infrastructure_error: "خطأ بنيوي", queue_error: "خطأ الطابور"
  };
  const failureLabels = {
    archive_lookup_failed: "فشل بحث الأرشيف", no_archive_capture: "لا توجد لقطة أرشيفية",
    low_quality_content: "محتوى غير صالح", login_required: "يتطلب تسجيل دخول",
    blocked: "محجوب", timed_out: "انتهت المهلة", unavailable: "غير متاح",
    unsupported_content_type: "وسيط غير منزّل", media_metadata_preserved: "بيانات الوسيط محفوظة",
    recovery_deferred: "مؤجل للاسترداد", successful_partial: "نص جزئي موثوق",
    snapshot_preserved: "لقطة الحادثة محفوظة",
    verified_record_reused: "سجل موثّق معاد الاستخدام", internal_error: "خطأ تشغيلي معزول",
    failed: "فشل غير مصنف"
  };
  const terminalStatuses = new Set(["completed", "completed_with_gaps", "completed_with_errors", "failed", "cancelled"]);
  const successStatuses = new Set(["successful", "successful_partial", "cached", "embedded_text_preserved"]);
  const policyStatuses = new Set(["media_metadata_preserved"]);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
  const number = (value, digits = 0) => Number(value || 0).toLocaleString("ar", { maximumFractionDigits: digits });
  const percent = (completed, total) => total ? Math.min(100, Math.max(0, completed / total * 100)) : 0;
  const formatDuration = (seconds) => {
    const value = Math.max(0, Number(seconds || 0));
    if (!value) return "—";
    if (value < 60) return `${number(value, 0)} ث`;
    if (value < 3600) return `${number(Math.floor(value / 60))} د ${number(value % 60)} ث`;
    if (value < 86400) return `${number(Math.floor(value / 3600))} س ${number(Math.floor(value % 3600 / 60))} د`;
    return `${number(Math.floor(value / 86400))} ي ${number(Math.floor(value % 86400 / 3600))} س`;
  };
  const setText = (selector, value) => {
    const element = document.querySelector(selector);
    const text = String(value ?? "");
    if (element && element.textContent !== text) element.textContent = text;
  };
  const setBar = (selector, value) => {
    const element = document.querySelector(selector);
    const width = `${Math.min(100, Math.max(0, Number(value || 0)))}%`;
    if (element && element.style.width !== width) element.style.width = width;
  };
  const timing = (value) => value === null || value === undefined ? "—" : `${number(value, 1)}ث`;

  document.querySelectorAll("[data-status]").forEach(element => {
    element.textContent = statusLabels[element.dataset.status] || element.dataset.status;
  });

  function setupNewJob() {
    const form = document.querySelector("[data-new-job-form]");
    if (!form) return;
    const profiles = {
      conservative: { workers: 32, perHost: 3, social: 6, archive: 6, delay: 0.1, timeout: 8, fastTimeout: 4, chunk: 1000, label: "محافظ" },
      balanced: { workers: 64, perHost: 4, social: 12, archive: 12, delay: 0.05, timeout: 6, fastTimeout: 3, chunk: 5000, label: "V4 متوازن" },
      turbo: { workers: 96, perHost: 6, social: 16, archive: 16, delay: 0.03, timeout: 5, fastTimeout: 2.5, chunk: 5000, label: "V4 أقصى" },
      custom: { label: "مخصص" }
    };
    const first = form.querySelector("#first-sequence");
    const last = form.querySelector("#last-sequence");
    const profileInput = form.querySelector("#performance-profile");
    const updateSummary = () => {
      const count = Math.max(0, Number(last.value) - Number(first.value) + 1);
      const profile = profiles[profileInput.value] || profiles.balanced;
      setText("#range-count", `${number(count)} حادثة`);
      setText("#submit-summary", `${number(count)} حادثة · ملف ${profile.label}`);
    };
    document.querySelectorAll("[data-profile]").forEach(button => button.addEventListener("click", () => {
      const name = button.dataset.profile;
      const profile = profiles[name];
      document.querySelectorAll("[data-profile]").forEach(item => item.classList.toggle("selected", item === button));
      profileInput.value = name;
      form.querySelector("#workers").value = profile.workers;
      form.querySelector("#per-host-workers").value = profile.perHost;
      form.querySelector("#social-workers").value = profile.social;
      form.querySelector("#archive-workers").value = profile.archive;
      form.querySelector("#delay").value = profile.delay;
      form.querySelector("#timeout").value = profile.timeout;
      form.querySelector("#fast-timeout").value = profile.fastTimeout;
      form.querySelector("#source-chunk-size").value = profile.chunk;
      updateSummary();
    }));
    document.querySelectorAll("[data-range-size]").forEach(button => button.addEventListener("click", () => {
      last.value = Math.min(8114, Number(first.value || 1) + Number(button.dataset.rangeSize) - 1);
      updateSummary();
    }));
    first.addEventListener("input", updateSummary);
    last.addEventListener("input", updateSummary);
    ["#workers", "#per-host-workers", "#social-workers", "#archive-workers", "#delay", "#timeout", "#fast-timeout", "#source-chunk-size"].forEach(selector => {
      form.querySelector(selector).addEventListener("input", () => {
        profileInput.value = "custom";
        document.querySelectorAll("[data-profile]").forEach(item => item.classList.remove("selected"));
        updateSummary();
      });
    });
    updateSummary();
  }

  function setupJobMonitor() {
    const root = document.querySelector("[data-job-id]");
    if (!root) return;
    const jobId = root.dataset.jobId;
    let currentStatus = root.dataset.initialStatus;
    let source = null;
    let pollTimer = null;
    let pollDelay = 2500;
    let transportState = "connecting";
    const sectionSignatures = new Map();

    const sectionChanged = (name, value) => {
      const signature = JSON.stringify(value ?? null);
      if (sectionSignatures.get(name) === signature) return false;
      sectionSignatures.set(name, signature);
      return true;
    };

    const setConnection = (state, label) => {
      transportState = state;
      const element = document.querySelector("#live-state");
      if (!element) return;
      const className = `live-state ${state}`;
      const markup = `<i></i> ${escapeHtml(label)}`;
      if (element.className !== className) element.className = className;
      if (element.innerHTML !== markup) element.innerHTML = markup;
    };

    const renderEvents = (events) => {
      const target = document.querySelector("#event-log");
      if (!target || !sectionChanged("events", events)) return;
      target.innerHTML = (events || []).map(event => {
        const parsed = event.created_at ? new Date(event.created_at) : null;
        const time = parsed && !Number.isNaN(parsed.valueOf())
          ? parsed.toLocaleTimeString("ar", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
          : "—";
        return `<li class="event-${escapeHtml(event.level)}"><time>${escapeHtml(time)}</time><span>${escapeHtml(event.message)}</span></li>`;
      }).join("") || '<li class="empty-small">لا توجد أحداث بعد</li>';
    };

    const renderItems = (items) => {
      const target = document.querySelector("#recent-items");
      if (!target) return;
      setText("#items-count", number((items || []).length));
      if (!sectionChanged("items", items)) return;
      target.innerHTML = (items || []).map(item => {
        const detail = item.detail || {};
        const host = detail.host ? `<span>${escapeHtml(detail.host)}</span>` : "";
        const quality = detail.quality_score !== null && detail.quality_score !== undefined ? `<span>جودة ${escapeHtml(detail.quality_score)}</span>` : "";
        const provenance = detail.provenance ? `<span>${escapeHtml(detail.provenance)}</span>` : "";
        const timingParts = [
          ["شبكة", "network_seconds"], ["طابور", "queue_seconds"],
          ["تهدئة", "pacing_seconds"], ["حفظ", "persist_seconds"]
        ].filter(([, key]) => Object.prototype.hasOwnProperty.call(detail, key))
          .map(([label, key]) => `${label} ${number(detail[key], 1)}ث`);
        const timings = timingParts.length ? `<span>${escapeHtml(timingParts.join(" · "))}</span>` : "";
        const rowClass = successStatuses.has(item.status) ? "item-success" : policyStatuses.has(item.status) ? "item-policy" : "item-warning";
        return `<div class="item-row ${rowClass}">
          <span class="kind-pill">${item.kind === "source" ? "مصدر" : "حادثة"}</span>
          <div class="item-main"><b class="ltr">${escapeHtml(item.identity)}</b><small>${escapeHtml(failureLabels[item.status] || item.status)}</small><em>${host}${quality}${provenance}${timings}</em></div>
          <time>${timing(item.duration_seconds)}</time>
        </div>`;
      }).join("") || '<p class="empty-small">بانتظار أول عنصر محفوظ…</p>';
    };

    const renderHosts = (hosts) => {
      const target = document.querySelector("#host-breakdown");
      if (!target || !sectionChanged("hosts", hosts)) return;
      target.innerHTML = (hosts || []).map(row => {
        const decided = Number(row.successful || 0) + Number(row.policy_complete || 0);
        const ok = percent(decided, row.total);
        const wait = Number(row.queue_p50_seconds || 0) + Number(row.pacing_p50_seconds || 0);
        const waitLabel = wait > 0 ? ` · انتظار P50 ${number(wait, 1)}ث` : "";
        return `<div class="host-row"><div><b class="ltr">${escapeHtml(row.host)}</b><small>${number(decided)}/${number(row.total)} محسوم${escapeHtml(waitLabel)}</small></div><div class="mini-track"><i style="width:${ok}%"></i></div></div>`;
      }).join("") || '<p class="empty-small">تظهر المضيفات بعد بدء المصادر</p>';
    };

    const renderFailures = (failures) => {
      const target = document.querySelector("#failure-breakdown");
      if (!target || !sectionChanged("failures", failures)) return;
      const rows = Object.entries(failures || {}).sort((a, b) => b[1] - a[1]);
      target.innerHTML = rows.map(([status, count]) => `<div><span>${escapeHtml(failureLabels[status] || status)}</span><b>${number(count)}</b></div>`).join("") || '<p class="empty-small success-text">لا توجد حالات غير ناجحة في النافذة الأخيرة</p>';
    };

    const renderBottleneck = (bottleneck, backlog) => {
      const panel = document.querySelector("#bottleneck-panel");
      if (!panel) return;
      if (!bottleneck) {
        panel.classList.add("hidden");
        return;
      }
      panel.classList.remove("hidden");
      setText("#bottleneck-host", bottleneck.host || "—");
      const reason = bottleneck.reason === "queue" ? "انتظار طابور المضيف" : "تهدئة الطلبات للمضيف";
      setText("#bottleneck-detail", `${reason} · وسيط ${timing(bottleneck.seconds)} · عينة ${number(bottleneck.sample_size)}`);
      setText("#backlog-count", `${number(backlog)} متبقٍ`);
    };

    const updateActions = (status) => {
      const allowed = {
        pause: status === "running",
        resume: status === "paused" || status === "failed",
        cancel: ["queued", "running", "pause_requested", "paused"].includes(status),
        retry_failed: terminalStatuses.has(status)
      };
      document.querySelectorAll("[data-action-form]").forEach(form => {
        const button = form.querySelector("button");
        button.disabled = !allowed[form.dataset.action];
      });
    };

    const render = (data) => {
      currentStatus = data.status;
      const status = document.querySelector("#job-status");
      if (status) {
        const statusText = statusLabels[data.status] || data.status;
        const statusClass = `status status-${data.status}`;
        if (status.textContent !== statusText) status.textContent = statusText;
        if (status.className !== statusClass) status.className = statusClass;
      }
      setText("#job-stage", stageLabels[data.stage] || data.stage);
      setText("#overall-percent", `${number(data.percent, 1)}٪`);
      setBar("#overall-bar", data.percent);
      const incidentPercent = percent(data.incidents.completed, data.incidents.total);
      const sourcePercent = percent(data.sources.completed, data.sources.total);
      setText("#incident-progress", `${number(data.incidents.completed)}/${number(data.incidents.total)}`);
      setText("#incident-percent", `${number(incidentPercent)}٪`);
      setText("#incident-failed", `${number(data.incidents.failed)} فشل`);
      setBar("#incident-bar", incidentPercent);
      setText("#source-progress", `${number(data.sources.completed)}/${number(data.sources.total)}`);
      setText("#source-percent", `${number(sourcePercent)}٪`);
      setText("#source-failed", `${number(data.sources.deferred)} استرداد · ${number(data.sources.policy_complete)} مكتمل بالسياسة · ${number(data.sources.operational_errors)} خطأ`);
      setBar("#source-bar", sourcePercent);
      setText("#job-rate", number(data.performance.rate_current, 1));
      setText("#job-rate-average", number(data.performance.rate_average, 1));
      setText("#job-eta", data.performance.eta_seconds ? formatDuration(data.performance.eta_seconds) : "—");
      setText("#decision-rate", data.sources.completed ? `${number(data.performance.decision_rate, 1)}٪` : "—");
      setText("#success-rate", data.sources.completed ? `${number(data.performance.success_rate, 1)}٪` : "—");
      setText("#coverage-rate", data.sources.total ? `${number(data.performance.content_coverage_rate, 1)}٪` : "—");
      setText("#reliability-rate", data.sources.completed ? `${number(data.performance.operational_reliability, 1)}٪` : "—");
      setText("#elapsed-time", formatDuration(data.performance.elapsed_seconds));
      setText("#p50-time", timing(data.performance.recent_p50_seconds));
      setText("#p90-time", timing(data.performance.recent_p90_seconds));
      setText("#network-p50", timing(data.performance.recent_network_p50_seconds));
      setText("#network-p90", timing(data.performance.recent_network_p90_seconds));
      setText("#queue-p50", timing(data.performance.recent_queue_p50_seconds));
      setText("#queue-p90", timing(data.performance.recent_queue_p90_seconds));
      setText("#pacing-p50", timing(data.performance.recent_pacing_p50_seconds));
      setText("#pacing-p90", timing(data.performance.recent_pacing_p90_seconds));
      setText("#persist-p50", timing(data.performance.recent_persist_p50_seconds));
      setText("#persist-p90", timing(data.performance.recent_persist_p90_seconds));
      setText("#unattributed-p50", timing(data.performance.recent_unattributed_p50_seconds));
      setText("#performance-backlog", number(data.performance.backlog));
      setText("#worker-count", number(data.configuration.workers || 0));
      const updated = data.updated_at ? new Date(data.updated_at) : null;
      setText("#last-update", updated && !Number.isNaN(updated.valueOf()) ? `آخر تحديث ${updated.toLocaleTimeString("ar", {hour:"2-digit", minute:"2-digit", second:"2-digit"})}` : "الآن");
      const error = document.querySelector("#structural-error");
      if (error) {
        error.classList.toggle("hidden", !data.last_error);
        if (data.last_error) error.textContent = `خطأ بنيوي: ${data.last_error}`;
      }
      renderEvents(data.events);
      renderItems(data.items);
      renderHosts(data.host_breakdown);
      renderFailures(data.failure_breakdown);
      renderBottleneck(data.bottleneck, data.performance.backlog);
      updateActions(data.status);
      if (terminalStatuses.has(data.status)) {
        setConnection("finished", "اكتملت المزامنة");
      } else if (data.performance.progress_stalled) {
        setConnection("stalled", `بلا تقدم منذ ${formatDuration(data.performance.progress_stale_seconds)}`);
      } else if (transportState === "stalled") {
        setConnection("online", "متصل لحظيًا");
      }
    };

    const fetchSnapshot = async () => {
      try {
        const response = await fetch(`/admin/api/jobs/${jobId}`, { credentials: "same-origin", cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        render(await response.json());
        pollDelay = 2500;
        if (!terminalStatuses.has(currentStatus)) pollTimer = window.setTimeout(fetchSnapshot, pollDelay);
      } catch (_) {
        pollDelay = Math.min(15000, Math.round(pollDelay * 1.6));
        setConnection("offline", "إعادة الاتصال تلقائيًا");
        pollTimer = window.setTimeout(fetchSnapshot, pollDelay);
      }
    };

    const startStream = () => {
      if (!("EventSource" in window)) {
        setConnection("fallback", "تحديث دوري");
        fetchSnapshot();
        return;
      }
      source = new EventSource(`/admin/api/jobs/${jobId}/stream`);
      source.addEventListener("open", () => setConnection("online", "متصل لحظيًا"));
      source.addEventListener("snapshot", event => {
        if (pollTimer) window.clearTimeout(pollTimer);
        render(JSON.parse(event.data));
        if (terminalStatuses.has(currentStatus)) source.close();
      });
      source.addEventListener("error", () => {
        source.close();
        if (!terminalStatuses.has(currentStatus)) {
          setConnection("fallback", "تحديث دوري احتياطي");
          fetchSnapshot();
        }
      });
    };

    document.querySelectorAll("[data-action-form]").forEach(form => form.addEventListener("submit", event => {
      const action = form.dataset.action;
      const message = action === "cancel" ? "سيُلغى التنفيذ عند أقرب نقطة حفظ مع الاحتفاظ بكل النتائج. هل تتابع؟" : action === "retry_failed" ? "ستُعاد عناصر الاسترداد المؤجلة والأخطاء التشغيلية فقط. هل تتابع؟" : "";
      if (message && !window.confirm(message)) event.preventDefault();
    }));
    updateActions(currentStatus);
    startStream();
  }

  setupNewJob();
  setupJobMonitor();
})();
