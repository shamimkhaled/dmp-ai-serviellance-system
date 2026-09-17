import { useEffect, useState } from "react";
import { AdminPage, EvidencePage } from "../components/index";
import { DraftingTool } from "../components/DraftingTool";
import { ALERT_SERVICE_URL, SEVERITY } from "../lib/constants";
import { IconEmpty } from "../lib/icons";

function RulesPanel({ language }) {
  const [rules, setRules] = useState([]);
  const [msg, setMsg] = useState(null);
  const t = (bn, en) => language === "bn" ? bn : en;

  const load = () =>
    fetch(`${ALERT_SERVICE_URL}/rules?include_disabled=true`)
      .then(r => r.json())
      .then(d => setRules(Array.isArray(d) ? d : []))
      .catch(() => setRules([]));

  useEffect(() => { load(); }, []);

  const patch = async (id, body) => {
    setMsg(null);
    try {
      const r = await fetch(`${ALERT_SERVICE_URL}/rules/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      await load();
    } catch (e) { setMsg(e.message); }
  };

  return (
    <div className="rules-panel">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("এআই নিয়ম","AI rules")}</div>
          <div className="page-sub">{t("সতর্কতার তীব্রতা শুধু নিয়ম থেকে আসে","Alert severity comes only from rules")}</div>
        </div>
      </div>
      {msg && <div className="form-err">{msg}</div>}
      {rules.length === 0 && (
        <div className="empty-state"><span className="empty-icon"><IconEmpty /></span>{t("কোনো নিয়ম নেই","No rules")}</div>
      )}
      {rules.length > 0 && (
        <div className="events-table-wrap">
          <table className="events-table">
            <thead>
              <tr>
                <th>{t("নাম","Name")}</th>
                <th>{t("ইভেন্ট","Event")}</th>
                <th>{t("সতর্কতা","Alert")}</th>
                <th>{t("তীব্রতা","Severity")}</th>
                <th>{t("সক্রিয়","Enabled")}</th>
                <th>{t("কুলডাউন","Cooldown")}</th>
              </tr>
            </thead>
            <tbody>
              {rules.map(rule => (
                <tr key={rule.id} className={rule.enabled ? "" : "row-disabled"}>
                  <td>{rule.name}</td>
                  <td className="mono">{rule.event_type || "*"}</td>
                  <td className="mono">{rule.alert_type}</td>
                  <td>
                    <select
                      aria-label={t("তীব্রতা","Severity")}
                      value={rule.severity}
                      onChange={e => patch(rule.id, { severity: Number(e.target.value) })}
                    >
                      {[4, 3, 2, 1].map(s => (
                        <option key={s} value={s}>{t(SEVERITY[s].bn, SEVERITY[s].en)}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <button type="button" className={`layer-toggle ${rule.enabled ? "on" : ""}`}
                      aria-pressed={rule.enabled}
                      onClick={() => patch(rule.id, { enabled: !rule.enabled })}>
                      {rule.enabled ? t("চালু","On") : t("বন্ধ","Off")}
                    </button>
                  </td>
                  <td className="mono">{rule.cooldown_seconds}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function AdminHub({ language, officer, role = "admin", alertApiUrl, videoIngestUrl, trafficAiUrl }) {
  const isAdmin = role === "admin";
  const [tab, setTab] = useState(isAdmin ? "system" : "industry");
  const t = (bn, en) => language === "bn" ? bn : en;
  const tabs = [
    ...(isAdmin ? [
      ["system", t("সিস্টেম","System")],
      ["rules", t("নিয়ম","Rules")],
    ] : []),
    ["industry", t("ইন্ডাস্ট্রি প্যাক","Industry pack")],
  ];

  return (
    <div className="admin-hub">
      <div className="sub-tabs" role="tablist">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" role="tab" aria-selected={tab === key}
            className={`sub-tab ${tab === key ? "active" : ""}`}
            onClick={() => setTab(key)}>{label}</button>
        ))}
      </div>
      {tab === "system" && (
        <AdminPage
          language={language}
          alertApiUrl={alertApiUrl}
          videoIngestUrl={videoIngestUrl}
          trafficAiUrl={trafficAiUrl}
        />
      )}
      {tab === "rules" && <RulesPanel language={language} />}
      {tab === "industry" && (
        <div className="industry-pack">
          <p className="industry-note">
            {t(
              "প্রমাণ ও জিডি/এফআইআর পুলিশ ইন্ডাস্ট্রি প্যাকের অংশ — মূল ন্যাভে নেই।",
              "Evidence and GD/FIR belong to the police industry pack — not on the main nav."
            )}
          </p>
          <EvidencePage language={language} apiUrl={alertApiUrl} />
          <DraftingTool language={language} officer={officer} />
        </div>
      )}
    </div>
  );
}
