"""HomeShield's own MQTT connection to its broker (paho-mqtt)."""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

SUBSCRIPTIONS = ("homeshield/+/status", "homeshield/+/info", "homeshield/+/config/ack",
                 "homeshield/+/pin/+/state", "homeshield/+/event")


class MqttLink:
    """Connects as the "homeshield" account, hands every message to the hub,
    and reconnects by itself (e.g. after the broker restarts)."""

    def __init__(self, hub, *, host: str = "127.0.0.1", port: int, user: str, password: str):
        self.hub = hub
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.client = None
        self.error: Optional[str] = None

    def start(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt is not installed. Run: pip install paho-mqtt"
            log.warning(self.error)
            return False
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="homeshield-server",
                        clean_session=True)
        c.username_pw_set(self.user, self.password)
        c.reconnect_delay_set(min_delay=1, max_delay=10)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c
        self.hub.attach(self)
        c.connect_async(self.host, self.port, keepalive=30)
        c.loop_start()
        return True

    def stop(self) -> None:
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.client.publish(topic, payload, qos=1, retain=retain)

    # paho callbacks (its network thread)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self.error = f"the broker refused HomeShield ({reason_code})"
            log.warning("MQTT: %s", self.error)
            return
        self.error = None
        client.subscribe([(t, 1) for t in SUBSCRIPTIONS])
        log.info("MQTT: connected to the broker on port %d", self.port)
        self.hub.on_connected()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.info("MQTT: disconnected (%s); retrying", reason_code)
        self.hub.on_disconnected()

    def _on_message(self, client, userdata, msg):
        try:
            self.hub.handle(msg.topic, msg.payload.decode("utf-8", errors="replace"))
        except Exception as e:
            log.exception("MQTT message on %s failed: %s", msg.topic, e)
