import { useState, useEffect, useCallback, useRef } from "react";
import { AlertPanel, CameraGrid, IncidentList, StatusBar } from "./components/index";
import { useAlerts, useWebSocket } from "./hooks/index";

const ALERT_SERVICE_URL = import.meta.env.VITE_ALERT_SERVICE_URL || "http://localhost:8004";
const WS_URL            = ALERT_SERVICE_URL.replace("http", "ws");

const NAV_ITEMS = [
  {
    key: "alerts",
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon">
        <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
      </svg>
    ),
    bn: "সতর্কতা",
    en: "Alerts",
    countKey: "pending",
  },
  {
    key: "cameras",
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon">
        <path d="M2 6a2 2 0 012-2h6a2 2 0 012 2v8a2 2 0 01-2 2H4a2 2 0 01-2-2V6zM14.553 7.106A1 1 0 0014 8v4a1 1 0 00.553.894l2 1A1 1 0 0018 13V7a1 1 0 00-1.447-.894l-2 1z" />
      </svg>
    ),
    bn: "ক্যামেরা",
    en: "Cameras",
  },
  {
    key: "incidents",
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon">
        <path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9z" />
        <path fillRule="evenodd" d="M4 5a2 2 0 012-2 3 3 0 003 3h2a3 3 0 003-3 2 2 0 012 2v11a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm3 4a1 1 0 000 2h.01a1 1 0 100-2H7zm3 0a1 1 0 000 2h3a1 1 0 100-2h-3zm-3 4a1 1 0 100 2h.01a1 1 0 100-2H7zm3 0a1 1 0 100 2h3a1 1 0 100-2h-3z" clipRule="evenodd" />
      </svg>
    ),
    bn: "ঘটনা",
    en: "Incidents",
    countKey: "open",
  },
  {
    key: "forensics",
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon">
        <path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd" />
      </svg>
    ),
    bn: "ফরেনসিক সার্চ",
    en: "Forensics",
  },
  {
    key: "drafting",
    icon: (
      <svg viewBox="0 0 20 20" fill="currentColor" className="nav-icon">
        <path d="M17.414 2.586a2 2 0 00-2.828 0L7 10.172V13h2.828l7.586-7.586a2 2 0 000-2.828z" />
        <path fillRule="evenodd" d="M2 6a2 2 0 012-2h4a1 1 0 010 2H4v10h10v-4a1 1 0 112 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" clipRule="evenodd" />
      </svg>
    ),
    bn: "জিডি / এফআইআর",
    en: "GD / FIR",
  },
];

