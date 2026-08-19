-- Seed the always-on Eyenor pull camera (safe to re-run).
-- MediaMTX path is also declared in services/video-ingest/mediamtx.yml.
INSERT INTO cameras
    (camera_id, name, rtsp_url, brand, connection_mode, host, port,
     username, channel, location_name, zone_type, active)
VALUES
    ('eyenor_cam1',
     'Eyenor Cam 1',
     'rtsp://admin:123456@172.19.1.3:554/h264/ch1/main/av_stream',
     'eyenor',
     'pull',
     '172.19.1.3',
     554,
     'admin',
     1,
     'Eyenor Cam 1',
     'facility',
     TRUE)
ON CONFLICT (camera_id) DO NOTHING;
