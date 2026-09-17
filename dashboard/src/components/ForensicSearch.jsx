import { useState } from "react";
import { IconSearch, IconEmpty } from "../lib/icons";

export function ForensicSearch({ language, apiUrl }) {
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
          <div className="page-title">{t("ভিডিও ফরেনসিক সার্চ","Video forensic search")}</div>
          <div className="page-sub">{t("বাংলা বা ইংরেজিতে ভিডিও ইভেন্ট খুঁজুন","Search video events in Bangla or English")}</div>
        </div>
      </div>
      <div className="search-bar">
        <IconSearch />
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
          <div className="empty-state"><span className="empty-icon"><IconEmpty /></span>{t("কোনো ফলাফল পাওয়া যায়নি","No results found")}</div>
        )}
        {results.length === 0 && !query && (
          <div className="empty-state"><span className="empty-icon"><IconSearch /></span>{t("সার্চ করতে উপরের বক্সে কিছু লিখুন","Enter a search query above to find footage")}</div>
        )}
      </div>
    </div>
  );
}