export default function App() {
  const [language,     setLanguage]     = useState("bn");
  const [activeTab,    setActiveTab]    = useState("alerts");
  const [sidebarOpen,  setSidebarOpen]  = useState(false);
  const [officer]                       = useState({
    id:   "demo-officer-001",
    name: "Demo Officer",
    role: "operator",
  });

  const { alerts, incidents, acceptAlert, rejectAlert, escalateAlert, handleWsMessage } =
    useAlerts(ALERT_SERVICE_URL, officer.id);

  const sessionRef = useRef(null);
  if (!sessionRef.current) {
    sessionRef.current = `${officer.id}-${crypto.randomUUID?.() ?? Date.now()}`;
  }

  const { connected, lastMessage } =
    useWebSocket(`${WS_URL}/ws/${sessionRef.current}`);

  useEffect(() => {
    if (lastMessage) handleWsMessage(lastMessage);
  }, [lastMessage, handleWsMessage]);

  const t = (bn, en) => language === "bn" ? bn : en;

  const pendingCount = alerts.filter(a => a.status === "pending").length;
  const openCount    = incidents.filter(i => i.status === "open").length;

  const tabTitles = {
    alerts:    t("সতর্কতা", "Alerts"),
    cameras:   t("লাইভ ক্যামেরা", "Live Cameras"),
    incidents: t("ঘটনা তালিকা", "Incidents"),
    forensics: t("ভিডিও ফরেনসিক সার্চ", "Video Forensic Search"),
    drafting:  t("জিডি / এফআইআর ড্রাফটিং", "GD / FIR Drafting"),
  };

  const handleNavClick = (key) => {
    setActiveTab(key);
    setSidebarOpen(false);
  };

  return (
    <div className="app">
      {/* ── Sidebar ───────────────────────────── */}
      {sidebarOpen && (
        <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />
      )}

      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-logo">
          <div className="logo-shield">AI</div>
          <div className="logo-text">
            <div className="logo-name">
              {t("পুলিশ কমান্ড", "Police Command")}
            </div>
            <div className="logo-sub">
              {t("বাংলাদেশ পুলিশ", "Bangladesh Police")}
            </div>
          </div>
        </div>

        <nav className="sidebar-nav">
          <div className="nav-section-label">{t("মেনু", "Navigation")}</div>
          {NAV_ITEMS.map(item => {
            const count = item.countKey === "pending" ? pendingCount
                        : item.countKey === "open"    ? openCount
                        : 0;
            return (
              <button
                key={item.key}
                className={`nav-item ${activeTab === item.key ? "active" : ""}`}
                onClick={() => handleNavClick(item.key)}
              >
                {item.icon}
                {t(item.bn, item.en)}
                {count > 0 && <span className="nav-count">{count}</span>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className="officer-info">
            <div className="officer-avatar">
              {officer.name.charAt(0)}
            </div>
            <div>
              <div className="officer-name">{officer.name}</div>
              <div className="officer-role">{t("অপারেটর", "Operator")}</div>
            </div>
          </div>
        </div>
      </aside>

      {/* ── Main area ─────────────────────────── */}
      <div className="main-area">
        {/* ── Top bar ───────────────────────── */}
        <header className="topbar">
          <button
            className="hamburger"
            onClick={() => setSidebarOpen(o => !o)}
            aria-label="Toggle menu"
          >
            ☰
          </button>

          <div className="topbar-title">{tabTitles[activeTab]}</div>

          <div className="topbar-right">
            <div className={`ws-indicator ${connected ? "connected" : "disconnected"}`}>
              <span className="ws-dot" />
              {connected ? t("সংযুক্ত", "Live") : t("বিচ্ছিন্ন", "Offline")}
            </div>

            <button
              className="lang-toggle"
              onClick={() => setLanguage(l => l === "bn" ? "en" : "bn")}
            >
              {language === "bn" ? "EN" : "বাং"}
            </button>
          </div>
        </header>

        {/* ── Content ───────────────────────── */}
        <main className="main-content">
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
            <IncidentList incidents={incidents} language={language} />
          )}
          {activeTab === "forensics" && (
            <ForensicSearch language={language} apiUrl={ALERT_SERVICE_URL} />
          )}
          {activeTab === "drafting" && (
            <DraftingTool language={language} officer={officer} />
          )}
        </main>

        {/* ── Status bar ────────────────────── */}
        <StatusBar
          alertCount={alerts.length}
          pendingCount={pendingCount}
          language={language}
        />
      </div>
    </div>
  );
}


// ── Forensic search panel ──────────────────────
function ForensicSearch({ language, apiUrl }) {
  const [query,   setQuery]   = useState("");
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);

  const t = (bn, en) => language === "bn" ? bn : en;

  const search = async () => {
    if (!query.trim()) return;
    setLoading(true);
    try {
      const resp = await fetch(`${apiUrl}/forensics/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, limit: 20 }),
      });
      const data = await resp.json();
      setResults(data.results || []);
    } catch (err) {
      console.error("Forensic search failed:", err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="forensics-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ভিডিও ফরেনসিক সার্চ", "Video Forensic Search")}</div>
          <div className="page-sub">{t("বাংলা বা ইংরেজিতে ভিডিও ইভেন্ট খুঁজুন", "Search video events in Bangla or English")}</div>
        </div>
      </div>

      <div className="search-bar">
        <svg viewBox="0 0 20 20" fill="currentColor" style={{ width: 18, height: 18, color: "var(--text-subtle)", flexShrink: 0 }}>
          <path fillRule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clipRule="evenodd" />
        </svg>
        <input
          className="search-input"
          placeholder={t(
            "যেমন: রাত ৮টার পরে সাদা মাইক্রোবাস, গেট ক্যামেরা…",
            "e.g. white microbus after 8pm gate camera…"
          )}
          value={query}
          onChange={e => setQuery(e.target.value)}
          onKeyDown={e => e.key === "Enter" && search()}
        />
        <button className="search-btn" onClick={search} disabled={loading}>
          {loading ? t("খোঁজা হচ্ছে…", "Searching…") : t("খুঁজুন", "Search")}
        </button>
      </div>

      <div className="search-results">
        {results.map((r, i) => (
          <div key={i} className="result-card">
            <div className="result-header">
              <span className="result-cam">{r.camera_id}</span>
              <span className="result-time">{r.timestamp}</span>
              <span className="result-score">
                {t("মিল", "Match")}: {(r.similarity * 100).toFixed(0)}%
              </span>
            </div>
            <div className="result-desc">{r.description}</div>
            {r.snapshot_path && (
              <button className="btn-secondary">
                {t("ভিডিও দেখুন", "View Footage")}
              </button>
            )}
          </div>
        ))}
        {results.length === 0 && query && !loading && (
          <div className="empty-state">
            <span className="empty-icon">🔍</span>
            {t("কোনো ফলাফল পাওয়া যায়নি", "No results found")}
          </div>
        )}
        {results.length === 0 && !query && (
          <div className="empty-state">
            <span className="empty-icon">🎥</span>
            {t("সার্চ করতে উপরের বক্সে কিছু লিখুন", "Enter a search query above to find footage")}
          </div>
        )}
      </div>
    </div>
  );
}


// ── GD/FIR Drafting panel ─────────────────────
function DraftingTool({ language, officer }) {
  const [notes,     setNotes]     = useState("");
  const [draftType, setDraftType] = useState("GD");
  const [draft,     setDraft]     = useState(null);
  const [loading,   setLoading]   = useState(false);
  const [status,    setStatus]    = useState("");

  const DRAFTING_URL = (import.meta.env.VITE_ALERT_SERVICE_URL || "http://localhost:8004")
    .replace(":8004", ":8006");

  const t = (bn, en) => language === "bn" ? bn : en;

  const generateDraft = async () => {
    if (!notes.trim()) return;
    setLoading(true);
    setStatus("");
    try {
      const resp = await fetch(`${DRAFTING_URL}/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_type: draftType,
          raw_notes:  notes,
          language:   language,
          officer_id: officer.id,
        }),
      });
      const data = await resp.json();
      setDraft(data);
    } catch (err) {
      setStatus(t("ত্রুটি হয়েছে। আবার চেষ্টা করুন।", "Error generating draft. Please retry."));
    } finally {
      setLoading(false);
    }
  };

  const approveDraft = async () => {
    if (!draft) return;
    const editedText = document.getElementById("draft-text-edit")?.value;
    await fetch(`${DRAFTING_URL}/draft/${draft.draft_id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        officer_id: officer.id,
        approved:   true,
        edits:      editedText || null,
      }),
    });
    setStatus(t("ড্রাফট অনুমোদিত হয়েছে ✓", "Draft approved ✓"));
    setDraft(prev => ({ ...prev, status: "approved" }));
  };

  return (
    <div className="drafting-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("জিডি / এফআইআর ড্রাফটিং", "GD / FIR Drafting Assistant")}</div>
          <div className="page-sub">{t("কথ্য নোট থেকে এআই দিয়ে ড্রাফট তৈরি", "AI-assisted draft from spoken or written officer notes")}</div>
        </div>
      </div>

      <div className="draft-type-selector">
        {["GD", "FIR"].map(type => (
          <button
            key={type}
            className={`draft-type-btn ${draftType === type ? "active" : ""}`}
            onClick={() => setDraftType(type)}
          >
            {type}
          </button>
        ))}
      </div>

      <textarea
        className="notes-input"
        placeholder={t(
          "অফিসারের নোট এখানে লিখুন — কথ্য বা লিখিত যেকোনো ভাষায়…",
          "Enter officer notes here — spoken or written, any language…"
        )}
        value={notes}
        onChange={e => setNotes(e.target.value)}
        rows={6}
      />

      <button className="btn-primary" onClick={generateDraft} disabled={loading}>
        {loading
          ? t("ড্রাফট তৈরি হচ্ছে…", "Generating draft…")
          : t("AI দিয়ে ড্রাফট তৈরি করুন", "Generate draft with AI")}
      </button>

      {draft && (
        <div className="draft-result">
          {draft.missing_fields?.length > 0 && (
            <div className="missing-fields-warning">
              ⚠ {t("অনুপস্থিত তথ্য", "Missing fields")}:{" "}
              {draft.missing_fields.join(", ")}
            </div>
          )}

          <div className="draft-label">
            {t("ড্রাফট — পর্যালোচনা করুন এবং প্রয়োজনে সম্পাদনা করুন",
               "Draft — Review and edit if needed")}
          </div>
          <textarea
            id="draft-text-edit"
            className="draft-text"
            defaultValue={draft.draft_text}
            rows={12}
          />

          {draft.entities_extracted && (
            <div className="entities-section">
              <div className="entities-title">
                {t("চিহ্নিত তথ্য", "Extracted entities")}
              </div>
              {Object.entries(draft.entities_extracted)
                .filter(([, v]) => v?.length > 0)
                .map(([k, v]) => (
                  <div key={k} className="entity-row">
                    <span className="entity-key">{k}:</span>
                    <span className="entity-vals">{v.join(", ")}</span>
                  </div>
                ))}
            </div>
          )}

          {draft.status !== "approved" && (
            <div className="approval-section">
              <div className="approval-notice">
                ⚠ {t(
                  "এআই শুধু সহায়তা করেছে। অফিসারের অনুমোদন বাধ্যতামূলক।",
                  "AI assisted only. Officer approval is mandatory before submission."
                )}
              </div>
              <div className="approval-actions">
                <button className="btn-approve" onClick={approveDraft}>
                  {t("অনুমোদন করুন ✓", "Approve ✓")}
                </button>
                <button className="btn-reject">
                  {t("ফেরত পাঠান", "Return for revision")}
                </button>
              </div>
            </div>
          )}

          {status && <div className="status-msg">{status}</div>}
        </div>
      )}
    </div>
  );
}
