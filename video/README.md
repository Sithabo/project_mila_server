# video/

Camera capture and hardware H.264 encoding code will live here.

The validated GStreamer pipeline concept (v4l2src → jpegdec → nvvidconv →
nvv4l2h264enc → h264parse) is documented in `docs/ENCODER_VALIDATION.md`. No
project code has been written yet — this package is currently scaffolding only.
