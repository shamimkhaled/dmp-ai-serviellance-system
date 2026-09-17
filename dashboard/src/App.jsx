import { useState, useEffect, useRef } from "react";
import {
  AlertPanel, CameraGrid, StatusBar, CommandCenter, AnalyticsPage,
} from "./components/index";
import { LivePage } from "./pages/Live";
import { EventsPage } from "./pages/Events";
import { InvestigationPage } from "./pages/Investigation";
import { AdminHub } from "./pages/AdminHub";
import { useAlerts, useWebSocket } from "./hooks/index";
import {
  ALERT_SERVICE_URL, VIDEO_INGEST_URL, TRAFFIC_AI_URL,
} from "./lib/constants";
import { getAccessToken, logout } from "./lib/auth";
import {
  IconOverview, IconLive, IconAlerts, IconCameras, IconEvents,
  IconInvestigate, IconAnalytics, IconAdmin, IconMenu,
} from "./lib/icons";

const WS_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`
    : "ws://localhost:3000";

const NAV_ALLOW = {
  viewer:       new Set(["overview", "live", "alerts", "events", "analytics"]),
  operator:     new Set(["overview", "live", "alerts", "cameras", "events", "investigation", "analytics"]),
  investigator: new Set(["overview", "live", "alerts", "events", "investigation", "analytics", "admin"]),
  admin:        new Set(["overview", "live", "alerts", "cameras", "events", "investigation", "analytics", "admin"]),
};

const NAV_ITEMS = [
  { key: "overview",      icon: <IconOverview className="nav-icon" />,      bn: "ওভারভিউ",       en: "Overview" },
  { key: "live",          icon: <IconLive className="nav-icon" />,          bn: "লাইভ",          en: "Live" },
  { key: "alerts",        icon: <IconAlerts className="nav-icon" />,        bn: "সতর্কতা",       en: "Alerts", countKey: "pending" },
  { key: "cameras",       icon: <IconCameras className="nav-icon" />,       bn: "ক্যামেরা",      en: "Cameras" },
  { key: "events",        icon: <IconEvents className="nav-icon" />,        bn: "ইভেন্ট",        en: "Events" },
  { key: "investigation", icon: <IconInvestigate className="nav-icon" />,   bn: "তদন্ত",         en: "Investigation", countKey: "open" },
  { key: "analytics",     icon: <IconAnalytics className="nav-icon" />,     bn: "বিশ্লেষণ",      en: "Analytics" },
  { key: "admin",         icon: <IconAdmin className="nav-icon" />,         bn: "প্রশাসন",       en: "Administration" },
];

const TAB_TITLES = {
  overview:      { bn: "ওভারভিউ",              en: "Overview" },
  live:          { bn: "লাইভ ওয়াল",           en: "Live" },
  alerts:        { bn: "সতর্কতা",              en: "Alerts" },
  cameras:       { bn: "ক্যামেরা ব্যবস্থাপনা", en: "Cameras" },
  events:        { bn: "ইভেন্ট",               en: "Events" },
  investigation: { bn: "তদন্ত",                en: "Investigation" },
  analytics:     { bn: "বিশ্লেষণ",             en: "Analytics" },
  admin:         { bn: "প্রশাসন",              en: "Administration" },
};

function useCameraList(ingestUrl) {
  const [cameras, setCameras] = useState([]);
  useEffect(() => {
    const load = () =>
      fetch(`${ingestUrl}/cameras`)
        .then(r => r.json())
        .then((d) => setCameras(Array.isArray(d) ? d : []))
        .catch(() => {});
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [ingestUrl]);
  return cameras;
}

export default function App() {
  const [language,    setLanguage]    = useState("en");
  const [activeTab,   setActiveTab]   = useState("overview");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [me,          setMe]          = useState(null);

  useEffect(() => {
    fetch(`${ALERT_SERVICE_URL}/me`)
      .then(r => (r.ok ? r.json() : null))
      .then(setMe)
      .catch(() => setMe(null));
  }, []);

  const officer = {
    id: me?.officer_id || me?.sub || "operator-001",
    name: me?.name || me?.username || "Operator",
    role: me?.role || "viewer",
  };
  const allowedNav = NAV_ALLOW[officer.role] || NAV_ALLOW.viewer;

  const { alerts, incidents, acceptAlert, rejectAlert, escalateAlert,
          investigateAlert, resolveAlert, assignAlert, handleWsMessage } =
    useAlerts(ALERT_SERVICE_URL, officer.id);

  const sessionRef = useRef(null);
  if (!sessionRef.current) sessionRef.current = `${officer.id}-${crypto.randomUUID?.() ?? Date.now()}`;

  const token = getAccessToken();
  const wsUrl = token
    ? `${WS_URL}/ws/${sessionRef.current}?access_token=${encodeURIComponent(token)}`
    : `${WS_URL}/ws/${sessionRef.current}`;
  const { connected, lastMessage } = useWebSocket(wsUrl);
  useEffect(() => { if (lastMessage) handleWsMessage(lastMessage); }, [lastMessage, handleWsMessage]);

  const cameras = useCameraList(VIDEO_INGEST_URL);

  const t = (bn, en) => language === "bn" ? bn : en;

  const pendingCount = alerts.filter(a => a.status === "pending").length;
  const openCount    = incidents.filter(i => i.status === "open").length;

  const handleNavClick = (key) => { setActiveTab(key); setSidebarOpen(false); };
  const handleAlertSelect = () => setActiveTab("alerts");

  useEffect(() => {
    const allow = NAV_ALLOW[officer.role] || NAV_ALLOW.viewer;
    if (!allow.has(activeTab)) setActiveTab("overview");
  }, [officer.role, activeTab]);

  return (
    <div className="app">
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />}

      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-logo">
          <div className="logo-shield">AI</div>
          <div className="logo-text">
            <div className="logo-name">{t("ভিএমএস ইন্টেলিজেন্স","VMS Intelligence")}</div>
            <div className="logo-sub">{t("অপারেশন প্ল্যাটফর্ম","Operations platform")}</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-label">{t("মেনু","Navigation")}</div>
          {NAV_ITEMS.filter(item => allowedNav.has(item.key)).map(item => {
            const count = item.countKey === "pending" ? pendingCount
                        : item.countKey === "open"    ? openCount
                        : 0;
            return (
              <button key={item.key}
                className={`nav-item ${activeTab === item.key ? "active" : ""}`}
                onClick={() => handleNavClick(item.key)}>
                {item.icon}
                {t(item.bn, item.en)}
                {count > 0 && <span className="nav-count">{count}</span>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className="officer-info">
            <div className="officer-avatar">{officer.name.charAt(0)}</div>
            <div>
              <div className="officer-name">{officer.name}</div>
              <div className="officer-role">{officer.role}</div>
            </div>
          </div>
          <button type="button" className="btn-sm" style={{ marginTop: "0.5rem", width: "100%" }} onClick={logout}>
            {t("সাইন আউট","Sign out")}
          </button>
        </div>
      </aside>

      <div className="main-area">
        <header className="topbar">
          <button className="hamburger" onClick={() => setSidebarOpen(o => !o)} aria-label={t("মেনু","Toggle menu")}>
            <IconMenu />
          </button>
          <div className="topbar-title">{t(TAB_TITLES[activeTab]?.bn, TAB_TITLES[activeTab]?.en)}</div>
          <div className="topbar-right">
            <div className={`ws-indicator ${connected ? "connected" : "disconnected"}`}>
              <span className="ws-dot"/>
              {connected ? t("সংযুক্ত","Live") : t("বিচ্ছিন্ন","Offline")}
            </div>
            <button className="lang-toggle" onClick={() => setLanguage(l => l === "bn" ? "en" : "bn")}>
              {language === "bn" ? "EN" : "বাং"}
            </button>
          </div>
        </header>

        <main className="main-content">
          {activeTab === "overview" && (
            <CommandCenter
              cameras={cameras}
              alerts={alerts}
              language={language}
              onAlertSelect={handleAlertSelect}
            />
          )}
          {activeTab === "live" && (
            <LivePage cameras={cameras} language={language} />
          )}
          {activeTab === "alerts" && (
            <AlertPanel
              alerts={alerts}
              language={language}
              apiUrl={ALERT_SERVICE_URL}
              officerId={officer.id}
              canAct={officer.role !== "viewer"}
              onAccept={acceptAlert}
              onReject={rejectAlert}
              onEscalate={escalateAlert}
              onInvestigate={investigateAlert}
              onResolve={resolveAlert}
              onAssign={assignAlert}
            />
          )}
          {activeTab === "cameras" && (
            <CameraGrid language={language} canManage={officer.role === "admin"} />
          )}
          {activeTab === "events" && (
            <EventsPage language={language} />
          )}
          {activeTab === "investigation" && (
            <InvestigationPage
              incidents={incidents}
              alerts={alerts}
              language={language}
              apiUrl={ALERT_SERVICE_URL}
            />
          )}
          {activeTab === "analytics" && (
            <AnalyticsPage language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "admin" && (
            <AdminHub
              language={language}
              officer={officer}
              role={officer.role}
              alertApiUrl={ALERT_SERVICE_URL}
              videoIngestUrl={VIDEO_INGEST_URL}
              trafficAiUrl={TRAFFIC_AI_URL}
            />
          )}
        </main>

        <StatusBar alertCount={alerts.length} pendingCount={pendingCount} language={language} />
      </div>
    </div>
  );
}
