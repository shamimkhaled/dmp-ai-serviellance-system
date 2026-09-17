import { useState } from "react";
import { ALERT_SERVICE_URL } from "../lib/constants";

export function DraftingTool({ language, officer }) {
  const [notes,     setNotes]     = useState("");
  const [draftType, setDraftType] = useState("GD");
  const [draft,     setDraft]     = useState(null);
  const [loading,   setLoading]   = useState(false);
  const [status,    setStatus]    = useState("");
  const DRAFTING_URL = import.meta.env.VITE_DRAFTING_URL || ALERT_SERVICE_URL;
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
    setStatus(t("ড্রাফট অনুমোদিত হয়েছে","Draft approved"));
    setDraft(prev => ({ ...prev, status: "approved" }));
  };

  return (
    <div className="drafting-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("জিডি / এফআইআর ড্রাফটিং","GD / FIR drafting")}</div>
          <div className="page-sub">{t("পুলিশ ইন্ডাস্ট্রি প্যাক — কথ্য নোট থেকে এআই ড্রাফট","Police industry pack — AI-assisted draft from officer notes")}</div>
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
            <div className="missing-fields-warning">{t("অনুপস্থিত তথ্য","Missing fields")}: {draft.missing_fields.join(", ")}</div>
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
              <div className="approval-notice">{t("এআই শুধু সহায়তা করেছে। অপারেটরের অনুমোদন বাধ্যতামূলক।","AI assisted only. Operator approval is mandatory before submission.")}</div>
              <div className="approval-actions">
                <button className="btn-approve" onClick={approveDraft}>{t("অনুমোদন করুন","Approve")}</button>
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
