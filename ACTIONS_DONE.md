# Actions Done

## 2025-04-04 (Night)
- Fixed issue where downloads were queued but not starting automatically
- Added queue processing mechanism in the slot monitor to start pending downloads
- Improved slot management to respect max_concurrent_downloads limit
- Stored spotdl commands in task objects for deferred execution
- Updated CHANGELOG.md with version 0.1.5

## 2025-04-04 (Evening)
- Added download slot monitoring feature to track active/free download slots
- Added configurable monitoring interval (default: 10 seconds)
- Added max concurrent downloads configuration parameter
- Updated CHANGELOG.md with version 0.1.4
- Updated example.env with new configuration options

## 2025-04-04 (Afternoon)
- Implemented proper process timeout for Spotify downloads
- Added multiple empty line detection for reliable download completion
- Added playlist name tracking for m3u8 file handling
- Improved download completion detection
- Updated README.md with comprehensive documentation
- Updated CHANGELOG.md with version 0.1.2
- Modified spotdl command to run inside the destination folder
- Updated m3u8 file handling to account for new execution directory

## 2025-04-04 (Morning)
- Fixed periodic status updates functionality
- Fixed dictionary unpacking bug in SpotifyDownloader._run_status_updates()
- Fixed typo in variable name (metaata -> metadata)
- Updated CHANGELOG.md with latest fixes

## 2025-03-29
- Initial project setup
- Created basic directory structure
- Added project configuration files
- Set up MQTT client integration
- Integrated spotdl for Spotify download functionality