import asyncio
import functools
import os
import signal

import structlog
from dotenv import load_dotenv

from spotify_proxy.config.config import GXConfig, MQTTTopics
from spotify_proxy.mqtt.client import MQTTAdapter
from spotify_proxy.mqtt.models import SpotifyRequest
from spotify_proxy.spotify.downloader import SpotifyDownloader

logger = structlog.get_logger(__name__)


async def shutdown(
    mqtt_adapter: MQTTAdapter, spotify_downloader: SpotifyDownloader
) -> None:
    """Gracefully shutdown the application

    Args:
        mqtt_adapter: MQTT adapter instance
        spotify_downloader: SpotifyDownloader instance
    """
    logger.info("Shutting down...")

    # Stop status updates - catch specific network/IO errors
    try:
        await spotify_downloader.stop_status_updates()
    except (ConnectionError, TimeoutError, IOError) as e:
        logger.error("Error stopping status updates", error=str(e))
    except Exception as e:  # Still need a fallback for unexpected errors
        logger.error("Unexpected error stopping status updates", error=str(e))

    # Disconnect from MQTT broker - catch network errors
    try:
        await mqtt_adapter.disconnect()
    except (ConnectionError, TimeoutError, IOError) as e:
        logger.error(
            "Network error disconnecting from MQTT broker", error=str(e)
        )
    except Exception as e:  # Still need a fallback for unexpected errors
        logger.error("Unexpected error disconnecting from MQTT", error=str(e))


async def handle_spotify_response(
    mqtt_adapter: MQTTAdapter,
    topic_config: MQTTTopics,
    spotify_downloader: SpotifyDownloader,
    client,  # noqa: ARG001 - Used by MQTT callback mechanism
    topic: str,  # noqa: ARG001 - Used by MQTT callback mechanism
    payload: bytes,
    qos: int,  # noqa: ARG001
    properties,  # noqa: ARG001
) -> None:
    """Handle Spotify response callback

    This function is called when a message is received from the MQTT broker
    on the spotify request topic. It processes the message using
    SpotifyDownloader and publishes the response back to the MQTT broker.

    Args:
        mqtt_adapter: MQTT adapter instance
        topic_config: MQTT topic configuration
        spotify_downloader: SpotifyDownloader instance
        client: MQTT client instance
        topic: MQTT topic
        payload: Message payload
        qos: Quality of service (not used, received from MQTT)
        properties: Message properties (not used, received from MQTT)
    """

    spotify_request = SpotifyRequest.model_validate_json(
        payload.decode("utf-8")
    )

    # Process message using SpotifyDownloader
    response = spotify_downloader.process_download_request(spotify_request)

    # Publish initial response back to MQTT broker
    if response:
        await mqtt_adapter.publish_spotify_response(
            topic_config.download_status_topic, response
        )

    # The actual download is processed in a background thread
    # The spotify_downloader class will manage tracking and status updates


async def main() -> None:
    """Main application entry point"""
    # Load environment variables from .env file if it exists
    load_dotenv()

    # dump environment variables for debugging
    logger.debug("Environment variables", env_vars=os.environ)

    logger.info("Starting Spotify Proxy Service")

    # Initialize configuration
    config = GXConfig()
    topic_config = MQTTTopics()

    if config.debug:
        logger.info("Debug mode enabled")

    # Log configuration (exclude sensitive data)
    logger.debug(
        "Configuration loaded",
        mqtt_host=config.mqtt.host,
        mqtt_port=config.mqtt.port,
        mqtt_client_id=config.mqtt.client_id,
    )

    # Initialize MQTT adapter
    mqtt_adapter = MQTTAdapter(config.mqtt)

    # Connect to MQTT broker
    try:
        await mqtt_adapter.connect()
    except (ConnectionError, TimeoutError, IOError) as e:
        logger.error("Failed to connect to MQTT broker, exiting", error=str(e))
        return
    except Exception as e:  # Still need a fallback for unexpected errors
        logger.error(
            "Unexpected error connecting to MQTT broker", error=str(e)
        )
        return

    # Initialize and subscribe to MQTT topics
    try:
        # Initialize SpotifyDownloader
        spotify_downloader = SpotifyDownloader(config.spotify)

        # Set up signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(
                    shutdown(mqtt_adapter, spotify_downloader)
                ),
            )

        # Create a callback with the spotify_downloader and mqtt_adapter
        callback = functools.partial(
            handle_spotify_response,
            mqtt_adapter,
            topic_config,
            spotify_downloader,
        )

        # Subscribe to Spotify request topic
        await mqtt_adapter.subscribe(
            topic_config.download_request_topic,
            qos=1,
            cb=callback,
        )

        # Start periodic status updates (every 10 seconds)
        spotify_downloader.start_status_updates(
            mqtt_adapter,
            topic_config.download_status_topic,
            interval_seconds=10,
        )

        logger.info(
            "Spotify Proxy Service started",
            topics={
                "download": topic_config.download_request_topic,
                "status": topic_config.download_status_topic,
            },
        )

        # Keep the application running
        # Using Event instead of a while loop with sleep
        stop_event = asyncio.Event()
        await stop_event.wait()

    except (ConnectionError, TimeoutError, IOError) as e:
        logger.error("Network error in Spotify service", error=str(e))
        await mqtt_adapter.disconnect()
    except Exception as e:  # Still need a fallback for unexpected errors
        logger.error("Unexpected error in Spotify service", error=str(e))
        await mqtt_adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
