# Changelog

## 1.0.1

- **Classic Top Menu Bar & Procedural Toolbar:** Added a top menu bar (`File`, `Edit`, `View`, `Profiles`, `Help`) with keyboard shortcuts and a 2×-supersampled procedural icon toolbar with hover tooltips and instant feedback.
- **Light & Dark Mode:** Complete native dark theme toggleable via `View -> Dark Mode` or the moon toolbar button (persisted across sessions), styling title bars, menus, toolbar, panels, viewer list, and activity log.
- **Full-Height Column Gridlines:** Column divider lines in the video queue extend all the way down to the bottom of the list and reposition dynamically on resize.
- **Active Video in Title Bar:** The title bar displays the currently selected video file name.
- **Smooth Window Resizing:** Integrated window drag protection so resizing stays perfectly smooth on Windows.

## 1.0.0

First public release.

- Batch queue: drop videos or folders
- Profiles: Balanced, Fastest (hardware encoder when available), Smallest file (AV1 / HEVC), Super Compressed (under 20 MB)
- One-click size limits: Discord 10 MB, WhatsApp 16 MB, Email 25 MB or custom (2-pass encoding)
- Trim the start and end of each video; predicted size and time update live
- Scale down to 1080p / 720p / 480p / 360p, change or remove audio, MP4 or MKV
- Built-in updates: checks GitHub once a day and installs new versions in one click
