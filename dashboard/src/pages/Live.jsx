import { useEffect, useState } from "react";
import { WhepPlayer } from "../components/player";
import { VIDEO_INGEST_URL, toPlayerCamera } from "../lib/constants";
import { IconEmpty, IconX } from "../lib/icons";

function CameraDetail({ camera, language, onBack }) {
  const [zones, setZones] = useState([]);
  const t = (bn, en) => language === "bn" ? bn : en;
  const playerCam = toPlayerCamera(camera);

  useEffect(() => {
    let cancelled = false;
    fetch(`${VIDEO_INGEST_URL}/cameras/${camera.camera_id}/zones`)
      .then(r => (r.ok ? r.json() : []))
      .then(z => { if (!cancelled) setZones(Array.isArray(z) ? z : []); })
      .catch(() => { if (!cancelled) setZones([]); });
    return () => { cancelled = true; };
  }, [camera.camera_id]);

  return (
    <div className="camera-detail">
      <div className="page-heading">
        <div>
          <div className="page-title">{camera.name || camera.camera_id}</div>
          <div className="page-sub">{camera.camera_id}</div>
        </div>
        <button type="button" className="btn-secondary btn-sm" onClick={onBack} aria-label={t("ফিরে যান","Back to live wall")}>
          <IconX /> {t("ফিরে যান","Back")}
        </button>
      </div>
      <div className="camera-detail-body">
        <WhepPlayer camera={playerCam} language={language} />
        <aside className="camera-detail-meta">
          <dl className="meta-list">
            <div><dt>{t("অবস্থান","Location")}</dt><dd>{camera.location_name || "—"}</dd></div>
            <div><dt>{t("স্ট্যাটাস","Status")}</dt><dd>{camera.stream_status || (camera.streaming ? "live" : "offline")}</dd></div>
            <div><dt>{t("ব্র্যান্ড","Brand")}</dt><dd>{camera.brand || "—"}</dd></div>
            <div><dt>{t("মোড","Mode")}</dt><dd>{camera.connection_mode || "—"}</dd></div>
            <div><dt>{t("AI FPS","Inference FPS")}</dt><dd>{camera.inference_fps ?? t("ডিফল্ট","Default")}</dd></div>
            <div><dt>{t("কোডেক","Codec")}</dt><dd>{camera.video_codec || "—"}</dd></div>
          </dl>
          <div className="meta-zones">
            <div className="admin-section-title">{t("জোন","Zones")}</div>
            {zones.length === 0 && <p className="form-hint">{t("কোনো জোন নেই","No zones configured")}</p>}
            {zones.map(z => (
              <div key={z.id || z.name} className="zone-chip">
                <span>{z.name || z.id}</span>
                <span className="zone-type">{z.zone_type}</span>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

export function LivePage({ cameras, language }) {
  const [selected, setSelected] = useState(null);
  const t = (bn, en) => language === "bn" ? bn : en;

  if (selected) {
    return <CameraDetail camera={selected} language={language} onBack={() => setSelected(null)} />;
  }

  return (
    <div className="live-page">
      <div className="page-heading">
        <div>
          <div className="page-title">{t("লাইভ ওয়াল","Live wall")}</div>
          <div className="page-sub">{t("ক্যামেরায় ক্লিক করে বিস্তারিত খুলুন","Open a camera for stream, location, FPS, and zones")}</div>
        </div>
        <span className="grid-count">{cameras.length}</span>
      </div>
      <div className="live-wall">
        {cameras.length === 0 && (
          <div className="empty-state" style={{ gridColumn: "1/-1" }}>
            <span className="empty-icon"><IconEmpty /></span>
            {t("কোনো ক্যামেরা নেই","No cameras configured")}
          </div>
        )}
        {cameras.map(cam => (
          <WhepPlayer
            key={cam.camera_id}
            compact
            camera={toPlayerCamera(cam)}
            language={language}
            onOpen={() => setSelected(cam)}
          />
        ))}
      </div>
    </div>
  );
}
