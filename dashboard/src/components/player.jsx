import { useState, useRef, useEffect } from "react";
import { VIDEO_INGEST_URL, TRAFFIC_AI_URL, TRAFFIC_AI_WS } from "../lib/constants";
import { authMediaUrl } from "../lib/auth";
import { markOverlayFrame } from "../lib/perf";
import { IconBolt, IconPencil, IconX, IconCameraOff } from "../lib/icons";

const DEFAULT_LAYERS = { boxes: true, labels: true, counts: true, zones: false };

function DetectionCanvas({ cameraId, videoRef, layers = DEFAULT_LAYERS }) {
  const canvasRef = useRef(null);
  const layersRef = useRef(layers);
  const zonesRef = useRef([]);
  layersRef.current = layers;

  useEffect(() => {
    if (!layers.zones || !cameraId) { zonesRef.current = []; return; }
    let cancelled = false;
    fetch(`${VIDEO_INGEST_URL}/cameras/${cameraId}/zones`)
      .then(r => (r.ok ? r.json() : []))
      .then(z => { if (!cancelled) zonesRef.current = Array.isArray(z) ? z : []; })
      .catch(() => { if (!cancelled) zonesRef.current = []; });
    return () => { cancelled = true; };
  }, [cameraId, layers.zones]);

  useEffect(() => {
    if (!cameraId) return;
    const url = authMediaUrl(`${TRAFFIC_AI_WS}/detections/${cameraId}/ws`);
    let ws, reconnectTimer, pingTimer, raf = 0, latest = null;

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
        latest = data;
        if (!raf) {
          raf = requestAnimationFrame(() => {
            raf = 0;
            if (latest) {
              markOverlayFrame();
              draw(latest);
            }
          });
        }
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        reconnectTimer = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    }

    function drawZones(ctx, W, H) {
      (zonesRef.current || []).forEach(z => {
        const restricted = z.zone_type === "restricted";
        const stroke = restricted ? "rgba(239,68,68,0.9)" : "rgba(59,130,246,0.85)";
        const fill = restricted ? "rgba(239,68,68,0.12)" : "rgba(59,130,246,0.08)";
        if (z.polygon_points?.length >= 3) {
          ctx.beginPath();
          z.polygon_points.forEach((p, i) => {
            const x = p[0] * W, y = p[1] * H;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          });
          ctx.closePath();
          ctx.fillStyle = fill;
          ctx.fill();
          ctx.strokeStyle = stroke;
          ctx.lineWidth = 1.5;
          ctx.setLineDash([6, 4]);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        if (z.line_points?.length >= 2) {
          ctx.beginPath();
          ctx.moveTo(z.line_points[0][0] * W, z.line_points[0][1] * H);
          ctx.lineTo(z.line_points[1][0] * W, z.line_points[1][1] * H);
          ctx.strokeStyle = "#f59e0b";
          ctx.lineWidth = 2;
          ctx.stroke();
        }
      });
    }

    function draw(data) {
      const canvas = canvasRef.current;
      const video  = videoRef?.current;
      if (!canvas) return;
      const L = layersRef.current || DEFAULT_LAYERS;
      const W = video?.clientWidth  || 640;
      const H = video?.clientHeight || 360;
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      const ctx  = canvas.getContext("2d");
      const srcW = data.frame_w || 640;
      const srcH = data.frame_h || 360;
      const sx = W / srcW;
      const sy = H / srcH;
      ctx.clearRect(0, 0, W, H);
      if (L.zones) drawZones(ctx, W, H);

      if (L.boxes || L.labels) {
        (data.detections || []).forEach(det => {
          const [x1, y1, x2, y2] = det.bbox || [];
          const rx1 = x1 * sx, ry1 = y1 * sy, rw = (x2 - x1) * sx, rh = (y2 - y1) * sy;
          const isV  = !!det.violation;
          const col  = isV ? "#ef4444" : "#22c55e";
          if (L.boxes) {
            ctx.strokeStyle = col;
            ctx.lineWidth   = isV ? 3 : 1.5;
            ctx.strokeRect(rx1, ry1, rw, rh);
          }
          if (!L.labels) return;
          const label = isV
            ? `${det.violation.replace(/_/g," ")} [${det.class || det.object_type}]${det.track_id != null ? " #"+det.track_id : ""}`
            : `${det.class || det.object_type} ${(det.confidence*100).toFixed(0)}%${det.track_id != null ? " #"+det.track_id : ""}`;
          const dwell = det.attributes && det.attributes.dwell_ms;
          const dwellLabel = dwell >= 1000 ? ` ${Math.round(dwell/1000)}s` : "";
          ctx.font = "11px 'Fira Code', monospace";
          const tw = ctx.measureText(label + dwellLabel).width + 8;
          const ly = ry1 > 18 ? ry1 - 16 : ry1 + rh;
          ctx.fillStyle = col;
          ctx.fillRect(rx1, ly, tw, 16);
          ctx.fillStyle = isV ? "#fff" : "#020617";
          ctx.fillText(label + dwellLabel, rx1 + 4, ly + 11);
        });
      }

      if (L.counts) {
        const n = (data.detections || []).length;
        const counts = data.counts || {};
        let banner = n > 0 ? `${n} object${n > 1 ? "s" : ""}` : "";
        if (counts.entered || counts.exited) {
          banner += `${banner ? "  " : ""}in:${counts.entered || 0} out:${counts.exited || 0}`;
        }
        if (banner) {
          ctx.font = "bold 11px 'Fira Code', monospace";
          ctx.fillStyle = "rgba(2,6,23,0.75)";
          const bw = Math.min(W - 8, Math.max(130, ctx.measureText(banner).width + 16));
          ctx.fillRect(4, 4, bw, 18);
          ctx.fillStyle = "#22c55e";
          ctx.fillText(banner, 8, 16);
        }
      }
    }

    connect();
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(pingTimer);
      clearTimeout(reconnectTimer);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }, [cameraId]);

  return (
    <canvas ref={canvasRef} aria-hidden="true" style={{
      position: "absolute", top: 0, left: 0,
      width: "100%", height: "100%",
      pointerEvents: "none", zIndex: 2,
    }} />
  );
}

