# spotify_proxy/mqtt/client.py

"""MQTT Client module"""

import asyncio  # <--- Importar asyncio
import json
from typing import Any, Callable, Dict

import structlog
from gmqtt import Client as MQTTClient

from spotify_proxy.config.config import MQTTConfig

logger = structlog.get_logger(__name__)


class MQTTAdapter:
    """MQTT Adapter for handling MQTT operations"""

    def __init__(self, config: MQTTConfig):
        """Initialize MQTT adapter"""
        self.config = config
        self.client = MQTTClient(self.config.client_id)
        self.client.set_auth_credentials(
            self.config.username, self.config.password
        )
        self._connected = False
        self._loop: asyncio.AbstractEventLoop | None = (
            None  # <--- Añadir variable para el loop
        )

    @property
    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._connected

    async def connect(self) -> None:
        """Connect to MQTT broker and capture the event loop."""
        if self._connected:
            logger.debug("Already connected to MQTT broker.")
            return

        # --- Capturar el loop actual ---
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
                logger.debug("Captured asyncio event loop.")
            except RuntimeError as e:
                # Esto puede pasar si connect se llama desde fuera de un contexto async corriendo
                logger.error(
                    "Failed to get running asyncio loop. Ensure connect() is awaited.",
                    error=str(e),
                )
                raise ConnectionError(
                    "Cannot get asyncio loop. Not running in an async context?"
                ) from e
        # --- Fin captura loop ---

        logger.info(
            "Connecting to MQTT broker",
            host=self.config.host,
            port=self.config.port,
        )
        try:
            await self.client.connect(self.config.host, self.config.port)
            self._connected = True
            logger.info("Connected to MQTT broker")
        except Exception as e:
            logger.error("Failed to connect to MQTT broker", error=str(e))
            self._connected = False
            # No reseteamos el loop aquí, podría ser útil para reintentos
            raise ConnectionError(f"Failed to connect to MQTT: {e}") from e

    async def disconnect(self) -> None:
        """Disconnect from MQTT broker"""
        if self._connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.error("Error during MQTT disconnect", error=str(e))
            finally:
                self._connected = False
                # self._loop = None # No limpiar el loop, podríamos reconectar
                logger.info("Disconnected from MQTT broker")
        else:
            logger.debug("Already disconnected from MQTT broker.")

    async def subscribe(self, topic: str, qos: int, cb: Callable) -> None:
        """Subscribe to a topic

        Args:
            topic: The topic to subscribe to
            qos: The quality of service level
            cb: The callback function to handle incoming messages (called by gmqtt)
        """
        if not self._connected:
            logger.warning(
                "Not connected to MQTT broker. Attempting to connect..."
            )
            try:
                await self.connect()
            except ConnectionError as e:
                logger.error(
                    "Failed to connect before subscribing.", error=str(e)
                )
                raise  # Re-raise para que el llamador sepa que falló

        # Comprobar de nuevo después del intento de conexión
        if not self._connected:
            logger.error("Cannot subscribe, failed to connect to MQTT broker.")
            raise ConnectionError("Failed to connect before subscribing")

        logger.info("Subscribing to topic", topic=topic, qos=qos)
        try:
            # gmqtt usa on_message para *todas* las suscripciones.
            # Si necesitas callbacks diferentes por tópico, tendrás que gestionarlo
            # dentro de la función 'cb' que pasas aquí.
            self.client.on_message = cb
            self.client.subscribe(topic, qos=qos)  # Llamada síncrona de gmqtt
            logger.info("Subscription request sent for topic", topic=topic)
        except Exception as e:
            logger.error(
                "Failed to subscribe to topic", topic=topic, error=str(e)
            )
            raise

    async def publish_spotify_response(
        self, topic: str, response: Dict[str, Any]
    ) -> None:
        """
        Publish a Spotify response message to MQTT broker (ASYNC).

        This method MUST be called from within the asyncio event loop
        or scheduled correctly using publish_spotify_response_threadsafe.

        Args:
            topic: The topic to publish to
            response: The response message (dictionary) to publish
        """
        if not self._connected:
            # Podríamos intentar reconectar aquí, pero complica la lógica
            # Es mejor que la lógica que llama maneje la desconexión.
            logger.error(
                "Cannot publish Spotify response: not connected.", topic=topic
            )
            # Lanzar una excepción es útil para que el llamador sepa del fallo
            raise ConnectionError("MQTT client is not connected.")

        try:
            payload = json.dumps(response)
        except (TypeError, OverflowError) as e:
            logger.error(
                "Failed to serialize response to JSON",
                response_keys=list(
                    response.keys()
                ),  # Log solo las claves por si el contenido es grande/sensible
                error=str(e),
            )
            # Podríamos lanzar una excepción aquí también
            raise ValueError("Failed to serialize response payload") from e

        logger.debug(
            "Attempting to publish Spotify response",
            topic=topic,
            payload_size=len(payload),
        )

        try:
            # gmqtt publish es async y devuelve una tupla (mid, rc) o similar
            # No necesitamos 'await' aquí porque gmqtt lo gestiona internamente
            # pero la llamada es asíncrona en su efecto.
            # Nota: Originalmente pensé que necesitaba await, pero la doc de gmqtt
            # y ejemplos sugieren que publish() en sí no es una corutina, pero
            # opera sobre el loop async. Mantenemos la función como async por consistencia
            # y por si futuras versiones de gmqtt cambian.
            self.client.publish(topic, payload, qos=1)
            logger.debug("Publish command issued to gmqtt client", topic=topic)
        except Exception as e:
            logger.error(
                "Failed to publish response via gmqtt",
                topic=topic,
                error=str(e),
                exc_info=True,  # Log traceback for unexpected errors
            )
            # Considerar el estado de _connected aquí. Si falla por desconexión,
            # gmqtt podría intentar reconectar o deberíamos marcar _connected = False?
            # Por ahora, relanzamos la excepción.
            raise

    def publish_spotify_response_threadsafe(
        self, topic: str, response: Dict[str, Any]
    ) -> None:
        """
        Schedules publishing a Spotify response from a different thread
        using the asyncio event loop captured by this adapter. (Thread-Safe)

        Args:
            topic: The topic to publish to.
            response: The response message (dictionary) to publish.
        """
        if not self._loop:
            logger.error(
                "Cannot schedule publish: MQTT Adapter has no asyncio loop reference."
                " Was connect() awaited successfully?"
            )
            return  # No se puede hacer nada sin el loop

        if not self._connected:
            # Podemos intentar ponerlo en el loop igualmente, pero fallará si no
            # reconecta a tiempo. Loguear una advertencia es útil.
            logger.warning(
                "Scheduling publish but MQTT Adapter is currently not connected.",
                topic=topic,
            )
            # Continuamos para intentar ponerlo en el loop.

        # --- Usar call_soon_threadsafe ---
        try:
            # Creamos una tarea (Task) dentro del lambda para ejecutar la corutina async
            # Esto asegura que la corutina se ejecuta correctamente en el loop.
            # Pasamos una copia de 'response' al lambda para evitar problemas si
            # la variable 'response' cambia en el hilo llamador antes de que el lambda se ejecute.
            response_copy = response.copy()
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self.publish_spotify_response(topic, response_copy),
                    # Opcional: Dar un nombre a la tarea para debugging
                    # name=f"mqtt_publish_{topic}_{response_copy.get('message_id', 'unknown')}"
                )
            )
            logger.debug(
                "Scheduled status update for publishing via threadsafe call",
                topic=topic,
                # message_id=response_copy.get("message_id"), # Útil para trazar
            )
        except Exception as e:
            # Errores aquí suelen ser graves (ej. loop cerrado)
            logger.error(
                "Failed to schedule status update via call_soon_threadsafe",
                topic=topic,
                error=str(e),
                exc_info=True,  # Incluir traceback
            )
        # --- Fin call_soon_threadsafe ---

    # El método publish_telegram_message necesitaría una refactorización similar
    # si también se llama desde hilos. Por ahora, lo dejamos como está, asumiendo
    # que se llama desde el loop principal o no se usa.
    async def publish_telegram_message(
        self,
        topic: str,
        message: Dict[str, Any],
        channel_id: str = "",  # noqa: ARG002
    ) -> None:
        """Publish a telegram message to MQTT broker (Needs review if called from threads)"""
        # TODO: Add thread-safe version if needed.
        if not self._connected:
            logger.warning("Not connected to MQTT broker, reconnecting...")
            await (
                self.connect()
            )  # Este await puede fallar si se llama desde hilo

        # ... (resto del método sin cambios, pero susceptible a problemas si se llama desde hilo) ...
        payload = json.dumps(message)
        logger.debug(
            "Publishing message to MQTT broker", topic=topic, message=message
        )
        try:
            self.client.publish(topic, payload, qos=1)  # No es await
            logger.debug("Message published", topic=topic)
        except Exception as e:
            logger.error(
                "Failed to publish message", error=str(e), topic=topic
            )
            # El reintento con await connect() fallará si se llama desde hilo
            try:
                await self.connect()
                self.client.publish(topic, payload, qos=1)
                logger.debug(
                    "Message published after reconnection", topic=topic
                )
            except Exception as retry_e:
                logger.error(
                    "Failed to publish message after reconnection",
                    error=str(retry_e),
                    topic=topic,
                )
                raise
