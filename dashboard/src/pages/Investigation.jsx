import { useEffect, useMemo, useState } from "react";
import { IncidentList } from "../components/index";
import { ForensicSearch } from "../components/ForensicSearch";
import { ALERT_SERVICE_URL, ALERT_LABELS, SEVERITY } from "../lib/constants";
import { IconEmpty } from "../lib/icons";

function Timeline({ alerts, language }) {
  const [events, setEvents] = useState([]);
  const t = (bn, en) => language === "bn" ? bn : en;

  useEffect(() => {
    let cancelled = false;
    fetch(`${ALERT_SERVICE_URL}/events?limit=200`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setEvents(Array.isArray(d) ? d : []); })
      .catch(() => { if (!cancelled) setEvents([]); });
    return () => { cancelled = true; };
  }, []);

  const items = useMemo(() => {
    const alertItems = (alerts || []).map(a => ({
      kind: "alert",
      id: a.alert_id,
      time: a.timestamp || a.created_at,
      title: t(ALERT_LABELS[a.alert_type]?.bn, ALERT_LABELS[a.alert_type]?.en) || a.alert_type,
      camera: a.camera_id,
      severity: a.severity,
      status: a.status,
    }));
    const eventItems = events.map(e => ({
      kind: "event",
      id: e.id,
      time: e.occurred_at,
      title: t(ALERT_LABELS[e.event_type]?.bn, ALERT_LABELS[e.event_type]?.en) || e.event_type,
      camera: e.camera_id,
      object: e.object_type,
    }));
    return [...alertItems, ...eventItems].sort((a, b) => new Date(b.time || 0) - new Date(a.time || 0));
  }, [alerts, events, language]);

  return (
    <div className="timeline">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("টাইমলাইন","Timeline")}</div>
          <div className="page-sub">{t("সতর্কতা ও ইভেন্ট সময় অনুসারে","Alerts and events by time")}</div>
        </div>
      </div>
      {items.length === 0 && (
        <div className="empty-state"><span className="empty-icon"><IconEmpty /></span>{t("কোনো এন্ট্রি নেই","No timeline entries")}</div>
      )}
      <ol className="timeline-list">
        {items.slice(0, 200).map(item => (
          <li key={`${item.kind}-${item.id}`} className={`timeline-item kind-${item.kind}`}>
            <span className="timeline-kind">{item.kind === "alert" ? t("সতর্কতা","Alert") : t("ইভেন্ট","Event")}</span>
            <span className="timeline-title">{item.title}</span>
            <span className="mono timeline-cam">{item.camera}</span>
            {item.severity != null && (
              <span className="severity-badge" style={{ background: SEVERITY[item.severity]?.color }}>
                {t(SEVERITY[item.severity]?.bn, SEVERITY[item.severity]?.en)}
              </span>
            )}
            <span className="mono timeline-time">{item.time ? new Date(item.time).toLocaleString() : "—"}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function InvestigationPage({ incidents, alerts, language, apiUrl }) {
  const [tab, setTab] = useState("cases");
  const t = (bn, en) => language === "bn" ? bn : en;
  const tabs = [
    ["cases", t("কেস","Cases")],
    ["timeline", t("টাইমলাইন","Timeline")],
    ["search", t("সার্চ","Search")],
  ];

  return (
    <div className="investigation-page">
      <div className="sub-tabs" role="tablist">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" role="tab" aria-selected={tab === key}
            className={`sub-tab ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>
      {tab === "cases" && <IncidentList incidents={incidents} language={language} apiUrl={apiUrl} />}
      {tab === "timeline" && <Timeline alerts={alerts} language={language} />}
      {tab === "search" && <ForensicSearch language={language} apiUrl={apiUrl} />}
    </div>
  );
}
