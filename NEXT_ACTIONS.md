# Next Actions

## Priority 1
- Implement MQTT message handling for Spotify download requests
- Add error handling and recovery for MQTT connection issues
- Create tests for MQTT client and Spotify downloader integration

## Priority 2
- Implement caching system to avoid re-downloading tracks
- Add support for dynamic adjustment of concurrent download limit
- Improve download progress reporting via MQTT

## Priority 3
- Create a dashboard for monitoring downloads
- Add support for configuring download format and quality
- Implement track metadata enrichment
- Refactor SpotifyDownloader._run_spotdl_command to reduce complexity
- Fix BLE001 linting errors throughout the codebase