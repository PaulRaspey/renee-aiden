/* Renée mobile status page (#9). Polls /api/status every 10s and surfaces
 * pod state, cost, and Beacon liveness. Stop button hits /api/sleep so Paul
 * can terminate the pod from his phone without opening the desktop dashboard.
 *
 * No build step — vanilla JS, served by proxy_server's static route table.
 */
(() => {
  "use strict";
  const POLL_MS = 10000;

  const $ = (id) => document.getElementById(id);

  function setVal(id, text, cls = "") {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = "val" + (cls ? " " + cls : "");
  }

  function toast(msg, ms = 2200) {
    const el = $("toast");
    el.textContent = msg;
    el.classList.add("show");
    setTimeout(() => el.classList.remove("show"), ms);
  }

  async function refresh() {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();

      // Pod
      if (j.pod && j.pod.ok) {
        const cls = j.pod.status === "RUNNING"
          ? "ok"
          : (j.pod.status === "STOPPED" ? "err" : "warn");
        setVal("pod-status", j.pod.status || "?", cls);
        setVal("pod-gpu", j.pod.gpu_type || "—");
        const m = Math.round((j.pod.uptime_seconds || 0) / 60);
        setVal("pod-uptime", m > 0 ? m + " min" : "—");
      } else {
        setVal("pod-status", "unreachable", "warn");
        setVal("pod-gpu", "—");
        setVal("pod-uptime", "—");
      }

      // Cost
      if (j.cost) {
        const sess = j.cost.session_usd != null
          ? "$" + j.cost.session_usd.toFixed(2) : "—";
        const today = j.cost.today_usd != null
          ? "$" + j.cost.today_usd.toFixed(2) : "—";
        const month = j.cost.this_month_usd != null
          ? "$" + j.cost.this_month_usd.toFixed(2) : "—";
        const monthCls = j.cost.over_budget ? "err" :
          (j.cost.this_month_usd > (j.cost.monthly_budget_usd || Infinity) * 0.7 ? "warn" : "");
        setVal("cost-session", sess);
        setVal("cost-today", today);
        setVal("cost-month",
          j.cost.monthly_budget_usd != null
            ? `${month} / $${j.cost.monthly_budget_usd}`
            : month,
          monthCls);
      }

      // Beacon
      if (j.beacon) {
        if (!j.beacon.configured) {
          setVal("beacon-state", "off", "");
          setVal("beacon-hbs", "—");
        } else if (j.beacon.reachable) {
          setVal("beacon-state", "live", "ok");
          setVal("beacon-hbs", String(j.beacon.heartbeats || 0));
        } else {
          setVal("beacon-state", "unreachable", "err");
          setVal("beacon-hbs", "—");
        }
      }

      const ts = new Date(j.ts || Date.now());
      $("updated").textContent = "updated " + ts.toLocaleTimeString();
    } catch (e) {
      $("updated").textContent = "refresh failed: " + e.message;
    }
  }

  $("stop").addEventListener("click", async () => {
    if (!confirm("Stop the pod? This ends the current session and stops billing.")) return;
    const btn = $("stop");
    btn.disabled = true;
    btn.textContent = "Stopping…";
    try {
      const r = await fetch("/api/sleep", { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (r.ok && body.ok) {
        toast("pod stopping");
        // Force a status refresh shortly so the page reflects new state
        setTimeout(refresh, 1500);
      } else {
        toast(body.error || "stop failed");
      }
    } catch (e) {
      toast("network error: " + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Stop the pod";
    }
  });

  refresh();
  setInterval(refresh, POLL_MS);
})();
