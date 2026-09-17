-- Lab seed (already applied on existing DBs). New installs should set
-- EYENOR_CAM1_RTSP instead of relying on this file.
-- MediaMTX paths are registered by video-ingest, not YAML.
INSERT INTO cameras
    (camera_id, name, rtsp_url, brand, connection_mode, host, port,
     username, channel, location_name, zone_type, active)
VALUES
    ('eyenor_cam1',
     'Eyenor Cam 1',
     'rtsp://USER:PASSWORD@CAMERA_IP:554/h264/ch1/main/av_stream',
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
