import { useState, useRef, useEffect, useCallback } from "react";

function useIsMobile(breakpoint = 680) {
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= breakpoint);
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${breakpoint}px)`);
    const handler = (e) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [breakpoint]);
  return isMobile;
}

const SEVERITY = {
  4: { bn: "জরুরি",   en: "Critical", color: "#ef4444", bg: "rgba(239,68,68,0.15)" },
  3: { bn: "উচ্চ",    en: "High",     color: "#f59e0b", bg: "rgba(245,158,11,0.15)" },
  2: { bn: "মাঝারি",  en: "Medium",   color: "#3b82f6", bg: "rgba(59,130,246,0.15)" },
  1: { bn: "নিম্ন",   en: "Low",      color: "#10b981", bg: "rgba(16,185,129,0.15)" },
};

const ALERT_LABELS = {
  red_light_violation:  { bn: "লাল বাতি লঙ্ঘন",    en: "Red light violation" },
  wrong_lane:           { bn: "ভুল লেন",            en: "Wrong lane" },
  helmet_missing:       { bn: "হেলমেট নেই",          en: "No helmet" },
  face_match:           { bn: "মুখাবয়ব মিলেছে",     en: "Face match" },
  crowd_dense:          { bn: "ভিড় সতর্কতা",        en: "Crowd alert" },
  stop_line_violation:  { bn: "স্টপ লাইন লঙ্ঘন",   en: "Stop line violation" },
  person_down:          { bn: "ব্যক্তি পড়ে গেছে",  en: "Person down" },
  fire_smoke:           { bn: "আগুন / ধোঁয়া",      en: "Fire / smoke" },
  abandoned_object:     { bn: "পরিত্যক্ত বস্তু",   en: "Abandoned object" },
};

// ── Alert Panel ───────────────────────────────
export function AlertPanel({ alerts, language, onAccept, onReject, onEscalate }) {
  const [selected, setSelected] = useState(null);
  const isMobile = useIsMobile(760);
  const t = (bn, en) => language === "bn" ? bn : en;
  const pending = alerts.filter(a => a.status === "pending");

  const handleSelect = (alert) => {
    setSelected(alert);
  };

  const handleBack = () => setSelected(null);

  const handleAccept = () => { onAccept(selected.alert_id); setSelected(null); };
  const handleReject = () => { onReject(selected.alert_id); setSelected(null); };
  const handleEscalate = () => { onEscalate(selected.alert_id); setSelected(null); };

  // Mobile: show only list OR only detail (never both)
  if (isMobile) {
    return (
      <div className="alert-panel-mobile">
        {!selected ? (
          <div className="alert-list-panel">
            <div className="list-header">
              <span className="list-title">{t("সতর্কতা তালিকা", "Alert list")}</span>
              <span className="list-count">{pending.length} {t("মুলতুবি", "pending")}</span>
            </div>
            <div className="alert-list-scroll">
              {pending.length === 0 && (
                <div className="empty-state">
                  <span className="empty-icon">✅</span>
                  {t("কোনো মুলতুবি সতর্কতা নেই", "No pending alerts")}
                </div>
              )}
              {pending.map(alert => (
                <AlertCard
                  key={alert.alert_id}
                  alert={alert}
                  selected={false}
                  language={language}
                  onClick={() => handleSelect(alert)}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="alert-detail-panel">
            <AlertDetail
              alert={selected}
              language={language}
              onBack={handleBack}
              onAccept={handleAccept}
              onReject={handleReject}
              onEscalate={handleEscalate}
            />
          </div>
        )}
      </div>
    );
  }

  // Desktop: side-by-side
  return (
    <div className="alert-panel">
      <div className="alert-list-panel">
        <div className="list-header">
          <span className="list-title">{t("সতর্কতা তালিকা", "Alert list")}</span>
          <span className="list-count">{pending.length} {t("মুলতুবি", "pending")}</span>
        </div>
        <div className="alert-list-scroll">
          {pending.length === 0 && (
            <div className="empty-state">
              <span className="empty-icon">✅</span>
              {t("কোনো মুলতুবি সতর্কতা নেই", "No pending alerts")}
            </div>
          )}
          {pending.map(alert => (
            <AlertCard
              key={alert.alert_id}
              alert={alert}
              selected={selected?.alert_id === alert.alert_id}
              language={language}
              onClick={() => handleSelect(alert)}
            />
          ))}
        </div>
      </div>

      <div className="alert-detail-panel">
        {!selected ? (
          <div className="no-selection">
            <span className="no-selection-icon">👆</span>
            {t("বাম থেকে একটি সতর্কতা নির্বাচন করুন", "Select an alert from the left to review")}
          </div>
        ) : (
          <AlertDetail
            alert={selected}
            language={language}
            onAccept={handleAccept}
            onReject={handleReject}
            onEscalate={handleEscalate}
          />
        )}
      </div>
    </div>
  );
}

function AlertCard({ alert, selected, language, onClick }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const sev = SEVERITY[alert.severity];
  return (
    <div
      className={`alert-card sev-${alert.severity} ${selected ? "selected" : ""}`}
      onClick={onClick}
    >
      <div className="alert-card-top">
        <div className="severity-badge" style={{ background: sev?.color || "#666" }}>
          {t(sev?.bn, sev?.en) ?? `L${alert.severity}`}
        </div>
        <div className="alert-type-label">
          {t(ALERT_LABELS[alert.alert_type]?.bn, ALERT_LABELS[alert.alert_type]?.en) ?? alert.alert_type}
        </div>
        <div className="alert-conf">{(alert.confidence * 100).toFixed(0)}%</div>
      </div>
      <div className="alert-card-mid">
        <span className="alert-cam">{alert.camera_id}</span>
        <span className="alert-loc">{alert.location}</span>
      </div>
      <div className="alert-time">{new Date(alert.timestamp).toLocaleTimeString()}</div>
    </div>
  );
}

function AlertDetail({ alert, language, onBack, onAccept, onReject, onEscalate }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const sev = SEVERITY[alert.severity];

  return (
    <div className="detail-view">
      {onBack && (
        <button className="detail-back-btn" onClick={onBack}>
          ← {t("তালিকায় ফিরুন", "Back to list")}
        </button>
      )}
      <div className="detail-header-section">
        <div>
          <div className="detail-type">
            {t(ALERT_LABELS[alert.alert_type]?.bn,
               ALERT_LABELS[alert.alert_type]?.en) ?? alert.alert_type}
          </div>
          <div className="detail-meta">
            {alert.camera_id} · {alert.location} · {new Date(alert.timestamp).toLocaleString()}
          </div>
        </div>
        {sev && (
          <div
            className="severity-badge"
            style={{ background: sev.color, marginLeft: "auto", flexShrink: 0 }}
          >
            {t(sev.bn, sev.en)}
          </div>
        )}
      </div>

      {alert.snapshot_b64 && (
        <div className="snapshot-container">
          <img
            src={`data:image/jpeg;base64,${alert.snapshot_b64}`}
            alt="Alert snapshot"
            className="snapshot-img"
          />
        </div>
      )}

      <div className="detail-info">
        <div className="info-row">
          <span>{t("আস্থা", "Confidence")}</span>
          <strong>{(alert.confidence * 100).toFixed(1)}%</strong>
        </div>
        <div className="info-row">
          <span>{t("তীব্রতা", "Severity")}</span>
          <strong>L{alert.severity} — {sev?.en}</strong>
        </div>
        {alert.metadata?.vehicle_type && (
          <div className="info-row">
            <span>{t("যানবাহন", "Vehicle")}</span>
            <strong>{alert.metadata.vehicle_type}</strong>
          </div>
        )}
        {alert.metadata?.matched_name && (
          <div className="info-row">
            <span>{t("মিলেছে", "Matched")}</span>
            <span>
              <strong className="face-match-name">{alert.metadata.matched_name}</strong>
              <span className="risk-tag">{alert.metadata.risk_category}</span>
            </span>
          </div>
        )}
      </div>

      <div className="ai-disclaimer">
        ⚠ {t(
          "এআই সহায়তা প্রদান করেছে। সিদ্ধান্ত ও দায়িত্ব অফিসারের।",
          "AI assisted only. The decision and responsibility remain with the officer."
        )}
      </div>

      <div className="action-buttons">
        <button className="btn-accept" onClick={onAccept}>
          {t("গ্রহণ করুন ✓", "Accept ✓")}
        </button>
        <button className="btn-escalate" onClick={onEscalate}>
          {t("উর্ধ্বতন ↑", "Escalate ↑")}
        </button>
        <button className="btn-reject" onClick={onReject}>
          {t("বাতিল ✗", "Reject ✗")}
        </button>
      </div>
    </div>
  );
}


// ── Camera Grid ───────────────────────────────
const VIDEO_INGEST_URL = import.meta.env.VITE_VIDEO_INGEST_URL || "http://localhost:8001";
const WHEP_BASE        = import.meta.env.VITE_MEDIAMTX_WHEP_URL || "http://localhost:8889";
const HLS_BASE         = import.meta.env.VITE_MEDIAMTX_HLS_URL || "http://localhost:8888";
const TRAFFIC_AI_URL   = import.meta.env.VITE_TRAFFIC_AI_URL || "http://localhost:8002";

const FALLBACK_BRANDS = [
  { id: "hikvision", label: "Hikvision",       notes: "IP + username + password" },
  { id: "dahua",     label: "Dahua / Amcrest", notes: "IP + username + password" },
  { id: "axis",      label: "Axis",            notes: "IP + username + password" },
  { id: "tplink",    label: "TP-Link / Tapo",  notes: "IP + username + password" },
  { id: "reolink",   label: "Reolink",         notes: "IP + username + password" },
  { id: "uniview",   label: "Uniview",         notes: "IP + username + password" },
  { id: "onvif",     label: "ONVIF / Generic", notes: "Common ONVIF path" },
  { id: "custom",    label: "Custom URL",      notes: "Paste full RTSP URL from camera manual" },
];

export function CameraGrid({ language }) {
  const [cameras,   setCameras]   = useState([]);
  const [brands,    setBrands]    = useState([]);
  const [showForm,  setShowForm]  = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(defaultForm());
  const [msg,  setMsg]  = useState(null);
  const [busy, setBusy] = useState(false);

  const t = (bn, en) => language === "bn" ? bn : en;

  function defaultForm() {
    return {
      camera_id: "", name: "", brand: "hikvision", connection_mode: "pull",
      host: "", port: 554, username: "", password: "", channel: 1,
      rtsp_url: "", location_name: "", zone_type: "entry_exit",
    };
  }

  const load = async () => {
    try {
      const [c, b] = await Promise.all([
        fetch(`${VIDEO_INGEST_URL}/cameras`).then(r => r.json()),
        fetch(`${VIDEO_INGEST_URL}/cameras/brands`).then(r => r.json()),
      ]);
      setCameras(c);
      setBrands(b.brands || []);
    } catch {}
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  const resetForm = () => { setEditingId(null); setForm(defaultForm()); setMsg(null); };

  const openAddForm = () => { resetForm(); setShowForm(true); };

  const openEditForm = async (cameraId) => {
    setMsg(null);
    setBusy(true);
    try {
      const resp = await fetch(`${VIDEO_INGEST_URL}/cameras/${cameraId}`);
      const cam = await resp.json();
      if (!resp.ok) throw new Error(cam.detail || "Failed to load camera");
      setEditingId(cameraId);
      setForm({
        camera_id: cam.camera_id, name: cam.name || "",
        brand: cam.brand || "custom", connection_mode: cam.connection_mode || "pull",
        host: cam.host || "", port: cam.port || 554,
        username: cam.username || "", password: "",
        channel: cam.channel || 1, rtsp_url: cam.rtsp_url || "",
        location_name: cam.location_name || "", zone_type: cam.zone_type || "entry_exit",
      });
      setShowForm(true);
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (cameraId, cameraName) => {
    if (!window.confirm(t(`"${cameraName || cameraId}" মুছে ফেলবেন?`, `Delete camera "${cameraName || cameraId}"?`))) return;
    setBusy(true);
    try {
      const resp = await fetch(`${VIDEO_INGEST_URL}/cameras/${cameraId}`, { method: "DELETE" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || "Delete failed");
      if (editingId === cameraId) { setShowForm(false); resetForm(); }
      load();
    } catch (err) {
      alert(err.message);
    } finally {
      setBusy(false);
    }
  };

  const brandOptions = brands.length > 0 ? brands : FALLBACK_BRANDS;
  const isCustom  = form.brand === "custom";
  const isPublish = form.connection_mode === "publish";
  const brandMeta = brandOptions.find(b => b.id === form.brand);

  const onSubmit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const payload = { ...form };
      if (editingId && !payload.password) delete payload.password;
      const url    = editingId ? `${VIDEO_INGEST_URL}/cameras/${editingId}` : `${VIDEO_INGEST_URL}/cameras/connect`;
      const method = editingId ? "PATCH" : "POST";
      const resp = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await resp.json();
      if (!resp.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      setMsg({ ok: data.stream_ok !== false, text: data.status_message || data.instructions || (editingId ? t("ক্যামেরা আপডেট হয়েছে", "Camera updated") : t("ক্যামেরা যুক্ত হয়েছে", "Camera connected")) });
      setShowForm(false);
      resetForm();
      load();
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="camera-grid">
      <div className="grid-header">
        <div className="grid-title">
          {t("লাইভ ক্যামেরা", "Live cameras")}
          <span className="grid-count">{cameras.length}</span>
        </div>
        <button
          className="btn-primary btn-sm"
          onClick={() => showForm && !editingId ? (setShowForm(false), resetForm()) : openAddForm()}
        >
          {showForm && !editingId ? t("বাতিল", "Cancel") : t("+ ক্যামেরা যোগ", "+ Add camera")}
        </button>
      </div>

      {showForm && (
        <form className="camera-form" onSubmit={onSubmit}>
          <div className="form-section-title">
            {editingId ? t(`সম্পাদনা: ${editingId}`, `Edit: ${editingId}`) : t("নতুন ক্যামেরা", "New camera")}
          </div>

          <div className="form-row">
            <label>{t("ক্যামেরা ID", "Camera ID")}</label>
            <input required readOnly={!!editingId} minLength={2} maxLength={32}
              pattern={"^[-a-zA-Z0-9_]{2,32}$"}
              title="2–32 characters: letters, numbers, hyphen, underscore"
              placeholder="cam05"
              value={form.camera_id} onChange={e => setForm({ ...form, camera_id: e.target.value })} />
          </div>
          <div className="form-row">
            <label>{t("নাম", "Name")}</label>
            <input required placeholder="Main Gate"
              value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="form-row">
            <label>{t("ব্র্যান্ড", "Brand")}</label>
            <select value={form.brand} onChange={e => setForm({ ...form, brand: e.target.value, connection_mode: e.target.value === "custom" ? "pull" : form.connection_mode })}>
              {brandOptions.map(b => <option key={b.id} value={b.id}>{b.label}</option>)}
            </select>
            {brandMeta?.notes && <span className="form-hint">{brandMeta.notes}</span>}
          </div>

          {!isCustom && (
            <div className="form-row">
              <label>{t("সংযোগ মোড", "Connection mode")}</label>
              <select value={form.connection_mode} onChange={e => setForm({ ...form, connection_mode: e.target.value })}>
                <option value="pull">{t("Pull — সার্ভার ক্যামেরা থেকে নেয়", "Pull — server reads camera")}</option>
                <option value="publish">{t("Push — ক্যামেরা সার্ভারে পাঠায়", "Push — camera sends to server")}</option>
              </select>
            </div>
          )}

          {isCustom && (
            <div className="form-section custom-url-section">
              <div className="form-section-title">{t("কাস্টম RTSP URL", "Custom RTSP URL")}</div>
              <div className="form-row">
                <label>{t("সম্পূর্ণ RTSP URL", "Full RTSP URL")} *</label>
                <input required className="rtsp-url-input"
                  placeholder="rtsp://admin:password@192.168.1.50:554/stream1"
                  value={form.rtsp_url} onChange={e => setForm({ ...form, rtsp_url: e.target.value.trim() })} />
                <span className="form-hint">
                  {t("ক্যামেরার RTSP URL পেস্ট করুন। সাধারণ পোর্ট: 554", "Paste the camera RTSP URL. Typical port: 554")}
                </span>
              </div>
            </div>
          )}

          {!isCustom && !isPublish && (
            <div className="form-section">
              <div className="form-section-title">{t("ক্যামেরা credentials", "Camera credentials")}</div>
              <div className="form-row">
                <label>IP / Host *</label>
                <input required placeholder="192.168.1.100"
                  value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} />
              </div>
              <div className="form-row">
                <label>{t("পোর্ট", "Port")}</label>
                <input type="number" value={form.port} onChange={e => setForm({ ...form, port: +e.target.value })} />
              </div>
              <div className="form-row">
                <label>{t("ইউজার", "Username")}</label>
                <input placeholder="admin" value={form.username} onChange={e => setForm({ ...form, username: e.target.value })} />
              </div>
              <div className="form-row">
                <label>{t("পাসওয়ার্ড", "Password")}</label>
                <input type="password" value={form.password}
                  placeholder={editingId ? t("খালি = আগের পাসওয়ার্ড", "Leave blank to keep current") : ""}
                  onChange={e => setForm({ ...form, password: e.target.value })} />
              </div>
              <div className="form-row">
                <label>{t("চ্যানেল", "Channel")}</label>
                <input type="number" min="1" value={form.channel} onChange={e => setForm({ ...form, channel: +e.target.value })} />
              </div>
            </div>
          )}

          {!isCustom && isPublish && (
            <p className="publish-hint">
              {t("যোগ করার পর ক্যামেরায় RTSP publish URL সেট করুন:", "After adding, set your camera RTSP publish URL to:")}{" "}
              <code>rtsp://&lt;server-ip&gt;:8554/{form.camera_id || "cam_id"}</code>
            </p>
          )}

          <div className="form-row">
            <label>{t("অবস্থান", "Location")}</label>
            <input value={form.location_name} onChange={e => setForm({ ...form, location_name: e.target.value })} />
          </div>

          <div className="form-actions">
            <button type="submit" className="btn-primary" disabled={busy}>
              {busy ? t("সংরক্ষণ হচ্ছে…", "Saving…") : editingId ? t("আপডেট করুন", "Update") : t("সংযুক্ত করুন", "Connect")}
            </button>
            {editingId && (
              <button type="button" className="btn-cancel" disabled={busy} onClick={() => { setShowForm(false); resetForm(); }}>
                {t("বাতিল", "Cancel")}
              </button>
            )}
          </div>
          {msg && <div className={msg.ok ? "form-ok" : "form-err"}>{msg.text}</div>}
        </form>
      )}

      <div className="camera-cells">
        {cameras.length === 0 && (
          <div className="empty-state" style={{ gridColumn: "1/-1" }}>
            <span className="empty-icon">📷</span>
            {t("কোনো ক্যামেরা নেই — উপরে যোগ করুন", "No cameras — add one above")}
          </div>
        )}
        {cameras.map(cam => (
          <WhepPlayer
            key={cam.camera_id}
            camera={{
              id:               cam.camera_id,
              name:             cam.name,
              streaming:        cam.streaming,
              stream_status:    cam.stream_status,
              status_message:   cam.status_message,
              connection_mode:  cam.connection_mode,
              brand:            cam.brand,
              location:         cam.location_name,
              whep:             cam.whep_url || `${WHEP_BASE}/${cam.camera_id}/whep`,
              hls:              cam.hls_url  || `${HLS_BASE}/${cam.camera_id}/index.m3u8`,
              playback_mode:    cam.playback_mode,
              webrtc_compatible: cam.webrtc_compatible !== false,
              video_codec:      cam.video_codec,
            }}
            language={language}
            onEdit={() => openEditForm(cam.camera_id)}
            onDelete={() => onDelete(cam.camera_id, cam.name)}
            onRefresh={load}
          />
        ))}
      </div>
    </div>
  );
}

function WhepPlayer({ camera, language, onEdit, onDelete, onRefresh }) {
  const videoRef  = useRef(null);
  const hlsRef    = useRef(null);
  const timerRef  = useRef(null);
  const [status,      setStatus]      = useState("connecting");
  const [errorMsg,    setErrorMsg]    = useState(camera.status_message || "");
  const [testResult,  setTestResult]  = useState(null); // { ok, text }
  const [testBusy,    setTestBusy]    = useState(false);
  const [viewMode,    setViewMode]    = useState("live"); // "live" | "ai"

  const showTestResult = (result) => {
    clearTimeout(timerRef.current);
    setTestResult(result);
    timerRef.current = setTimeout(() => setTestResult(null), 6000);
  };

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const runTest = async () => {
    if (testBusy) return;
    setTestBusy(true);
    setTestResult(null);
    try {
      const r = await fetch(`${VIDEO_INGEST_URL}/cameras/${camera.id}/test`, { method: "POST" });
      const d = await r.json().catch(() => ({}));
      const errText = d.error || d.status_message || d.detail || `Error ${r.status}`;
      const passed = d.ok === true || (r.ok && d.ok !== false);
      showTestResult({ ok: passed, text: passed ? (d.status_message || "Connection OK") : errText });
      if (!passed) setErrorMsg(errText);
      if (passed) onRefresh?.();
    } catch (e) {
      showTestResult({ ok: false, text: e.message || "Network error" });
    } finally {
      setTestBusy(false);
    }
  };

  const t = (bn, en) => language === "bn" ? bn : en;

  useEffect(() => {
    setErrorMsg(camera.status_message || "");
    if (camera.stream_status !== "live") {
      setStatus(camera.stream_status === "waiting" ? "waiting" : "error");
      return;
    }
    // AI detection mode renders the worker's annotated MJPEG via an <img>,
    // so skip the WHEP/HLS connection entirely while it's active.
    if (viewMode === "ai") {
      setStatus("live");
      return;
    }

    let pc = null;
    let cancelled = false;

    async function connectHls() {
      const video = videoRef.current;
      if (!video || cancelled) return;
      setStatus("connecting");
      try {
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = camera.hls;
          await video.play();
          if (!cancelled) setStatus("live");
          return;
        }
        const { default: Hls } = await import("hls.js");
        if (!Hls.isSupported()) throw new Error("HLS not supported");
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        hlsRef.current = hls;
        hls.loadSource(camera.hls);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (!cancelled) { video.play().catch(() => {}); setStatus("live"); } });
        hls.on(Hls.Events.ERROR, (_, data) => { if (!cancelled && data.fatal) { setStatus("error"); setErrorMsg("HLS playback failed"); } });
      } catch {
        if (!cancelled) { setStatus("error"); setErrorMsg("HLS playback failed"); }
      }
    }

    async function connectWhep() {
      setStatus("connecting");
      // LAN-only deployment: no public STUN/TURN. MediaMTX advertises a
      // reachable host candidate (webrtcAdditionalHosts), so host candidates
      // connect directly — a public STUN here just causes ICE to stall/fail.
      pc = new RTCPeerConnection({ iceServers: [] });
      pc.ontrack = e => { if (!cancelled && videoRef.current && e.streams[0]) { videoRef.current.srcObject = e.streams[0]; setStatus("live"); } };
      pc.oniceconnectionstatechange = () => { if (pc?.iceConnectionState === "failed") setStatus("error"); };
      pc.addTransceiver("video", { direction: "recvonly" });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await fetch(camera.whep, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: offer.sdp });
      if (cancelled) return;
      if (resp.ok) {
        const sdp = await resp.text();
        await pc.setRemoteDescription({ type: "answer", sdp });
      } else {
        const body = await resp.text().catch(() => "");
        if (body.includes("codecs not supported") || camera.playback_mode === "hls") { pc.close(); pc = null; await connectHls(); return; }
        setStatus("error");
        setErrorMsg("WHEP failed — click Test (⚡) to diagnose");
      }
    }

    async function connect() {
      if (camera.playback_mode === "hls") {
        await connectHls();
      } else {
        try { await connectWhep(); } catch { if (!cancelled) await connectHls(); }
      }
    }

    connect();
    return () => {
      cancelled = true;
      pc?.close();
      hlsRef.current?.destroy();
      hlsRef.current = null;
      if (videoRef.current) { videoRef.current.removeAttribute("src"); videoRef.current.srcObject = null; }
    };
  }, [camera.whep, camera.hls, camera.stream_status, camera.status_message, camera.playback_mode, camera.webrtc_compatible, camera.video_codec, viewMode]);

  const statusLabel = {
    live:       t("লাইভ", "Live"),
    connecting: t("সংযোগ হচ্ছে…", "Connecting…"),
    waiting:    t("অপেক্ষমান", "Waiting"),
    error:      t("সমস্যা", "No video"),
  }[status] || status;

  return (
    <div className="camera-cell">
      <div className="camera-label">
        <span className={`cam-status-dot ${status}`} />
        <span className="camera-title-group">
          <span className="camera-name">{camera.name}</span>
          <span className="cam-id">{camera.id}</span>
          <span className={`cam-mode-badge mode-${camera.connection_mode}`}>
            {camera.connection_mode === "publish" ? "Push" : "Pull"}
          </span>
          <span className={`cam-status-badge status-${status}`}>{statusLabel}</span>
        </span>
        <span className="camera-actions">
          <button
            type="button"
            className={`btn-ai-toggle ${viewMode === "ai" ? "active" : ""}`}
            title={t("রিয়েলটাইম অবজেক্ট ডিটেকশন", "Toggle realtime AI detection overlay")}
            onClick={() => setViewMode(m => (m === "ai" ? "live" : "ai"))}
          >
            {viewMode === "ai" ? t("লাইভ", "Live") : "AI"}
          </button>
          <button
            type="button"
            className={`btn-icon ${testBusy ? "btn-icon-busy" : ""}`}
            title={t("টেস্ট", "Test connection")}
            onClick={runTest}
            disabled={testBusy}
          >
            {testBusy ? "…" : "⚡"}
          </button>
          <button type="button" className="btn-icon" title={t("সম্পাদনা", "Edit")} onClick={onEdit}>✎</button>
          <button type="button" className="btn-icon btn-icon-danger" title={t("মুছুন", "Delete")} onClick={onDelete}>✕</button>
        </span>
      </div>

      {testResult && (
        <div className={`cam-test-result ${testResult.ok ? "cam-test-ok" : "cam-test-err"}`}>
          {testResult.ok ? "✓" : "✗"} {testResult.text}
          <button className="cam-test-close" onClick={() => setTestResult(null)}>×</button>
        </div>
      )}

      {!testResult && (status === "error" || status === "waiting" || camera.video_codec === "H265") && errorMsg && (
        <div className="cam-status-msg">{errorMsg}</div>
      )}

      <div className="video-container">
        {viewMode === "ai" ? (
          <>
            <img
              className="camera-video"
              src={`${TRAFFIC_AI_URL}/preview/${camera.id}.mjpg`}
              alt={`AI detection ${camera.id}`}
              onError={() => setErrorMsg(t("AI ডিটেকশন স্ট্রিম পাওয়া যায়নি — traffic-ai চলছে কিনা দেখুন", "AI detection stream unavailable — is traffic-ai running for this camera?"))}
            />
            <span className="ai-overlay-badge">{t("● এআই ডিটেকশন", "● AI DETECTION")}</span>
          </>
        ) : status !== "live" && status !== "connecting" ? (
          <div className="cam-offline">
            <span className="cam-offline-icon">{status === "waiting" ? "⏳" : "📵"}</span>
            {status === "waiting" ? t("ক্যামেরা push এর অপেক্ষা", "Waiting for camera push") : t("ভিডিও নেই", "No video")}
          </div>
        ) : (
          <video ref={videoRef} autoPlay muted playsInline className="camera-video" />
        )}
        {viewMode !== "ai" && status === "connecting" && (
          <div className="cam-connecting">
            <span className="cam-offline-icon">📡</span>
            {t("সংযোগ হচ্ছে…", "Connecting…")}
          </div>
        )}
      </div>
    </div>
  );
}


// ── Incident List ──────────────────────────────
export function IncidentList({ incidents, language }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const open = incidents.filter(i => i.status === "open").length;

  return (
    <div className="incident-list">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ঘটনা তালিকা", "Incident cards")}</div>
          <div className="page-sub">{open} {t("খোলা ঘটনা", "open incidents")}</div>
        </div>
      </div>

      {incidents.length === 0 && (
        <div className="empty-state">
          <span className="empty-icon">📋</span>
          {t("কোনো ঘটনা নেই", "No incidents")}
        </div>
      )}

      {incidents.map(inc => (
        <div key={inc.id} className={`incident-card status-${inc.status}`}>
          <div className="inc-header">
            <div className="inc-title">{inc.title}</div>
            <span className={`inc-status ${inc.status}`}>
              {t(
                { open: "খোলা", assigned: "নিযুক্ত", dispatched: "প্রেরিত", closed: "বন্ধ" }[inc.status],
                inc.status
              )}
            </span>
          </div>
          <div className="inc-meta">
            <span>📍 {inc.location_name || "—"}</span>
            <span>🕐 {new Date(inc.created_at).toLocaleString()}</span>
            <span>L{inc.severity}</span>
          </div>
          {inc.alert_types && (
            <div className="inc-types">
              {inc.alert_types.map(at => (
                <span key={at} className="inc-type-tag">{at}</span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}


// ── Status bar ────────────────────────────────
export function StatusBar({ alertCount, pendingCount, language }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const [now, setNow] = useState(new Date().toLocaleTimeString());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date().toLocaleTimeString()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <footer className="status-bar">
      <span>{t("মোট", "Total")}: <span className="stat-value">{alertCount}</span></span>
      <span className="sep">·</span>
      <span>{t("মুলতুবি", "Pending")}: <span className="stat-pending">{pendingCount}</span></span>
      <span className="sep">·</span>
      <span className="ai-note">{t("এআই সহায়তা করে। পুলিশ সিদ্ধান্ত নেয়।", "AI assists. Police decide.")}</span>
      <span className="time">{now}</span>
    </footer>
  );
}
