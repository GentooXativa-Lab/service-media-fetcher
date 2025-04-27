# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.5] - 2025-04-04
### Fixed
- Fixed issue where downloads were queued but not starting automatically
- Added queue processing to start pending downloads when slots become available
- Improved download slot management to respect max_concurrent_downloads limit

## [0.1.4] - 2025-04-04
### Added
- Added download slot monitoring feature to display active/free download slots
- Added configurable interval for slot monitoring (default: 10 seconds)
- Added max concurrent downloads configuration parameter (default: 3)

## [0.1.3] - 2025-04-04
### Changed
- Modified spotdl command to run inside the destination folder
- Updated m3u8 file handling to account for new execution directory

## [0.1.2] - 2025-04-04
### Added
- Added process timeout to prevent stuck downloads
- Added multiple empty line detection for more reliable download completion
- Added spotify playlist name tracking for proper m3u8 file handling
- Updated README.md with comprehensive documentation

### Changed
- Improved download process handling with better timeout management
- Enhanced error handling for download processes

## [0.1.1] - 2025-04-04
### Fixed
- Fixed status update not running periodically due to incorrect dictionary unpacking
- Fixed typo in variable name (metaata -> metadata)

## [0.1.0] - 2025-03-29
### Added
- Initial project structure
- MQTT client integration
- Spotify downloader integration using spotdl
- Configuration system using pydantic-settings
- Logging system using structlog