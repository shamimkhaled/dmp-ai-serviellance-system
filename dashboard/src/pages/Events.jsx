import { useEffect, useState } from "react";
import { ALERT_SERVICE_URL, ALERT_LABELS } from "../lib/constants";
import { IconEmpty } from "../lib/icons";

const EVENT_ROW_H = 36;

export function EventsPage({ language }) {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [scrollTop, setScrollTop] = useState(0);
  const t = (bn, en) => language === "bn" ? bn : en;

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch(`${ALERT_SERVICE_URL}/events?limit=200`)
        .then(r => r.json())
        .then(d => { if (!cancelled) setEvents(Array.isArray(d) ? d : []); })
        .catch(() => { if (!cancelled) setEvents([]); })
        .finally(() => { if (!cancelled) setLoading(false); });
    };
    load();
    const iv = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(iv); };
  }, []);

  const typeLabel = (type) => t(ALERT_LABELS[type]?.bn, ALERT_LABELS[type]?.en) || type;
  const maxH = 560;
  const overscan = 8;
  const start = Math.max(0, Math.floor(scrollTop / EVENT_ROW_H) - overscan);
  const visible = Math.ceil(maxH / EVENT_ROW_H) + overscan * 2;
  const end = Math.min(events.length, start + visible);
  const slice = events.slice(start, end);
  const virtualize = events.length > 40;

  return (
    <div className="events-page">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ইভেন্ট","Events")}</div>
          <div className="page-sub">{t("নিয়ম মূল্যায়নের আগে নরমালাইজড ডিটেকশন ইভেন্ট","Normalized detection events before rule evaluation")}</div>
        </div>
        <span className="grid-count">{events.length}</span>
      </div>
      {loading && <div className="empty-state">{t("লোড হচ্ছে…","Loading…")}</div>}
      {!loading && events.length === 0 && (
        <div className="empty-state"><span className="empty-icon"><IconEmpty /></span>{t("কোনো ইভেন্ট নেই","No events yet")}</div>
      )}
      {!loading && events.length > 0 && (
        <div
          className="events-table-wrap"
          style={virtualize ? { maxHeight: maxH, overflow: "auto" } : undefined}
          onScroll={virtualize ? (e => setScrollTop(e.target.scrollTop)) : undefined}
        >
          <table className="events-table">
            <thead>
              <tr>
                <th>{t("সময়","Time")}</th>
                <th>{t("ধরন","Type")}</th>
                <th>{t("ক্যামেরা","Camera")}</th>
                <th>{t("অবজেক্ট","Object")}</th>
                <th>{t("ট্র্যাক","Track")}</th>
                <th>{t("জোন","Zone")}</th>
                <th>{t("কনফিডেন্স","Conf.")}</th>
              </tr>
            </thead>
            <tbody>
              {virtualize && start > 0 && (
                <tr aria-hidden="true"><td colSpan={7} style={{ height: start * EVENT_ROW_H, padding: 0, border: 0 }} /></tr>
              )}
              {(virtualize ? slice : events).map(ev => (
                <tr key={ev.id}>
                  <td className="mono">{ev.occurred_at ? new Date(ev.occurred_at).toLocaleString() : "—"}</td>
                  <td>{typeLabel(ev.event_type)}</td>
                  <td className="mono">{ev.camera_id}</td>
                  <td>{ev.object_type || "—"}</td>
                  <td className="mono">{ev.track_id ?? "—"}</td>
                  <td>{ev.zone_type || ev.zone_id || "—"}</td>
                  <td className="mono">{ev.confidence != null ? `${(ev.confidence * 100).toFixed(0)}%` : "—"}</td>
                </tr>
              ))}
              {virtualize && end < events.length && (
                <tr aria-hidden="true"><td colSpan={7} style={{ height: (events.length - end) * EVENT_ROW_H, padding: 0, border: 0 }} /></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
