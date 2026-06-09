import { useState, useEffect, useRef } from "react";
import {
  AlertPanel, CameraGrid, IncidentList, StatusBar,
  CommandCenter, AnalyticsPage, EvidencePage, AdminPage,
} from "./components/index";
import { useAlerts, useWebSocket } from "./hooks/index";

const ALERT_SERVICE_URL  = import.meta.env.VITE_ALERT_SERVICE_URL  || "http://localhost:8004";
const VIDEO_INGEST_URL   = import.meta.env.VITE_VIDEO_INGEST_URL   || "http://localhost:8001";
const TRAFFIC_AI_URL     = import.meta.env.VITE_TRAFFIC_AI_URL     || "http://localhost:8002";
const WS_URL             = ALERT_SERVICE_URL.replace("http", "ws");

// ── Navigation items ──────────────────────────────────────────────────────────
const NAV_ITEMS = [
  {
    key:  "command",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path d="M2 4a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H3a1 1 0 01-1-1V4zM2 9a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H3a1 1 0 01-1-1V9zm11-1a1 1 0 00-1 1v6a1 1 0 001 1h3a1 1 0 001-1V9a1 1 0 00-1-1h-3z"/></svg>,
    bn: "কমান্ড সেন্টার", en: "Command Center",
  },
  {
    key:  "alerts",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd"/></svg>,
    bn: "সতর্কতা", en: "Alerts", countKey: "pending",
  },
  {
    key:  "cameras",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path d="M2 6a2 2 0 012-2h6a2 2 0 012 2v8a2 2 0 01-2 2H4a2 2 0 01-2-2V6zM14.553 7.106A1 1 0 0014 8v4a1 1 0 00.553.894l2 1A1 1 0 0018 13V7a1 1 0 00-1.447-.894l-2 1z"/></svg>,
    bn: "ক্যামেরা", en: "Cameras",
  },
  {
    key:  "incidents",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z"/><path fillRule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clipRule="evenodd"/></svg>,
    bn: "ঘটনা", en: "Incidents", countKey: "open",
  },
  {
    key:  "analytics",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"/></svg>,
    bn: "বিশ্লেষণ", en: "Analytics",
  },
  {
    key:  "evidence",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path fillRule="evenodd" d="M4 3a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V5a2 2 0 00-2-2H4zm12 12H4l4-8 3 6 2-4 3 6z" clipRule="evenodd"/></svg>,
    bn: "প্রমাণ", en: "Evidence",
  },
  {
    key:  "forensics",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd"/></svg>,
    bn: "ফরেনসিক", en: "Forensics",
  },
  {
    key:  "drafting",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path d="M17.414 2.586a2 2 0 00-2.828 0L7 10.172V13h2.828l7.586-7.586a2 2 0 000-2.828z"/><path fillRule="evenodd" d="M2 6a2 2 0 012-2h4a1 1 0 010 2H4v10h10v-4a1 1 0 112 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" clipRule="evenodd"/></svg>,
    bn: "জিডি / এফআইআর", en: "GD / FIR",
  },
  {
    key:  "admin",
    icon: <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon"><path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd"/></svg>,
    bn: "অ্যাডমিন", en: "Admin",
  },
];

const TAB_TITLES = {
  command:   { bn: "কমান্ড সেন্টার",                en: "Command Center" },
  alerts:    { bn: "সতর্কতা",                        en: "Alerts" },
  cameras:   { bn: "লাইভ ক্যামেরা",                  en: "Live Cameras" },
  incidents: { bn: "ঘটনা তালিকা",                    en: "Incidents" },
  analytics: { bn: "ট্র্যাফিক বিশ্লেষণ",            en: "Traffic Analytics" },
  evidence:  { bn: "প্রমাণ ব্যবস্থাপনা",             en: "Evidence Management" },
  forensics: { bn: "ভিডিও ফরেনসিক সার্চ",            en: "Video Forensic Search" },
  drafting:  { bn: "জিডি / এফআইআর ড্রাফটিং",        en: "GD / FIR Drafting" },
  admin:     { bn: "সিস্টেম অ্যাডমিন",               en: "System Admin" },
};

// ── Camera list fetcher (needed for Command Center) ───────────────────────────
function useCameraList(ingestUrl) {
  const [cameras, setCameras] = useState([]);
  useEffect(() => {
    const load = () =>
      fetch(`${ingestUrl}/cameras`).then(r => r.json()).then(setCameras).catch(() => {});
    load();
    const iv = setInterval(load, 15000);
    return () => clearInterval(iv);
  }, [ingestUrl]);
  return cameras;
}

