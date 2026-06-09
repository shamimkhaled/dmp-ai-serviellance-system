// dashboard hooks — single React import (avoid duplicate declaration error)

import { useState, useEffect, useCallback, useRef } from "react";

export function useAlerts(apiUrl, officerId) {
  const [alerts,    setAlerts]    = useState([]);
  const [incidents, setIncidents] = useState([]);

  useEffect(() => {
    fetchAlerts();
    fetchIncidents();
    const interval = setInterval(() => {
      fetchAlerts();
      fetchIncidents();
    }, 30000);
    return () => clearInterval(interval);
  }, [apiUrl]);

  async function fetchAlerts() {
    try {
      const resp = await fetch(`${apiUrl}/alerts?limit=100`);
      const data = await resp.json();
      setAlerts(data);
    } catch (err) {
      console.error("Failed to fetch alerts:", err);
    }
  }

  async function fetchIncidents() {
    try {
      const resp = await fetch(`${apiUrl}/incidents?limit=50`);
      const data = await resp.json();
      setIncidents(data);
    } catch (err) {
      console.error("Failed to fetch incidents:", err);
    }
  }

  const handleWsMessage = useCallback((message) => {
    if (message.type === "new_alert" || message.type === "alert_catchup") {
      setAlerts(prev => {
        const exists = prev.find(a => a.alert_id === message.alert_id);
        if (exists) return prev;
        return [message, ...prev].slice(0, 200);
      });
    }
    if (message.type === "alert_updated") {
      setAlerts(prev =>
        prev.map(a =>
          a.alert_id === message.alert_id
            ? { ...a, status: message.status }
            : a
        )
      );
    }
  }, []);

  async function performAction(alertId, action, notes = null) {
    try {
      await fetch(`${apiUrl}/alerts/${alertId}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, notes, officer_id: officerId }),
      });
      setAlerts(prev =>
        prev.map(a => a.alert_id === alertId ? { ...a, status: action } : a)
      );
    } catch (err) {
      console.error(`Action ${action} failed for alert ${alertId}:`, err);
    }
  }

  return {
    alerts,
    incidents,
    handleWsMessage,
    acceptAlert:   (id) => performAction(id, "accepted"),
    rejectAlert:   (id) => performAction(id, "rejected"),
    escalateAlert: (id) => performAction(id, "escalated"),
    closeAlert:    (id) => performAction(id, "closed"),
  };
}

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
        try { setLastMessage(JSON.parse(event.data)); } catch { /* ignore */ }
      };

      ws.onclose = () => {
        setConnected(false);
        clearInterval(ws._ping);
        if (activeRef.current) {
          reconnectRef.current = setTimeout(connect, 3000);
        }
      };

      ws.onerror = () => { ws.close(); };
    }

    // Defer by one tick so StrictMode's sync cleanup fires before the socket
    // opens, preventing the "closed before established" console warning.
    reconnectRef.current = setTimeout(connect, 0);

    return () => {
      activeRef.current = false;
      clearTimeout(reconnectRef.current);
      const ws = wsRef.current;
      if (ws) {
        ws.onclose = null;
        ws.close();
        wsRef.current = null;
      }
    };
  }, [url]);

  return { connected, lastMessage };
}

export function useCameras(apiUrl) {
  const [cameras, setCameras] = useState([]);
  const [brands,  setBrands]  = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [camResp, brandResp] = await Promise.all([
        fetch(`${apiUrl}/cameras`),
        fetch(`${apiUrl}/cameras/brands`),
      ]);
      setCameras(await camResp.json());
      const b = await brandResp.json();
      setBrands(b.brands || []);
    } catch (err) {
      console.error("Failed to load cameras:", err);
    } finally {
      setLoading(false);
    }
  }, [apiUrl]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, [refresh]);

  async function connectCamera(payload) {
    const resp = await fetch(`${apiUrl}/cameras/connect`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Connect failed");
    await refresh();
    return data;
  }

  async function disconnectCamera(cameraId) {
    await fetch(`${apiUrl}/cameras/${cameraId}`, { method: "DELETE" });
    await refresh();
  }

  return { cameras, brands, loading, refresh, connectCamera, disconnectCamera };
}