function LayerToggles({ layers, setLayers, language }) {
  const t = (bn, en) => language === "bn" ? bn : en;
  const items = [
    ["boxes",  t("বক্স","Boxes")],
    ["labels", t("লেবেল","Labels")],
    ["counts", t("গণনা","Counts")],
    ["zones",  t("জোন","Zones")],
  ];
  return (
    <div className="layer-toggles" role="group" aria-label={t("ওভারলে স্তর","Overlay layers")}>
      {items.map(([key, label]) => (
        <button
          key={key}
          type="button"
          className={`layer-toggle ${layers[key] ? "on" : ""}`}
          aria-pressed={layers[key]}
          onClick={() => setLayers(l => ({ ...l, [key]: !l[key] }))}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function WhepPlayer({
  camera, language, onEdit, onDelete, onRefresh, compact = false, onOpen,
}) {
  const videoRef  = useRef(null);
  const hlsRef    = useRef(null);
  const timerRef  = useRef(null);
  const [status,     setStatus]     = useState("connecting");
  const [errorMsg,   setErrorMsg]   = useState(camera.status_message || "");
  const [testResult, setTestResult] = useState(null);
  const [testBusy,   setTestBusy]   = useState(false);
  const [viewMode,   setViewMode]   = useState("live");
  const [layers,     setLayers]     = useState(DEFAULT_LAYERS);
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
    if (camera.stream_status === "waiting") {
      setStatus("waiting");
      return;
    }
    if (viewMode === "mjpeg") { setStatus("live"); return; }

    let pc = null, cancelled = false;

    function viewFallback(url) {
      if (!url || !camera.id || String(url).includes(`${camera.id}_view`)) return null;
      return String(url).replace(`/${camera.id}/`, `/${camera.id}_view/`);
    }

    async function connectHls(src) {
      const video = videoRef.current;
      if (!video || cancelled) return;
      setStatus("connecting");
      try {
        if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = src; await video.play();
          if (!cancelled) setStatus("live"); return;
        }
        const { default: Hls } = await import("hls.js");
        if (!Hls.isSupported()) throw new Error("HLS not supported");
        const hls = new Hls({ enableWorker: true, lowLatencyMode: false });
        hlsRef.current = hls;
        hls.loadSource(src); hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (!cancelled) { video.play().catch(() => {}); setStatus("live"); } });
        hls.on(Hls.Events.ERROR, (_, d) => { if (!cancelled && d.fatal) { setStatus("error"); setErrorMsg("HLS playback failed"); } });
      } catch { if (!cancelled) { setStatus("error"); setErrorMsg("HLS playback failed"); } }
    }

    async function whepOnce(url) {
      pc?.close();
      pc = new RTCPeerConnection({ iceServers: [] });
      pc.ontrack = e => { if (!cancelled && videoRef.current && e.streams[0]) { videoRef.current.srcObject = e.streams[0]; setStatus("live"); } };
      pc.oniceconnectionstatechange = () => { if (pc?.iceConnectionState === "failed") setStatus("error"); };
      pc.addTransceiver("video", { direction: "recvonly" });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: offer.sdp });
      const body = resp.ok ? await resp.text() : await resp.text().catch(() => "");
      if (!resp.ok) {
        pc.close(); pc = null;
        return { ok: false, body };
      }
      await pc.setRemoteDescription({ type: "answer", sdp: body });
      return { ok: true };
    }

    async function connectWhep() {
      setStatus("connecting");
      const urls = [camera.whep, viewFallback(camera.whep)].filter(Boolean);
      let last = "";
      for (const url of urls) {
        if (cancelled) return;
        const r = await whepOnce(url);
        if (r.ok) return;
        last = r.body || "";
        if (last.includes("payload type not found") || last.includes("codecs not supported")) continue;
        break;
      }
      pc?.close(); pc = null;
      await connectHls(viewFallback(camera.hls) || camera.hls);
    }

    async function connect() {
      if (camera.playback_mode === "hls") await connectHls(camera.hls);
      else { try { await connectWhep(); } catch { if (!cancelled) await connectHls(camera.hls); } }
    }

    connect();
    return () => {
      cancelled = true; pc?.close();
      hlsRef.current?.destroy(); hlsRef.current = null;
      if (videoRef.current) { videoRef.current.removeAttribute("src"); videoRef.current.srcObject = null; }
    };
  }, [camera.whep, camera.hls, camera.stream_status, camera.status_message, camera.playback_mode, camera.webrtc_compatible, camera.video_codec, viewMode === "mjpeg"]);

  const statusLabel = {
    live: t("লাইভ","Live"), connecting: t("সংযোগ হচ্ছে…","Connecting…"),
    waiting: t("অপেক্ষমান","Waiting"), error: t("সমস্যা","No video"),
  }[status] || status;

  return (
    <div className={`camera-cell ${compact ? "compact" : ""}`}>
      <div className="camera-label">
        <span className={`cam-status-dot ${status}`} />
        <span className="camera-title-group">
          <span className="camera-name">{camera.name}</span>
          <span className="cam-id">{camera.id}</span>
          {!compact && (
            <>
              <span className={`cam-mode-badge mode-${camera.connection_mode}`}>
                {camera.connection_mode === "publish" ? "Push" : "Pull"}
              </span>
              <span className={`cam-status-badge status-${status}`}>{statusLabel}</span>
            </>
          )}
        </span>
        <span className="camera-actions">
          <select
            className="view-mode-select"
            aria-label={t("ভিউ মোড","View mode")}
            value={viewMode}
            onChange={e => setViewMode(e.target.value)}
          >
            <option value="live">{t("লাইভ","Live")}</option>
            <option value="overlay">{t("ওভারলে","Overlay")}</option>
            <option value="mjpeg">MJPEG</option>
          </select>
          {onOpen && (
            <button type="button" className="btn-sm" aria-label={t("বিস্তারিত","Open camera detail")} onClick={() => onOpen(camera)}>
              {t("বিস্তারিত","Open")}
            </button>
          )}
          {!compact && onRefresh && (
            <button type="button" className={`btn-icon ${testBusy ? "btn-icon-busy" : ""}`}
              aria-label={t("টেস্ট","Test connection")} onClick={runTest} disabled={testBusy}>
              <IconBolt />
            </button>
          )}
          {!compact && onEdit && (
            <button type="button" className="btn-icon" aria-label={t("সম্পাদনা","Edit")} onClick={onEdit}><IconPencil /></button>
          )}
          {!compact && onDelete && (
            <button type="button" className="btn-icon btn-icon-danger" aria-label={t("মুছুন","Delete")} onClick={onDelete}><IconX /></button>
          )}
        </span>
      </div>

      {viewMode === "overlay" && <LayerToggles layers={layers} setLayers={setLayers} language={language} />}

      {testResult && (
        <div className={`cam-test-result ${testResult.ok ? "cam-test-ok" : "cam-test-err"}`} role={testResult.ok ? "status" : "alert"}>
          {testResult.ok ? t("ঠিক আছে","OK") : t("ত্রুটি","Error")} {testResult.text}
          <button className="cam-test-close" aria-label={t("বন্ধ","Close")} onClick={() => setTestResult(null)}><IconX /></button>
        </div>
      )}
      {!testResult && (status === "error" || status === "waiting" || camera.video_codec === "H265") && errorMsg && (
        <div className="cam-status-msg" role="status">{errorMsg}</div>
      )}

      <div className="video-container" style={{ position: "relative" }}>
        {viewMode === "mjpeg" ? (
          <>
            <img className="camera-video"
              src={authMediaUrl(`${TRAFFIC_AI_URL}/preview/${camera.id}.mjpg`)}
              alt={`${camera.name || camera.id} annotated preview`}
              onError={() => setErrorMsg(t("AI stream পাওয়া যায়নি","AI stream unavailable"))} />
            <span className="ai-overlay-badge">MJPEG</span>
          </>
        ) : status !== "live" && status !== "connecting" ? (
          <div className="cam-offline">
            <span className="cam-offline-icon"><IconCameraOff /></span>
            {status === "waiting" ? t("ক্যামেরা push এর অপেক্ষা","Waiting for camera push") : t("ভিডিও নেই","No video")}
          </div>
        ) : (
          <>
            <video ref={videoRef} autoPlay muted playsInline className="camera-video" />
            {viewMode === "overlay" && status === "live" && (
              <>
                <DetectionCanvas cameraId={camera.id} videoRef={videoRef} layers={layers} />
                <span className="ai-overlay-badge">{t("ওভারলে","Overlay")}</span>
              </>
            )}
          </>
        )}
        {viewMode !== "mjpeg" && status === "connecting" && (
          <div className="cam-connecting">{t("সংযোগ হচ্ছে…","Connecting…")}</div>
        )}
      </div>
    </div>
  );
}

export { DetectionCanvas };