// ── App ───────────────────────────────────────────────────────────────────────
export default function App() {
  const [language,    setLanguage]    = useState("bn");
  const [activeTab,   setActiveTab]   = useState("command");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [officer]                     = useState({ id: "demo-officer-001", name: "Demo Officer", role: "operator" });

  const { alerts, incidents, acceptAlert, rejectAlert, escalateAlert, handleWsMessage } =
    useAlerts(ALERT_SERVICE_URL, officer.id);

  const sessionRef = useRef(null);
  if (!sessionRef.current) sessionRef.current = `${officer.id}-${crypto.randomUUID?.() ?? Date.now()}`;

  const { connected, lastMessage } = useWebSocket(`${WS_URL}/ws/${sessionRef.current}`);
  useEffect(() => { if (lastMessage) handleWsMessage(lastMessage); }, [lastMessage, handleWsMessage]);

  const cameras = useCameraList(VIDEO_INGEST_URL);

  const t = (bn, en) => language === "bn" ? bn : en;

  const pendingCount = alerts.filter(a => a.status === "pending").length;
  const openCount    = incidents.filter(i => i.status === "open").length;

  const handleNavClick = (key) => { setActiveTab(key); setSidebarOpen(false); };

  // When an alert is clicked in Command Center, jump to Alerts tab
  const handleAlertSelect = () => setActiveTab("alerts");

  return (
    <div className="app">
      {sidebarOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />}

      {/* ── Sidebar ─────────────────────────────── */}
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-logo">
          <div className="logo-shield">AI</div>
          <div className="logo-text">
            <div className="logo-name">{t("পুলিশ কমান্ড","Police Command")}</div>
            <div className="logo-sub">{t("বাংলাদেশ পুলিশ","Bangladesh Police")}</div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-label">{t("মেনু","Navigation")}</div>
          {NAV_ITEMS.map(item => {
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
              <div className="officer-role">{t("অপারেটর","Operator")}</div>
            </div>
          </div>
        </div>
      </aside>

      {/* ── Main area ───────────────────────────── */}
      <div className="main-area">
        {/* Topbar */}
        <header className="topbar">
          <button className="hamburger" onClick={() => setSidebarOpen(o => !o)} aria-label="Toggle menu">☰</button>
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

        {/* Content */}
        <main className="main-content">
          {activeTab === "command" && (
            <CommandCenter
              cameras={cameras}
              alerts={alerts}
              language={language}
              onAlertSelect={handleAlertSelect}
            />
          )}
          {activeTab === "alerts" && (
            <AlertPanel
              alerts={alerts}
              language={language}
              onAccept={acceptAlert}
              onReject={rejectAlert}
              onEscalate={escalateAlert}
            />
          )}
          {activeTab === "cameras" && (
            <CameraGrid language={language} />
          )}
          {activeTab === "incidents" && (
            <IncidentList incidents={incidents} language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "analytics" && (
            <AnalyticsPage language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "evidence" && (
            <EvidencePage language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "forensics" && (
            <ForensicSearch language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "drafting" && (
            <DraftingTool language={language} officer={officer} />
          )}
          {activeTab === "admin" && (
            <AdminPage
              language={language}
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

// ── Forensic Search ───────────────────────────────────────────────────────────
function ForensicSearch({ language, apiUrl }) {
  const [query,   setQuery]   = useState("");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  const t = (bn, en) => language === "bn" ? bn : en;

  const search = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      const r = await fetch(`${apiUrl}/forensics/search`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 20 }),
      });
      const d = await r.json();
      setResults(d.results || []);
    } catch {} finally { setLoading(false); }
  };

  return (
    <div className="forensics-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ভিডিও ফরেনসিক সার্চ","Video Forensic Search")}</div>
          <div className="page-sub">{t("বাংলা বা ইংরেজিতে ভিডিও ইভেন্ট খুঁজুন","Search video events in Bangla or English")}</div>
        </div>
      </div>
      <div className="search-bar">
        <svg viewBox="0 0 20 20" fill="currentColor" style={{ width: 18, height: 18, color: "var(--text-subtle)", flexShrink: 0 }}>
          <path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd"/>
        </svg>
        <input className="search-input"
          placeholder={t("যেমন: রাত ৮টার পরে সাদা মাইক্রোবাস…","e.g. white microbus after 8pm gate camera…")}
          value={query} onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search()} />
        <button className="search-btn" onClick={search} disabled={loading}>
          {loading ? t("খোঁজা হচ্ছে…","Searching…") : t("খুঁজুন","Search")}
        </button>
      </div>
      <div className="search-results">
        {results.map((r, i) => (
          <div key={i} className="result-card">
            <div className="result-header">
              <span className="result-cam">{r.camera_id}</span>
              <span className="result-time">{r.timestamp}</span>
              <span className="result-score">{t("মিল","Match")}: {(r.similarity*100).toFixed(0)}%</span>
            </div>
            <div className="result-desc">{r.description}</div>
          </div>
        ))}
        {results.length === 0 && query && !loading && (
          <div className="empty-state"><span className="empty-icon">🔍</span>{t("কোনো ফলাফল পাওয়া যায়নি","No results found")}</div>
        )}
        {results.length === 0 && !query && (
          <div className="empty-state"><span className="empty-icon">🎥</span>{t("সার্চ করতে উপরের বক্সে কিছু লিখুন","Enter a search query above to find footage")}</div>
        )}
      </div>
    </div>
  );
}

// ── GD/FIR Drafting ───────────────────────────────────────────────────────────
function DraftingTool({ language, officer }) {
  const [notes,     setNotes]     = useState("");
  const [draftType, setDraftType] = useState("GD");
  const [draft,     setDraft]     = useState(null);
  const [loading,   setLoading]   = useState(false);
  const [status,    setStatus]    = useState("");
  const DRAFTING_URL = (import.meta.env.VITE_ALERT_SERVICE_URL || "http://localhost:8004").replace(":8004",":8006");
  const t = (bn, en) => language === "bn" ? bn : en;

  const generateDraft = async () => {
    if (!notes.trim()) return;
    setLoading(true); setStatus("");
    try {
      const r = await fetch(`${DRAFTING_URL}/draft`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_type: draftType, raw_notes: notes, language, officer_id: officer.id }),
      });
      setDraft(await r.json());
    } catch { setStatus(t("ত্রুটি হয়েছে। আবার চেষ্টা করুন।","Error generating draft. Please retry.")); }
    finally { setLoading(false); }
  };

  const approveDraft = async () => {
    if (!draft) return;
    const editedText = document.getElementById("draft-text-edit")?.value;
    await fetch(`${DRAFTING_URL}/draft/${draft.draft_id}/approve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ officer_id: officer.id, approved: true, edits: editedText || null }),
    });
    setStatus(t("ড্রাফট অনুমোদিত হয়েছে ✓","Draft approved ✓"));
    setDraft(prev => ({ ...prev, status: "approved" }));
  };

  return (
    <div className="drafting-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("জিডি / এফআইআর ড্রাফটিং","GD / FIR Drafting Assistant")}</div>
          <div className="page-sub">{t("কথ্য নোট থেকে এআই দিয়ে ড্রাফট তৈরি","AI-assisted draft from spoken or written officer notes")}</div>
        </div>
      </div>
      <div className="draft-type-selector">
        {["GD","FIR"].map(type => (
          <button key={type} className={`draft-type-btn ${draftType === type ? "active" : ""}`} onClick={() => setDraftType(type)}>{type}</button>
        ))}
      </div>
      <textarea className="notes-input"
        placeholder={t("অফিসারের নোট এখানে লিখুন…","Enter officer notes here…")}
        value={notes} onChange={e => setNotes(e.target.value)} rows={6} />
      <button className="btn-primary" onClick={generateDraft} disabled={loading}>
        {loading ? t("ড্রাফট তৈরি হচ্ছে…","Generating draft…") : t("AI দিয়ে ড্রাফট তৈরি করুন","Generate draft with AI")}
      </button>
      {draft && (
        <div className="draft-result">
          {draft.missing_fields?.length > 0 && (
            <div className="missing-fields-warning">⚠ {t("অনুপস্থিত তথ্য","Missing fields")}: {draft.missing_fields.join(", ")}</div>
          )}
          <div className="draft-label">{t("ড্রাফট — পর্যালোচনা করুন","Draft — Review and edit if needed")}</div>
          <textarea id="draft-text-edit" className="draft-text" defaultValue={draft.draft_text} rows={12} />
          {draft.entities_extracted && (
            <div className="entities-section">
              <div className="entities-title">{t("চিহ্নিত তথ্য","Extracted entities")}</div>
              {Object.entries(draft.entities_extracted).filter(([,v]) => v?.length > 0).map(([k,v]) => (
                <div key={k} className="entity-row">
                  <span className="entity-key">{k}:</span>
                  <span className="entity-vals">{v.join(", ")}</span>
                </div>
              ))}
            </div>
          )}
          {draft.status !== "approved" && (
            <div className="approval-section">
              <div className="approval-notice">⚠ {t("এআই শুধু সহায়তা করেছে। অফিসারের অনুমোদন বাধ্যতামূলক।","AI assisted only. Officer approval is mandatory before submission.")}</div>
              <div className="approval-actions">
                <button className="btn-approve" onClick={approveDraft}>{t("অনুমোদন করুন ✓","Approve ✓")}</button>
                <button className="btn-reject">{t("ফেরত পাঠান","Return for revision")}</button>
              </div>
            </div>
          )}
          {status && <div className="status-msg">{status}</div>}
        </div>
      )}
    </div>
  );
}
