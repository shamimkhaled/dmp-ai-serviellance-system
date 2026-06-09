// dashboard/src/hooks/index.js

import { useState, useEffect, useCallback, useRef } from "react";

// ── useAlerts ─────────────────────────────────────────────────────────────────
export function useAlerts(apiUrl, officerId) {
  const [alerts,    setAlerts]    = useState([]);
  const [incidents, setIncidents] = useState([]);

  useEffect(() => {
    fetchAlerts();
    fetchIncidents();
    const iv = setInterval(() => { fetchAlerts(); fetchIncidents(); }, 30000);
    return () => clearInterval(iv);
  }, [apiUrl]);

  async function fetchAlerts() {
    try {
      const r = await fetch(`${apiUrl}/alerts?limit=200`);
      setAlerts(await r.json());
    } catch {}
  }

  async function fetchIncidents() {
    try {
      const r = await fetch(`${apiUrl}/incidents?limit=100`);
      setIncidents(await r.json());
    } catch {}
  }

  const handleWsMessage = useCallback((message) => {
    if (message.type === "new_alert" || message.type === "alert_catchup") {
      setAlerts(prev => {
        if (prev.find(a => a.alert_id === message.alert_id)) return prev;
        return [message, ...prev].slice(0, 500);
      });
    }
    if (message.type === "alert_updated") {
      setAlerts(prev =>
        prev.map(a => a.alert_id === message.alert_id ? { ...a, status: message.status } : a)
      );
    }
  }, []);

  async function performAction(alertId, action, notes = null) {
    try {
      await fetch(`${apiUrl}/alerts/${alertId}/action`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ action, notes, officer_id: officerId }),
      });
      setAlerts(prev => prev.map(a => a.alert_id === alertId ? { ...a, status: action } : a));
    } catch {}
  }

  return {
    alerts, incidents, handleWsMessage,
    acceptAlert:   (id) => performAction(id, "accepted"),
    rejectAlert:   (id) => performAction(id, "rejected"),
    escalateAlert: (id) => performAction(id, "escalated"),
    closeAlert:    (id) => performAction(id, "closed"),
    refreshAlerts: fetchAlerts,
    refreshIncidents: fetchIncidents,
  };
}

// ── useWebSocket ──────────────────────────────────────────────────────────────
export function useWebSocket(url) {
  const [connected,   setConnected]   = useState(false);
  const [lastMessage, setLastMessage] = useState(null);
  const wsRef        = useRef(null);
  const reconnectRef = useRef(null);
  const activeRef    = useRef(false);

  useEffect(() => {
    activeRef.current = true;

    function connect() {
      if (!activeRef.current) return;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (!activeRef.current) { ws.close(); return; }
        setConnected(true);
        ws._ping = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 25000);
      };

      ws.onmessage = (event) => {
        if (event.data === "pong") return;
        try { setLastMessage(JSON.parse(event.data)); } catch {}
      };

      ws.onclose = () => {
        setConnected(false);
        clearInterval(ws._ping);
        if (activeRef.current) reconnectRef.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws.close();
    }

    reconnectRef.current = setTimeout(connect, 0);
    return () => {
      activeRef.current = false;
      clearTimeout(reconnectRef.current);
      const ws = wsRef.current;
      if (ws) { ws.onclose = null; ws.close(); wsRef.current = null; }
    };
  }, [url]);

  return { connected, lastMessage };
}

// ── useCameras ────────────────────────────────────────────────────────────────
export function useCameras(apiUrl) {
  const [cameras, setCameras] = useState([]);
  const [brands,  setBrands]  = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [cr, br] = await Promise.all([
        fetch(`${apiUrl}/cameras`),
        fetch(`${apiUrl}/cameras/brands`),
      ]);
      setCameras(await cr.json());
      const b = await br.json();
      setBrands(b.brands || []);
    } catch {} finally {
      setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  async function connectCamera(payload) {
    const r = await fetch(`${apiUrl}/cameras/connect`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Connect failed");
    await refresh();
    return d;
  }

  async function disconnectCamera(cameraId) {
    await fetch(`${apiUrl}/cameras/${cameraId}`, { method: "DELETE" });
    await refresh();
  }

  return { cameras, brands, loading, refresh, connectCamera, disconnectCamera };
}

// ── useAnalytics ──────────────────────────────────────────────────────────────
export function useAnalytics(apiUrl) {
  const [summary,    setSummary]    = useState(null);
  const [violations, setViolations] = useState([]);  // hourly time-series
  const [camStats,   setCamStats]   = useState([]);
  const [loading,    setLoading]    = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [sr, vr, cr] = await Promise.all([
        fetch(`${apiUrl}/analytics/summary`),
        fetch(`${apiUrl}/analytics/violations?hours=24`),
        fetch(`${apiUrl}/analytics/camera-stats?days=7`),
      ]);
      if (sr.ok) setSummary(await sr.json());
      if (vr.ok) setViolations(await vr.json());
      if (cr.ok) setCamStats(await cr.json());
    } catch {} finally {
      setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60000);  // refresh every minute
    return () => clearInterval(t);
  }, [refresh]);

  return { summary, violations, camStats, loading, refresh };
}

// ── useIncidentDetail ─────────────────────────────────────────────────────────
export function useIncidentDetail(apiUrl, incidentId) {
  const [detail,  setDetail]  = useState(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);

  useEffect(() => {
    if (!incidentId) { setDetail(null); return; }
    setLoading(true);
    setError(null);
    fetch(`${apiUrl}/incidents/${incidentId}`)
      .then(r => { if (!r.ok) throw new Error("Not found"); return r.json(); })
      .then(d => { setDetail(d); setLoading(false); })
      .catch(e => { setError(e.message); setLoading(false); });
  }, [apiUrl, incidentId]);

  return { detail, loading, error };
}

// ── useEvidence ───────────────────────────────────────────────────────────────
export function useEvidence(apiUrl, filters = {}) {
  const [evidence, setEvidence] = useState([]);
  const [loading,  setLoading]  = useState(true);

  const refresh = useCallback(async () => {
    const params = new URLSearchParams({ limit: 100, ...filters }).toString();
    try {
      const r = await fetch(`${apiUrl}/evidence?${params}`);
      if (r.ok) setEvidence(await r.json());
    } catch {} finally {
      setLoading(false);
    }
  }, [apiUrl, JSON.stringify(filters)]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, [refresh]);

  return { evidence, loading, refresh };
}

// ── useSystemHealth ───────────────────────────────────────────────────────────
export function useSystemHealth(alertApiUrl, videoIngestUrl, trafficAiUrl) {
  const [health, setHealth] = useState({
    alert_service:  null,
    video_ingest:   null,
    traffic_ai:     null,
  });

  const refresh = useCallback(async () => {
    const probe = async (url, key) => {
      try {
        const r = await fetch(`${url}/health`, { signal: AbortSignal.timeout(3000) });
        const d = await r.json();
        setHealth(prev => ({ ...prev, [key]: { ok: r.ok, ...d } }));
      } catch (e) {
        setHealth(prev => ({ ...prev, [key]: { ok: false, error: e.message } }));
      }
    };
    await Promise.all([
      probe(alertApiUrl,  "alert_service"),
      probe(videoIngestUrl, "video_ingest"),
      probe(trafficAiUrl,   "traffic_ai"),
    ]);
  }, [alertApiUrl, videoIngestUrl, trafficAiUrl]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  return { health, refresh };
}
