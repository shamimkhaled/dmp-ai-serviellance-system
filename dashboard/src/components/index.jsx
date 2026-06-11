import { useState, useRef, useEffect, useCallback } from "react";

// ── Shared helpers ────────────────────────────────────────────────────────────
function useIsMobile(bp = 680) {
  const [m, setM] = useState(() => window.innerWidth <= bp);
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${bp}px)`);
    const h  = (e) => setM(e.matches);
    mq.addEventListener("change", h);
    return () => mq.removeEventListener("change", h);
  }, [bp]);
  return m;
}

export const SEVERITY = {
  4: { bn: "জরুরি",   en: "Critical", color: "#ef4444", bg: "rgba(239,68,68,0.15)" },
  3: { bn: "উচ্চ",    en: "High",     color: "#f59e0b", bg: "rgba(245,158,11,0.15)" },
  2: { bn: "মাঝারি",  en: "Medium",   color: "#3b82f6", bg: "rgba(59,130,246,0.15)" },
  1: { bn: "নিম্ন",   en: "Low",      color: "#10b981", bg: "rgba(16,185,129,0.15)" },
};

export const ALERT_LABELS = {
  red_light_violation:  { bn: "লাল বাতি লঙ্ঘন",    en: "Red light violation" },
  wrong_lane:           { bn: "ভুল লেন",            en: "Wrong lane" },
  helmet_missing:       { bn: "হেলমেট নেই",          en: "No helmet" },
  face_match:           { bn: "মুখাবয়ব মিলেছে",     en: "Face match" },
  crowd_dense:          { bn: "ভিড় সতর্কতা",        en: "Crowd alert" },
  stop_line_violation:  { bn: "স্টপ লাইন লঙ্ঘন",   en: "Stop line violation" },
  person_down:          { bn: "ব্যক্তি পড়ে গেছে",  en: "Person down" },
  fire_smoke:           { bn: "আগুন / ধোঁয়া",      en: "Fire / smoke" },
  abandoned_object:     { bn: "পরিত্যক্ত বস্তু",   en: "Abandoned object" },
  illegal_parking:      { bn: "অবৈধ পার্কিং",       en: "Illegal parking" },
  speeding:             { bn: "অতিরিক্ত গতি",       en: "Speeding" },
};

const VIDEO_INGEST_URL = import.meta.env.VITE_VIDEO_INGEST_URL || "http://localhost:8001";
const WHEP_BASE        = import.meta.env.VITE_MEDIAMTX_WHEP_URL || "http://localhost:8889";
const HLS_BASE         = import.meta.env.VITE_MEDIAMTX_HLS_URL  || "http://localhost:8888";
const TRAFFIC_AI_URL   = import.meta.env.VITE_TRAFFIC_AI_URL    || "http://localhost:8002";
const TRAFFIC_AI_WS    = TRAFFIC_AI_URL.replace(/^http/, "ws");

const CHART_COLORS = ["#3b82f6","#ef4444","#f59e0b","#10b981","#8b5cf6","#ec4899","#06b6d4","#f97316"];

// ── Status bar ────────────────────────────────────────────────────────────────
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

// ═══════════════════════════════════════════════════════════════════════════════
// COMMAND CENTER
// ═══════════════════════════════════════════════════════════════════════════════
export function CommandCenter({ cameras, alerts, language, onAlertSelect }) {
  const [highlightedCams, setHighlightedCams] = useState(new Set());
  const t = (bn, en) => language === "bn" ? bn : en;

  const pending     = alerts.filter(a => a.status === "pending");
  const liveCameras = cameras.filter(c => c.streaming || c.stream_status === "live");
  const todayAlerts = alerts.filter(a => {
    try { return new Date(a.timestamp).toDateString() === new Date().toDateString(); }
    catch { return false; }
  });

  const lastAlertId = alerts[0]?.alert_id;
  useEffect(() => {
    if (!alerts[0] || alerts[0].status !== "pending") return;
    const camId = alerts[0].camera_id;
    setHighlightedCams(prev => new Set([...prev, camId]));
    const timer = setTimeout(() => {
      setHighlightedCams(prev => { const n = new Set(prev); n.delete(camId); return n; });
    }, 4000);
    return () => clearTimeout(timer);
  }, [lastAlertId]);

  return (
    <div className="command-center">
      {/* Stats bar */}
      <div className="cc-stats-bar">
        <div className="cc-stat">
          <span className="cc-stat-value cc-stat-green">{liveCameras.length}</span>
          <span className="cc-stat-label">{t("লাইভ ক্যামেরা", "Live Cameras")}</span>
        </div>
        <div className="cc-stat">
          <span className="cc-stat-value cc-stat-red">{pending.length}</span>
          <span className="cc-stat-label">{t("মুলতুবি সতর্কতা", "Pending Alerts")}</span>
        </div>
        <div className="cc-stat">
          <span className="cc-stat-value cc-stat-yellow">{todayAlerts.length}</span>
          <span className="cc-stat-label">{t("আজকের ঘটনা", "Today's Events")}</span>
        </div>
        <div className="cc-stat">
          <span className="cc-stat-value">{cameras.length}</span>
          <span className="cc-stat-label">{t("মোট ক্যামেরা", "Total Cameras")}</span>
        </div>
      </div>

      <div className="cc-body">
        {/* Camera grid — all cameras showing AI annotated MJPEG */}
        <div className="cc-camera-grid">
          {cameras.length === 0 && (
            <div className="cc-empty">
              <span className="empty-icon">📷</span>
              {t("কোনো ক্যামেরা নেই", "No cameras configured")}
            </div>
          )}
          {cameras.map(cam => {
            const isAlert = highlightedCams.has(cam.camera_id);
            return (
              <div key={cam.camera_id}
                className={`cc-cam-tile ${isAlert ? "cc-cam-alert" : ""}`}>
                <div className="cc-cam-header">
                  <span className={`cam-status-dot ${cam.streaming || cam.stream_status === "live" ? "live" : "error"}`} />
                  <span className="cc-cam-name">{cam.name || cam.camera_id}</span>
                  <span className="cc-cam-location">{cam.location_name || ""}</span>
                  {isAlert && <span className="cc-violation-badge">⚠ {t("লঙ্ঘন", "VIOLATION")}</span>}
                  <span className="cc-cam-id">{cam.camera_id}</span>
                </div>
                <img
                  className="cc-cam-video"
                  src={`${TRAFFIC_AI_URL}/preview/${cam.camera_id}.mjpg`}
                  alt={cam.name || cam.camera_id}
                />
              </div>
            );
          })}
        </div>

        {/* Live alert feed */}
        <div className="cc-alert-feed">
          <div className="cc-feed-title">
            <span className="cc-feed-dot" />
            {t("লাইভ সতর্কতা", "Live Alerts")}
            {pending.length > 0 && <span className="cc-feed-badge">{pending.length}</span>}
          </div>
          <div className="cc-feed-scroll">
            {pending.length === 0 && (
              <div className="cc-feed-empty">
                <span>✅</span>
                {t("কোনো মুলতুবি সতর্কতা নেই", "No pending alerts")}
              </div>
            )}
            {pending.slice(0, 40).map(alert => (
              <div key={alert.alert_id}
                className={`cc-feed-item cc-feed-sev-${alert.severity}`}
                onClick={() => onAlertSelect?.(alert)}>
                <div className="cc-feed-top">
                  <span className="cc-feed-sev-dot" style={{ background: SEVERITY[alert.severity]?.color }} />
                  <span className="cc-feed-type">
                    {ALERT_LABELS[alert.alert_type]?.en ?? alert.alert_type}
                  </span>
                  <span className="cc-feed-conf">{(alert.confidence * 100).toFixed(0)}%</span>
                </div>
                <div className="cc-feed-meta">
                  <span>{alert.camera_id}</span>
                  <span>{new Date(alert.timestamp).toLocaleTimeString()}</span>
                </div>
                {alert.snapshot_b64 && (
                  <img className="cc-feed-thumb"
                    src={`data:image/jpeg;base64,${alert.snapshot_b64}`}
                    alt="snap" />
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DETECTION CANVAS  — WebSocket bbox overlay on the live <video>
// ═══════════════════════════════════════════════════════════════════════════════
function DetectionCanvas({ cameraId, videoRef }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!cameraId) return;
    const url = `${TRAFFIC_AI_WS}/detections/${cameraId}/ws`;
    let ws, reconnectTimer, pingTimer;

    function connect() {
      ws = new WebSocket(url);

      ws.onopen = () => {
        pingTimer = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 20000);
      };

      ws.onmessage = (evt) => {
        if (evt.data === "pong") return;
        let data;
        try { data = JSON.parse(evt.data); } catch { return; }
        if (data.type !== "detections") return;
        draw(data);
      };

      ws.onclose = () => {
        clearInterval(pingTimer);
        reconnectTimer = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws.close();
    }

    function draw(data) {
      const canvas = canvasRef.current;
      const video  = videoRef?.current;
      if (!canvas) return;
      const W = video?.clientWidth  || 640;
      const H = video?.clientHeight || 360;
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      const ctx  = canvas.getContext("2d");
      const srcW = data.frame_w || 640;
      const srcH = data.frame_h || 360;
      const sx = W / srcW;
      const sy = H / srcH;
      ctx.clearRect(0, 0, W, H);

      (data.detections || []).forEach(det => {
        const [x1, y1, x2, y2] = det.bbox;
        const rx1 = x1 * sx, ry1 = y1 * sy, rw = (x2 - x1) * sx, rh = (y2 - y1) * sy;
        const isV  = !!det.violation;
        const col  = isV ? "#ef4444" : "#22c55e";
        ctx.strokeStyle = col;
        ctx.lineWidth   = isV ? 3 : 1.5;
        ctx.strokeRect(rx1, ry1, rw, rh);
        const label = isV
          ? `⚠ ${det.violation.replace(/_/g," ")} [${det.class}]${det.track_id != null ? " #"+det.track_id : ""}`
          : `${det.class} ${(det.confidence*100).toFixed(0)}%${det.track_id != null ? " #"+det.track_id : ""}`;
        ctx.font = "11px monospace";
        const tw = ctx.measureText(label).width + 8;
        const ly = ry1 > 18 ? ry1 - 16 : ry1 + rh;
        ctx.fillStyle = col;
        ctx.fillRect(rx1, ly, tw, 16);
        ctx.fillStyle = isV ? "#fff" : "#000";
        ctx.fillText(label, rx1 + 4, ly + 11);
      });

      const n = (data.detections || []).length;
      if (n > 0) {
        ctx.font = "bold 11px monospace";
        ctx.fillStyle = "rgba(0,0,0,0.65)";
        ctx.fillRect(4, 4, 130, 18);
        ctx.fillStyle = "#22c55e";
        ctx.fillText(`${n} object${n > 1 ? "s" : ""} detected`, 8, 16);
      }
    }

    connect();
    return () => {
      clearInterval(pingTimer);
      clearTimeout(reconnectTimer);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }, [cameraId]);

  return (
    <canvas ref={canvasRef} style={{
      position: "absolute", top: 0, left: 0,
      width: "100%", height: "100%",
      pointerEvents: "none", zIndex: 2,
    }} />
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// CAMERA GRID
// ═══════════════════════════════════════════════════════════════════════════════
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
  const [form,      setForm]      = useState(defaultForm());
  const [msg,       setMsg]       = useState(null);
  const [busy,      setBusy]      = useState(false);
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
      setCameras(c); setBrands(b.brands || []);
    } catch {}
  };

  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, []);

  const resetForm  = () => { setEditingId(null); setForm(defaultForm()); setMsg(null); };
  const openAddForm = () => { resetForm(); setShowForm(true); };

  const openEditForm = async (cameraId) => {
    setMsg(null); setBusy(true);
    try {
      const r = await fetch(`${VIDEO_INGEST_URL}/cameras/${cameraId}`);
      const cam = await r.json();
      if (!r.ok) throw new Error(cam.detail || "Failed to load camera");
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
    } finally { setBusy(false); }
  };

  const onDelete = async (cameraId, cameraName) => {
    if (!window.confirm(t(`"${cameraName || cameraId}" মুছে ফেলবেন?`, `Delete camera "${cameraName || cameraId}"?`))) return;
    setBusy(true);
    try {
      const r = await fetch(`${VIDEO_INGEST_URL}/cameras/${cameraId}`, { method: "DELETE" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Delete failed");
      if (editingId === cameraId) { setShowForm(false); resetForm(); }
      load();
    } catch (err) { alert(err.message); }
    finally { setBusy(false); }
  };

  const brandOptions = brands.length > 0 ? brands : FALLBACK_BRANDS;
  const isCustom  = form.brand === "custom";
  const isPublish = form.connection_mode === "publish";
  const brandMeta = brandOptions.find(b => b.id === form.brand);

  const onSubmit = async (e) => {
    e.preventDefault(); setBusy(true); setMsg(null);
    try {
      const payload = { ...form };
      if (editingId && !payload.password) delete payload.password;
      const url    = editingId ? `${VIDEO_INGEST_URL}/cameras/${editingId}` : `${VIDEO_INGEST_URL}/cameras/connect`;
      const method = editingId ? "PATCH" : "POST";
      const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      setMsg({ ok: true, text: d.status_message || d.instructions || (editingId ? t("ক্যামেরা আপডেট হয়েছে", "Camera updated") : t("ক্যামেরা যুক্ত হয়েছে", "Camera connected")) });
      setShowForm(false); resetForm(); load();
    } catch (err) { setMsg({ ok: false, text: err.message }); }
    finally { setBusy(false); }
  };

  return (
    <div className="camera-grid">
      <div className="grid-header">
        <div className="grid-title">
          {t("লাইভ ক্যামেরা", "Live cameras")}
          <span className="grid-count">{cameras.length}</span>
        </div>
        <button className="btn-primary btn-sm"
          onClick={() => showForm && !editingId ? (setShowForm(false), resetForm()) : openAddForm()}>
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
              pattern="^[a-zA-Z0-9_-]{2,32}$" placeholder="cam05"
              value={form.camera_id} onChange={e => setForm({ ...form, camera_id: e.target.value })} />
          </div>
          <div className="form-row">
            <label>{t("নাম", "Name")}</label>
            <input required placeholder="Main Gate" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} />
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
              </div>
            </div>
          )}
          {!isCustom && !isPublish && (
            <div className="form-section">
              <div className="form-section-title">{t("ক্যামেরা credentials", "Camera credentials")}</div>
              <div className="form-row">
                <label>IP / Host *</label>
                <input required placeholder="192.168.1.100" value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} />
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
          <WhepPlayer key={cam.camera_id}
            camera={{
              id: cam.camera_id, name: cam.name, streaming: cam.streaming,
              stream_status: cam.stream_status, status_message: cam.status_message,
              connection_mode: cam.connection_mode, brand: cam.brand,
              location: cam.location_name,
              whep: cam.whep_url  || `${WHEP_BASE}/${cam.camera_id}/whep`,
              hls:  cam.hls_url   || `${HLS_BASE}/${cam.camera_id}/index.m3u8`,
              playback_mode: cam.playback_mode,
              webrtc_compatible: cam.webrtc_compatible !== false,
              video_codec: cam.video_codec,
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

// ── WhepPlayer ─────────────────────────────────────────────────────────────────
function WhepPlayer({ camera, language, onEdit, onDelete, onRefresh }) {
  const videoRef  = useRef(null);
  const hlsRef    = useRef(null);
  const timerRef  = useRef(null);
  const [status,     setStatus]     = useState("connecting");
  const [errorMsg,   setErrorMsg]   = useState(camera.status_message || "");
  const [testResult, setTestResult] = useState(null);
  const [testBusy,   setTestBusy]   = useState(false);
  // "live" = raw WebRTC/HLS  |  "ai" = canvas bbox overlay on live  |  "mjpeg" = server annotated
  const [viewMode,   setViewMode]   = useState("live");
  const t = (bn, en) => language === "bn" ? bn : en;

  const showTestResult = (r) => {
    clearTimeout(timerRef.current);
    setTestResult(r);
    timerRef.current = setTimeout(() => setTestResult(null), 6000);
  };
  useEffect(() => () => clearTimeout(timerRef.current), []);

  const runTest = async () => {
    if (testBusy) return;
    setTestBusy(true); setTestResult(null);
    try {
      const r = await fetch(`${VIDEO_INGEST_URL}/cameras/${camera.id}/test`, { method: "POST" });
      const d = await r.json().catch(() => ({}));
      const ok = d.ok === true || (r.ok && d.ok !== false);
      showTestResult({ ok, text: ok ? (d.status_message || "Connection OK") : (d.error || d.status_message || `Error ${r.status}`) });
      if (ok) onRefresh?.();
    } catch (e) { showTestResult({ ok: false, text: e.message || "Network error" }); }
    finally { setTestBusy(false); }
  };

  useEffect(() => {
    setErrorMsg(camera.status_message || "");
    if (camera.stream_status !== "live") {
      setStatus(camera.stream_status === "waiting" ? "waiting" : "error");
      return;
    }
    if (viewMode === "ai" || viewMode === "mjpeg") { setStatus("live"); return; }

    let pc = null, cancelled = false;

    async function connectHls() {
      const video = videoRef.current;
      if (!video || cancelled) return;
      setStatus("connecting");
      try {
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = camera.hls; await video.play();
          if (!cancelled) setStatus("live"); return;
        }
        const { default: Hls } = await import("hls.js");
        if (!Hls.isSupported()) throw new Error("HLS not supported");
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        hlsRef.current = hls;
        hls.loadSource(camera.hls); hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (!cancelled) { video.play().catch(() => {}); setStatus("live"); } });
        hls.on(Hls.Events.ERROR, (_, d) => { if (!cancelled && d.fatal) { setStatus("error"); setErrorMsg("HLS playback failed"); } });
      } catch { if (!cancelled) { setStatus("error"); setErrorMsg("HLS playback failed"); } }
    }

    async function connectWhep() {
      setStatus("connecting");
      pc = new RTCPeerConnection({ iceServers: [] });
      pc.ontrack = e => { if (!cancelled && videoRef.current && e.streams[0]) { videoRef.current.srcObject = e.streams[0]; setStatus("live"); } };
      pc.oniceconnectionstatechange = () => { if (pc?.iceConnectionState === "failed") setStatus("error"); };
      pc.addTransceiver("video", { direction: "recvonly" });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await fetch(camera.whep, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: offer.sdp });
      if (cancelled) return;
      if (resp.ok) {
        await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
      } else {
        const body = await resp.text().catch(() => "");
        if (body.includes("codecs not supported") || camera.playback_mode === "hls") { pc.close(); pc = null; await connectHls(); return; }
        setStatus("error"); setErrorMsg("WHEP failed — click Test (⚡) to diagnose");
      }
    }

    async function connect() {
      if (camera.playback_mode === "hls") await connectHls();
      else { try { await connectWhep(); } catch { if (!cancelled) await connectHls(); } }
    }

    connect();
    return () => {
      cancelled = true; pc?.close();
      hlsRef.current?.destroy(); hlsRef.current = null;
      if (videoRef.current) { videoRef.current.removeAttribute("src"); videoRef.current.srcObject = null; }
    };
  }, [camera.whep, camera.hls, camera.stream_status, camera.status_message, camera.playback_mode, camera.webrtc_compatible, camera.video_codec, viewMode]);

  const statusLabel = {
    live: t("লাইভ","Live"), connecting: t("সংযোগ হচ্ছে…","Connecting…"),
    waiting: t("অপেক্ষমান","Waiting"), error: t("সমস্যা","No video"),
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
          <button type="button"
            className={`btn-ai-toggle ${viewMode !== "live" ? "active" : ""}`}
            title="Cycle: Live → AI Canvas Overlay → MJPEG"
            onClick={() => setViewMode(m => m === "live" ? "ai" : m === "ai" ? "mjpeg" : "live")}>
            {viewMode === "live" ? "AI" : viewMode === "ai" ? "MJPEG" : t("লাইভ","Live")}
          </button>
          <button type="button" className={`btn-icon ${testBusy ? "btn-icon-busy" : ""}`}
            title={t("টেস্ট","Test connection")} onClick={runTest} disabled={testBusy}>
            {testBusy ? "…" : "⚡"}
          </button>
          <button type="button" className="btn-icon" title={t("সম্পাদনা","Edit")} onClick={onEdit}>✎</button>
          <button type="button" className="btn-icon btn-icon-danger" title={t("মুছুন","Delete")} onClick={onDelete}>✕</button>
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

      <div className="video-container" style={{ position: "relative" }}>
        {viewMode === "mjpeg" ? (
          <>
            <img className="camera-video"
              src={`${TRAFFIC_AI_URL}/preview/${camera.id}.mjpg`} alt={`AI ${camera.id}`}
              onError={() => setErrorMsg(t("AI stream পাওয়া যায়নি","AI stream unavailable"))} />
            <span className="ai-overlay-badge">{t("● MJPEG AI","● MJPEG AI")}</span>
          </>
        ) : status !== "live" && status !== "connecting" ? (
          <div className="cam-offline">
            <span className="cam-offline-icon">{status === "waiting" ? "⏳" : "📵"}</span>
            {status === "waiting" ? t("ক্যামেরা push এর অপেক্ষা","Waiting for camera push") : t("ভিডিও নেই","No video")}
          </div>
        ) : (
          <>
            <video ref={videoRef} autoPlay muted playsInline className="camera-video" />
            {viewMode === "ai" && status === "live" && (
              <>
                <DetectionCanvas cameraId={camera.id} videoRef={videoRef} />
                <span className="ai-overlay-badge">{t("● AI ওভারলে","● AI Overlay")}</span>
              </>
            )}
          </>
        )}
        {viewMode === "live" && status === "connecting" && (
          <div className="cam-connecting">
            <span className="cam-offline-icon">📡</span>
            {t("সংযোগ হচ্ছে…","Connecting…")}
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ALERT PANEL
// ═══════════════════════════════════════════════════════════════════════════════
export function AlertPanel({ alerts, language, onAccept, onReject, onEscalate }) {
  const [selected, setSelected] = useState(null);
  const isMobile = useIsMobile(760);
  const t = (bn, en) => language === "bn" ? bn : en;
  const pending = alerts.filter(a => a.status === "pending");

  const handleBack     = () => setSelected(null);
  const handleAccept   = () => { onAccept(selected.alert_id);   setSelected(null); };
  const handleReject   = () => { onReject(selected.alert_id);   setSelected(null); };
  const handleEscalate = () => { onEscalate(selected.alert_id); setSelected(null); };

  if (isMobile) {
    return (
      <div className="alert-panel-mobile">
        {!selected ? (
          <div className="alert-list-panel">
            <div className="list-header">
              <span className="list-title">{t("সতর্কতা তালিকা","Alert list")}</span>
              <span className="list-count">{pending.length} {t("মুলতুবি","pending")}</span>
            </div>
            <div className="alert-list-scroll">
              {pending.length === 0 && <div className="empty-state"><span className="empty-icon">✅</span>{t("কোনো মুলতুবি সতর্কতা নেই","No pending alerts")}</div>}
              {pending.map(a => <AlertCard key={a.alert_id} alert={a} selected={false} language={language} onClick={() => setSelected(a)} />)}
            </div>
          </div>
        ) : (
          <div className="alert-detail-panel">
            <AlertDetail alert={selected} language={language} onBack={handleBack} onAccept={handleAccept} onReject={handleReject} onEscalate={handleEscalate} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="alert-panel">
      <div className="alert-list-panel">
        <div className="list-header">
          <span className="list-title">{t("সতর্কতা তালিকা","Alert list")}</span>
          <span className="list-count">{pending.length} {t("মুলতুবি","pending")}</span>
        </div>
        <div className="alert-list-scroll">
          {pending.length === 0 && <div className="empty-state"><span className="empty-icon">✅</span>{t("কোনো মুলতুবি সতর্কতা নেই","No pending alerts")}</div>}
          {pending.map(a => <AlertCard key={a.alert_id} alert={a} selected={selected?.alert_id === a.alert_id} language={language} onClick={() => setSelected(a)} />)}
        </div>
      </div>
      <div className="alert-detail-panel">
        {!selected
          ? <div className="no-selection"><span className="no-selection-icon">👆</span>{t("বাম থেকে একটি সতর্কতা নির্বাচন করুন","Select an alert from the left to review")}</div>
          : <AlertDetail alert={selected} language={language} onAccept={handleAccept} onReject={handleReject} onEscalate={handleEscalate} />
        }
      </div>
    </div>
  );
}

function AlertCard({ alert, selected, language, onClick }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const sev = SEVERITY[alert.severity];
  return (
    <div className={`alert-card sev-${alert.severity} ${selected ? "selected" : ""}`} onClick={onClick}>
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
      {onBack && <button className="detail-back-btn" onClick={onBack}>← {t("তালিকায় ফিরুন","Back to list")}</button>}
      <div className="detail-header-section">
        <div>
          <div className="detail-type">
            {t(ALERT_LABELS[alert.alert_type]?.bn, ALERT_LABELS[alert.alert_type]?.en) ?? alert.alert_type}
          </div>
          <div className="detail-meta">
            {alert.camera_id} · {alert.location} · {new Date(alert.timestamp).toLocaleString()}
          </div>
        </div>
        {sev && <div className="severity-badge" style={{ background: sev.color, marginLeft: "auto", flexShrink: 0 }}>{t(sev.bn, sev.en)}</div>}
      </div>
      {alert.snapshot_b64 && (
        <div className="snapshot-container">
          <img src={`data:image/jpeg;base64,${alert.snapshot_b64}`} alt="Alert snapshot" className="snapshot-img" />
        </div>
      )}
      <div className="detail-info">
        <div className="info-row"><span>{t("আস্থা","Confidence")}</span><strong>{(alert.confidence * 100).toFixed(1)}%</strong></div>
        <div className="info-row"><span>{t("তীব্রতা","Severity")}</span><strong>L{alert.severity} — {sev?.en}</strong></div>
        {alert.metadata?.vehicle_class && <div className="info-row"><span>{t("যানবাহন","Vehicle")}</span><strong>{alert.metadata.vehicle_class}</strong></div>}
        {alert.metadata?.speed_kmh > 0  && <div className="info-row"><span>{t("গতি","Speed")}</span><strong>{alert.metadata.speed_kmh} km/h</strong></div>}
        {alert.metadata?.plate          && <div className="info-row"><span>{t("নম্বর প্লেট","Plate")}</span><strong className="plate-badge">{alert.metadata.plate}</strong></div>}
        {alert.metadata?.matched_name   && (
          <div className="info-row">
            <span>{t("মিলেছে","Matched")}</span>
            <span><strong className="face-match-name">{alert.metadata.matched_name}</strong><span className="risk-tag">{alert.metadata.risk_category}</span></span>
          </div>
        )}
      </div>
      <div className="ai-disclaimer">⚠ {t("এআই সহায়তা প্রদান করেছে। সিদ্ধান্ত ও দায়িত্ব অফিসারের।","AI assisted only. The decision and responsibility remain with the officer.")}</div>
      <div className="action-buttons">
        <button className="btn-accept"   onClick={onAccept}>  {t("গ্রহণ করুন ✓","Accept ✓")}</button>
        <button className="btn-escalate" onClick={onEscalate}>{t("উর্ধ্বতন ↑",  "Escalate ↑")}</button>
        <button className="btn-reject"   onClick={onReject}>  {t("বাতিল ✗",      "Reject ✗")}</button>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// INCIDENT LIST  (with detail drill-down)
// ═══════════════════════════════════════════════════════════════════════════════
export function IncidentList({ incidents, language, apiUrl }) {
  const [selected,      setSelected]      = useState(null);
  const [detail,        setDetail]        = useState(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const t = (bn, en) => language === "bn" ? bn : en;
  const open = incidents.filter(i => i.status === "open").length;

  const openDetail = async (inc) => {
    setSelected(inc); setLoadingDetail(true);
    try {
      const r = await fetch(`${apiUrl}/incidents/${inc.id}`);
      if (r.ok) setDetail(await r.json());
    } catch {} finally { setLoadingDetail(false); }
  };

  const closeDetail = () => { setSelected(null); setDetail(null); };

  const updateIncident = async (incId, action) => {
    try {
      await fetch(`${apiUrl}/incidents/${incId}/action`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, notes: null }),
      });
      closeDetail();
    } catch {}
  };

  if (selected) {
    return (
      <div className="incident-detail-page">
        <button className="detail-back-btn" onClick={closeDetail}>← {t("ঘটনা তালিকায় ফিরুন","Back to incidents")}</button>
        <div className="inc-detail-header">
          <div className="inc-detail-title">{selected.title}</div>
          <span className={`inc-status ${selected.status}`}>{selected.status}</span>
        </div>
        <div className="inc-detail-meta">
          <span>📍 {selected.location_name || "—"}</span>
          <span>🕐 {new Date(selected.created_at).toLocaleString()}</span>
          <span>L{selected.severity}</span>
        </div>
        {selected.alert_types && (
          <div className="inc-types">
            {selected.alert_types.map(at => <span key={at} className="inc-type-tag">{at}</span>)}
          </div>
        )}
        {selected.status === "open" && (
          <div className="inc-action-row">
            <button className="btn-primary btn-sm"    onClick={() => updateIncident(selected.id,"assigned")}> {t("নিয়োগ করুন","Assign")}</button>
            <button className="btn-secondary btn-sm"  onClick={() => updateIncident(selected.id,"dispatched")}>{t("প্রেরণ করুন","Dispatch")}</button>
            <button className="btn-cancel btn-sm"     onClick={() => updateIncident(selected.id,"closed")}>   {t("বন্ধ করুন","Close")}</button>
          </div>
        )}
        {loadingDetail ? (
          <div className="inc-loading">{t("লোড হচ্ছে…","Loading alerts…")}</div>
        ) : (
          <div className="inc-alerts-section">
            <div className="inc-section-title">{t("সংশ্লিষ্ট সতর্কতা","Linked Alerts")} ({detail?.alerts?.length || 0})</div>
            {(detail?.alerts || []).map(alert => (
              <div key={alert.alert_id} className={`inc-alert-row sev-${alert.severity}`}>
                <span className="severity-badge" style={{ background: SEVERITY[alert.severity]?.color, fontSize: "11px", padding: "2px 6px" }}>L{alert.severity}</span>
                <span className="inc-alert-type">{ALERT_LABELS[alert.alert_type]?.en ?? alert.alert_type}</span>
                <span className="inc-alert-cam">{alert.camera_id}</span>
                <span className="inc-alert-conf">{(alert.confidence * 100).toFixed(0)}%</span>
                <span className="inc-alert-time">{new Date(alert.timestamp).toLocaleTimeString()}</span>
                {alert.snapshot_b64 && <img className="inc-alert-thumb" src={`data:image/jpeg;base64,${alert.snapshot_b64}`} alt="snap" />}
              </div>
            ))}
            {(!detail?.alerts || detail.alerts.length === 0) && (
              <div className="empty-state">{t("কোনো সংশ্লিষ্ট সতর্কতা নেই","No linked alerts")}</div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="incident-list">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ঘটনা তালিকা","Incident cards")}</div>
          <div className="page-sub">{open} {t("খোলা ঘটনা","open incidents")}</div>
        </div>
      </div>
      {incidents.length === 0 && <div className="empty-state"><span className="empty-icon">📋</span>{t("কোনো ঘটনা নেই","No incidents")}</div>}
      {incidents.map(inc => (
        <div key={inc.id} className={`incident-card status-${inc.status}`} onClick={() => openDetail(inc)} style={{ cursor: "pointer" }}>
          <div className="inc-header">
            <div className="inc-title">{inc.title}</div>
            <span className={`inc-status ${inc.status}`}>
              {t({ open: "খোলা", assigned: "নিযুক্ত", dispatched: "প্রেরিত", closed: "বন্ধ" }[inc.status], inc.status)}
            </span>
          </div>
          <div className="inc-meta">
            <span>📍 {inc.location_name || "—"}</span>
            <span>🕐 {new Date(inc.created_at).toLocaleString()}</span>
            <span>L{inc.severity}</span>
          </div>
          {inc.alert_types && <div className="inc-types">{inc.alert_types.map(at => <span key={at} className="inc-type-tag">{at}</span>)}</div>}
        </div>
      ))}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ANALYTICS PAGE  (pure SVG charts — no external library)
// ═══════════════════════════════════════════════════════════════════════════════
export function AnalyticsPage({ language, apiUrl }) {
  const [summary,    setSummary]    = useState(null);
  const [violations, setViolations] = useState([]);
  const [camStats,   setCamStats]   = useState([]);
  const [loading,    setLoading]    = useState(true);
  const t = (bn, en) => language === "bn" ? bn : en;

  useEffect(() => {
    const load = async () => {
      try {
        const [sr, vr, cr] = await Promise.all([
          fetch(`${apiUrl}/analytics/summary`),
          fetch(`${apiUrl}/analytics/violations?hours=24`),
          fetch(`${apiUrl}/analytics/camera-stats?days=7`),
        ]);
        if (sr.ok) setSummary(await sr.json());
        if (vr.ok) setViolations(await vr.json());
        if (cr.ok) setCamStats(await cr.json());
      } catch {} finally { setLoading(false); }
    };
    load();
    const iv = setInterval(load, 60000);
    return () => clearInterval(iv);
  }, [apiUrl]);

  if (loading) return <div className="analytics-loading">{t("লোড হচ্ছে…","Loading analytics…")}</div>;

  // Hourly buckets for line chart
  const hourlyMap = {};
  violations.forEach(v => { const h = v.hour.substring(0,13); hourlyMap[h] = (hourlyMap[h]||0) + v.count; });
  const hours = Array.from({ length: 24 }, (_, i) => {
    const d = new Date(); d.setHours(d.getHours() - (23-i), 0, 0, 0);
    return { label: d.getHours()+":00", count: hourlyMap[d.toISOString().substring(0,13)] || 0 };
  });
  const maxHourly = Math.max(...hours.map(h => h.count), 1);

  const byType  = summary?.by_type || [];
  const maxType = Math.max(...byType.map(r => r.count), 1);

  const camTotals = {};
  camStats.forEach(r => { camTotals[r.camera_id] = (camTotals[r.camera_id]||0) + r.count; });
  const camEntries = Object.entries(camTotals).sort((a,b) => b[1]-a[1]).slice(0,8);
  const maxCam = Math.max(...camEntries.map(([,c]) => c), 1);

  return (
    <div className="analytics-page">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("ট্র্যাফিক বিশ্লেষণ","Traffic Analytics")}</div>
          <div className="page-sub">{t("গত ২৪ ঘণ্টার ডেটা","Last 24 hours data")}</div>
        </div>
      </div>

      {/* Summary KPI cards */}
      <div className="analytics-summary-cards">
        <div className="analytics-card">
          <div className="analytics-card-value">{summary?.total_today ?? 0}</div>
          <div className="analytics-card-label">{t("আজকের মোট সতর্কতা","Total Alerts Today")}</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-card-value" style={{color:"#ef4444"}}>{summary?.pending ?? 0}</div>
          <div className="analytics-card-label">{t("মুলতুবি সতর্কতা","Pending Alerts")}</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-card-value" style={{color:"#f59e0b",fontSize:"14px"}}>{byType[0]?.type?.replace(/_/g," ") ?? "—"}</div>
          <div className="analytics-card-label">{t("সর্বোচ্চ লঙ্ঘন","Top Violation Type")}</div>
        </div>
        <div className="analytics-card">
          <div className="analytics-card-value" style={{color:"#10b981",fontSize:"14px"}}>{camEntries[0]?.[0] ?? "—"}</div>
          <div className="analytics-card-label">{t("সর্বোচ্চ সতর্ক ক্যামেরা","Most Active Camera")}</div>
        </div>
      </div>

      <div className="analytics-charts-grid">
        {/* Hourly line chart */}
        <div className="analytics-chart-box">
          <div className="chart-title">{t("ঘণ্টা অনুযায়ী সতর্কতা","Alerts by Hour — last 24h")}</div>
          <svg viewBox="0 0 480 110" className="line-chart" preserveAspectRatio="none">
            <defs>
              <linearGradient id="lineGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3b82f6" stopOpacity="0.3"/>
                <stop offset="100%" stopColor="#3b82f6" stopOpacity="0"/>
              </linearGradient>
            </defs>
            {[0,0.25,0.5,0.75,1].map(f => (
              <line key={f} x1="0" y1={95-f*85} x2="480" y2={95-f*85} stroke="#1f2937" strokeWidth="1" strokeDasharray="4,4"/>
            ))}
            <path d={`M 0 95 ${hours.map((h,i)=>`L ${i*(480/23)} ${95-(h.count/maxHourly)*85}`).join(" ")} L 480 95 Z`} fill="url(#lineGrad)"/>
            <polyline points={hours.map((h,i)=>`${i*(480/23)},${95-(h.count/maxHourly)*85}`).join(" ")} fill="none" stroke="#3b82f6" strokeWidth="2" strokeLinejoin="round"/>
            {hours.map((h,i) => h.count > 0 && <circle key={i} cx={i*(480/23)} cy={95-(h.count/maxHourly)*85} r="3" fill="#3b82f6"/>)}
          </svg>
          <div className="chart-x-labels">
            {hours.filter((_,i)=>i%4===0).map((h,i)=><span key={i}>{h.label}</span>)}
          </div>
        </div>

        {/* Violations by type */}
        <div className="analytics-chart-box">
          <div className="chart-title">{t("লঙ্ঘনের ধরন","Violations by Type — today")}</div>
          {byType.length === 0 && <div className="chart-empty">{t("কোনো ডেটা নেই","No data yet")}</div>}
          <div className="hbar-chart">
            {byType.slice(0,8).map((row,i) => (
              <div key={row.type} className="hbar-row">
                <span className="hbar-label">{row.type.replace(/_/g," ")}</span>
                <div className="hbar-track">
                  <div className="hbar-fill" style={{width:`${(row.count/maxType)*100}%`,background:CHART_COLORS[i%CHART_COLORS.length]}}/>
                </div>
                <span className="hbar-value">{row.count}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Per-camera activity */}
        <div className="analytics-chart-box">
          <div className="chart-title">{t("ক্যামেরা অনুযায়ী সতর্কতা","Alerts by Camera — 7 days")}</div>
          {camEntries.length === 0 && <div className="chart-empty">{t("কোনো ডেটা নেই","No data yet")}</div>}
          <div className="hbar-chart">
            {camEntries.map(([camId,count],i) => (
              <div key={camId} className="hbar-row">
                <span className="hbar-label">{camId}</span>
                <div className="hbar-track">
                  <div className="hbar-fill" style={{width:`${(count/maxCam)*100}%`,background:CHART_COLORS[i%CHART_COLORS.length]}}/>
                </div>
                <span className="hbar-value">{count}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Severity breakdown */}
        <div className="analytics-chart-box">
          <div className="chart-title">{t("তীব্রতা বিতরণ","Severity Distribution — today")}</div>
          <div className="severity-breakdown">
            {(summary?.by_severity || []).map(row => {
              const sev = SEVERITY[row.severity];
              const pct = summary?.total_today ? Math.round((row.count/summary.total_today)*100) : 0;
              return (
                <div key={row.severity} className="sev-row">
                  <span className="sev-dot" style={{background: sev?.color}}/>
                  <span className="sev-name">{sev?.en ?? `L${row.severity}`}</span>
                  <div className="hbar-track">
                    <div className="hbar-fill" style={{width:`${pct}%`,background:sev?.color}}/>
                  </div>
                  <span className="sev-count">{row.count} <span className="sev-pct">({pct}%)</span></span>
                </div>
              );
            })}
            {(!summary?.by_severity || summary.by_severity.length === 0) && <div className="chart-empty">{t("কোনো ডেটা নেই","No data yet")}</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// EVIDENCE PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export function EvidencePage({ language, apiUrl }) {
  const [evidence,   setEvidence]   = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [filterCam,  setFilterCam]  = useState("");
  const [filterType, setFilterType] = useState("");
  const [selected,   setSelected]   = useState(null);
  const t = (bn, en) => language === "bn" ? bn : en;

  useEffect(() => {
    const params = new URLSearchParams({ limit: 100 });
    if (filterCam)  params.set("camera_id",  filterCam);
    if (filterType) params.set("alert_type", filterType);
    setLoading(true);
    fetch(`${apiUrl}/evidence?${params}`)
      .then(r => r.ok ? r.json() : [])
      .then(d => { setEvidence(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [apiUrl, filterCam, filterType]);

  const allCams  = [...new Set(evidence.map(e => e.camera_id))].filter(Boolean);
  const allTypes = [...new Set(evidence.map(e => e.alert_type))].filter(Boolean);

  const downloadSnapshot = (ev) => {
    if (!ev.snapshot_b64) return;
    const a = document.createElement("a");
    a.href     = `data:image/jpeg;base64,${ev.snapshot_b64}`;
    a.download = `evidence_${ev.camera_id}_${ev.alert_id.substring(0,8)}.jpg`;
    a.click();
  };

  if (selected) {
    const sev = SEVERITY[selected.severity];
    return (
      <div className="evidence-detail">
        <button className="detail-back-btn" onClick={() => setSelected(null)}>← {t("প্রমাণ তালিকায় ফিরুন","Back to evidence")}</button>
        <div className="evidence-detail-header">
          <div className="evidence-detail-title">{ALERT_LABELS[selected.alert_type]?.en ?? selected.alert_type}</div>
          <div className="severity-badge" style={{background:sev?.color}}>L{selected.severity} {sev?.en}</div>
        </div>
        <div className="evidence-detail-meta">
          <span>📷 {selected.camera_id}</span>
          <span>📍 {selected.location || "—"}</span>
          <span>🕐 {new Date(selected.timestamp).toLocaleString()}</span>
          <span>{t("আস্থা","Conf")}: {(selected.confidence*100).toFixed(1)}%</span>
        </div>
        {selected.snapshot_b64 && (
          <div className="evidence-img-container">
            <img src={`data:image/jpeg;base64,${selected.snapshot_b64}`} alt="Evidence" className="evidence-full-img"/>
          </div>
        )}
        {selected.metadata && Object.keys(selected.metadata).length > 0 && (
          <div className="evidence-metadata">
            <div className="evidence-meta-title">{t("অতিরিক্ত তথ্য","Additional metadata")}</div>
            {Object.entries(selected.metadata).map(([k,v]) => v != null && (
              <div key={k} className="info-row">
                <span>{k.replace(/_/g," ")}</span><strong>{String(v)}</strong>
              </div>
            ))}
          </div>
        )}
        <div className="evidence-actions">
          <button className="btn-primary" onClick={() => downloadSnapshot(selected)}>
            ⬇ {t("ডাউনলোড করুন","Download Image")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="evidence-page">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("প্রমাণ ব্যবস্থাপনা","Evidence Management")}</div>
          <div className="page-sub">{t("সব স্ন্যাপশট এবং প্রমাণ চিত্র","All alert snapshots and evidence images")}</div>
        </div>
      </div>
      <div className="evidence-filters">
        <select value={filterCam}  onChange={e => setFilterCam(e.target.value)}  className="evidence-filter-select">
          <option value="">{t("সব ক্যামেরা","All cameras")}</option>
          {allCams.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        <select value={filterType} onChange={e => setFilterType(e.target.value)} className="evidence-filter-select">
          <option value="">{t("সব ধরন","All violation types")}</option>
          {allTypes.map(type => <option key={type} value={type}>{ALERT_LABELS[type]?.en ?? type}</option>)}
        </select>
        <span className="evidence-count">{evidence.length} {t("টি প্রমাণ","items")}</span>
      </div>
      {loading && <div className="analytics-loading">{t("লোড হচ্ছে…","Loading…")}</div>}
      {!loading && evidence.length === 0 && (
        <div className="empty-state"><span className="empty-icon">🖼️</span>{t("কোনো স্ন্যাপশট পাওয়া যায়নি","No snapshots found")}</div>
      )}
      <div className="evidence-grid">
        {evidence.map(ev => {
          const sev = SEVERITY[ev.severity];
          return (
            <div key={ev.alert_id} className="evidence-card" onClick={() => setSelected(ev)}>
              {ev.snapshot_b64
                ? <img src={`data:image/jpeg;base64,${ev.snapshot_b64}`} alt="evidence" className="evidence-thumb"/>
                : <div className="evidence-no-img">🖼️</div>
              }
              <div className="evidence-card-info">
                <div className="evidence-card-type">{ALERT_LABELS[ev.alert_type]?.en ?? ev.alert_type}</div>
                <div className="evidence-card-meta">
                  <span className="severity-badge" style={{background:sev?.color,fontSize:"10px",padding:"2px 5px"}}>L{ev.severity}</span>
                  <span>{ev.camera_id}</span>
                </div>
                <div className="evidence-card-time">{new Date(ev.timestamp).toLocaleString()}</div>
                <div className="evidence-card-conf">{(ev.confidence*100).toFixed(0)}%</div>
              </div>
              <button className="evidence-download-btn" title="Download"
                onClick={e => { e.stopPropagation(); downloadSnapshot(ev); }}>⬇</button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// ADMIN / SYSTEM HEALTH PAGE
// ═══════════════════════════════════════════════════════════════════════════════
export function AdminPage({ language, alertApiUrl, videoIngestUrl, trafficAiUrl }) {
  const [health, setHealth] = useState({});
  const t = (bn, en) => language === "bn" ? bn : en;

  const probe = useCallback(async () => {
    const services = [
      { key: "alert_service", url: alertApiUrl,    label: "Alert Service (:8004)" },
      { key: "video_ingest",  url: videoIngestUrl, label: "Video Ingest (:8001)" },
      { key: "traffic_ai",    url: trafficAiUrl,   label: "Traffic AI (:8002)" },
    ];
    const results = {};
    await Promise.all(services.map(async ({ key, url, label }) => {
      try {
        const r = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
        const d = await r.json().catch(() => ({}));
        results[key] = { ok: r.ok, label, ...d };
      } catch (e) { results[key] = { ok: false, label, error: e.message }; }
    }));
    setHealth(results);
  }, [alertApiUrl, videoIngestUrl, trafficAiUrl]);

  useEffect(() => { probe(); const iv = setInterval(probe, 15000); return () => clearInterval(iv); }, [probe]);

  return (
    <div className="admin-page">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("সিস্টেম অ্যাডমিন","System Administration")}</div>
          <div className="page-sub">{t("সার্ভিস স্বাস্থ্য ও কনফিগারেশন","Service health and configuration")}</div>
        </div>
        <button className="btn-secondary btn-sm" onClick={probe}>{t("রিফ্রেশ","Refresh")}</button>
      </div>

      <div className="admin-services">
        <div className="admin-section-title">{t("সার্ভিস স্বাস্থ্য","Service Health")}</div>
        {Object.entries(health).map(([key, s]) => (
          <div key={key} className={`admin-service-card ${s.ok ? "service-ok" : "service-err"}`}>
            <div className="service-header">
              <span className={`service-dot ${s.ok ? "dot-ok" : "dot-err"}`}/>
              <span className="service-name">{s.label}</span>
              <span className={`service-badge ${s.ok ? "badge-ok" : "badge-err"}`}>
                {s.ok ? t("চলছে","Running") : t("বন্ধ","Down")}
              </span>
            </div>
            {s.ok && (
              <div className="service-details">
                {s.frames_processed != null && <span>Frames processed: {s.frames_processed}</span>}
                {s.alerts_generated != null && <span>Alerts generated: {s.alerts_generated}</span>}
                {s.motion_skipped   != null && <span>Motion-skipped frames: {s.motion_skipped}</span>}
                {s.frame_source     != null && <span>Frame source: {s.frame_source}</span>}
                {s.model_format     != null && <span>AI model: {s.model_format}</span>}
                {s.ws_connections   != null && <span>WS connections: {s.ws_connections}</span>}
                {s.total_alerts     != null && <span>Total alerts in DB: {s.total_alerts}</span>}
                {s.active_cameras   != null && <span>Active cameras: {s.active_cameras}</span>}
              </div>
            )}
            {!s.ok && s.error && <div className="service-error">{s.error}</div>}
          </div>
        ))}
      </div>

      <div className="admin-info">
        <div className="admin-section-title">{t("সার্ভিস Endpoints","Service Endpoints")}</div>
        <div className="admin-urls">
          {[
            ["Alert Service",  alertApiUrl],
            ["Video Ingest",   videoIngestUrl],
            ["Traffic AI",     trafficAiUrl],
            ["WHEP Streaming", WHEP_BASE],
            ["HLS Streaming",  HLS_BASE],
            ["Detection WS",   `${TRAFFIC_AI_WS}/detections/{cam}/ws`],
          ].map(([label, url]) => (
            <div key={label} className="admin-url-row">
              <span className="admin-url-label">{label}</span>
              <code className="admin-url-value">{url}</code>
            </div>
          ))}
        </div>
      </div>

      <div className="admin-info">
        <div className="admin-section-title">{t("লাইভ স্ট্রিমিং আর্কিটেকচার","Live Streaming Architecture")}</div>
        <pre className="admin-arch-diagram">{`
RTSP Camera ──► MediaMTX (:8554)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    WHEP (:8889)  HLS (:8888) RTSP relay
    Browser         Browser    traffic-ai worker
    <video>         fallback        │
    ~100ms          ~2-5s    YOLOv8 + DeepSORT
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                  MJPEG preview         Detection WS
                 /preview/cam1    /detections/cam1/ws
                 <img> tag        Canvas overlay on <video>
                 server-annotated  client-drawn bboxes`.trim()}</pre>
      </div>
    </div>
  );
}
