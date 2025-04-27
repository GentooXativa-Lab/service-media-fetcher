# Spotify Proxy Service

## Project Overview
The Spotify Proxy Service is a Python-based service that integrates with MQTT to facilitate downloading and handling Spotify content. It uses the spotdl library to download Spotify tracks and playlists, and communicates over MQTT to receive download requests and publish status updates.

## Installation Instructions
1. Ensure you have Python 3.12+ installed
2. Clone this repository
3. Install dependencies:
```bash
uv sync
```
4. Install spotdl in your Python environment:
```bash
uv add spotdl
```

## Usage Instructions
1. Create a `.env` file based on the `example.env` file with your Spotify API credentials
2. Run the service:
```bash
uv run -m spotify_proxy
```

## Build & Test Commands
- Install dependencies: `uv sync`
- Add package: `uv add package_name`
- Add dev dependencies: `uv add --dev package_name`
- Run application: `uv run -m spotify_proxy`
- Lint: `uv run -m ruff check .`
- Format: `uv run -m ruff format .`
- Tests: `uv run -m pytest`
- Single test: `uv run -m pytest tests/path/to/test.py::test_function -v`
- Test with coverage: `uv run -m pytest`

## Code Style Guidelines
- Python 3.12+ compatible
- Line length: 79 characters max
- Imports: grouped by stdlib, third-party, local with blank lines between
- Formatting: use ruff (configured in pyproject.toml)
- Type hints: required for all parameters and return values
- Docstrings: use triple-quoted strings for modules, classes, and functions
- Naming: snake_case for variables/functions, PascalCase for classes
- Async: use asyncio for concurrent operations
- Error handling: use typed exceptions and explicit error messages
- Logging: use structlog for structured logging
- Testing: pytest with asyncio support and 95%+ coverage

## MQTT Broker
This service communicates with an MQTT broker using the gmqtt library. The service:
- Connects to the MQTT broker asynchronously
- Subscribes to specific topics to receive download requests
- Publishes download status updates to configured topics
- Handles incoming messages asynchronously

### Topics
- `/gx-lab/spotify/download`: For receiving download requests
- `/gx-lab/spotify/download/status`: For publishing download status updates

## Spotify Download Features
- Downloads Spotify playlists and albums using spotdl
- Creates m3u8 playlist files that are stored in the destination folder
- Implements process timeout to prevent hanging downloads
- Provides real-time status updates via MQTT
- Detects and handles stuck downloads automatically
- Cleans up completed downloads to manage memory usage# service-media-fetcher
