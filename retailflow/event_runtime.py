"""Independent continuous inventory stream and shipment business-event simulator."""

import argparse
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx
from dotenv import dotenv_values
from filelock import FileLock

from retailflow.api import EPOCH
from retailflow.blob_storage import BlobStore
from retailflow.event_contracts import InventoryMovement, decode, encode, generate
from retailflow.event_ingestion import ingest
from retailflow.shipment_webhook import KEY_FILE, read_key, signature

TOPIC = "retailflow.inventory.v1"


def setting(name: str, default: str) -> str:
    local = dotenv_values(".env", interpolate=False)
    return os.environ.get(name, local.get(name) or default)


def broker() -> str:
    value = setting("RETAILFLOW_KAFKA_BOOTSTRAP", "127.0.0.1:19092")
    if value not in {"127.0.0.1:19092", "retailflow-kafka:9092"}:
        raise ValueError("Only the approved local Kafka broker is supported")
    return value


def save(path: Path, record: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(record, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def source_step(
    state: Path,
    kind: str,
    deliver,
    *,
    now: datetime,
    seed: int = 42,
    interval: int = 600,
    order_interval: int = 600,
) -> int | None:
    """Freeze one event before sending, advance only after broker/webhook acknowledgement."""
    state.mkdir(parents=True, exist_ok=True, mode=0o770)
    with FileLock(str(state / "source.lock"), mode=0o660, timeout=10):
        path = state / "checkpoint.json"
        current = json.loads(path.read_text()) if path.exists() else None
        profile = [kind, seed, interval, order_interval if kind == "shipments-v1" else 0]
        if current and current["profile"] != profile:
            raise ValueError("Source profile changed; use a separately reviewed state namespace")
        latest = int((now - EPOCH).total_seconds()) // interval + 1
        if latest < 1:
            raise ValueError("Clock precedes the source epoch")
        sequence = current["next"] if current else latest
        if sequence > latest:
            return None
        if current and current.get("pending"):
            event = decode(json.dumps(current["pending"]).encode(), now=now)
        else:
            event = generate(
                kind, sequence, seed=seed, interval=interval, order_interval=order_interval
            )
            save(
                path,
                {"profile": profile, "next": sequence, "pending": event.model_dump(mode="json")},
            )
        if event.sequence != sequence:
            raise ValueError("Pending source sequence changed")
        deliver(event)
        save(path, {"profile": profile, "next": sequence + 1, "pending": None})
        return sequence


def consume_one(consumer, store, database: Path, *, timeout: float = 1) -> bool:
    message = consumer.poll(timeout)
    if message is None:
        return False
    if message.error():
        raise RuntimeError("Kafka consumer could not read an event")
    if message.topic() != TOPIC:
        raise ValueError("Unexpected inventory topic")
    event = decode(message.value())
    if not isinstance(event, InventoryMovement):
        raise ValueError("Non-inventory event on inventory stream")
    ingest(
        store, event, database, topic=TOPIC, partition=message.partition(), offset=message.offset()
    )
    positions = consumer.commit(message=message, asynchronous=False) or []
    if any(position.error for position in positions):
        raise RuntimeError("Kafka offset commit was not acknowledged")
    return True


def inventory_delivery(producer, event) -> None:
    outcome = []
    producer.produce(
        TOPIC,
        key=f"{event.warehouse_id}:{event.product_id}".encode(),
        value=encode(event),
        on_delivery=lambda error, message: outcome.append(error),
    )
    if producer.flush(15) or len(outcome) != 1 or outcome[0] is not None:
        raise RuntimeError("Inventory publish was not acknowledged")


def shipment_delivery(client: httpx.Client, key: bytes, event) -> None:
    body = encode(event)
    timestamp = str(int(time.time()))
    response = client.post(
        "/v1/shipments/events",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-RetailFlow-Timestamp": timestamp,
            "X-RetailFlow-Signature": signature(key, timestamp, body),
        },
    )
    if response.status_code != 202 or response.json().get("status") != "committed":
        raise RuntimeError("Shipment webhook did not acknowledge its commit")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "role",
        choices=["init-topics", "inventory-producer", "inventory-consumer", "shipment-simulator"],
    )
    args = parser.parse_args()
    os.umask(0o007)
    stopped = Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stopped.set())
    if args.role == "init-topics":
        from confluent_kafka.admin import AdminClient, NewTopic

        admin = AdminClient({"bootstrap.servers": broker()})
        topics = admin.list_topics(timeout=15).topics
        if TOPIC not in topics:
            admin.create_topics([NewTopic(TOPIC, num_partitions=3, replication_factor=1)])[
                TOPIC
            ].result(30)
        if len(admin.list_topics(TOPIC, timeout=15).topics[TOPIC].partitions) != 3:
            raise ValueError("Inventory topic must retain its three-partition contract")
        return
    if args.role == "inventory-consumer":
        from confluent_kafka import Consumer

        consumer = Consumer(
            {
                "bootstrap.servers": broker(),
                "group.id": "retailflow-inventory-v1",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
                "allow.auto.create.topics": False,
            }
        )
        store = BlobStore()
        try:
            store.ensure_container()
            consumer.subscribe([TOPIC])
            while not stopped.is_set():
                consume_one(consumer, store, Path(os.environ["RETAILFLOW_DUCKDB_PATH"]))
        finally:
            consumer.close()
            store.close()
        return
    kind = "inventory-v1" if args.role == "inventory-producer" else "shipments-v1"
    interval = int(setting("RETAILFLOW_EVENT_INTERVAL_SECONDS", "600"))
    seed = int(setting("RETAILFLOW_SEED", "42"))
    order_interval = int(setting("RETAILFLOW_INTERVAL_SECONDS", "600"))
    if not 5 <= interval <= 3600:
        raise ValueError("Event interval must be 5-3600 seconds")
    state = Path(setting("RETAILFLOW_EVENT_STATE_DIR", "artifacts/event-state")) / kind
    client = None
    producer = None
    if kind == "inventory-v1":
        from confluent_kafka import Producer

        producer = Producer(
            {
                "bootstrap.servers": broker(),
                "enable.idempotence": True,
                "acks": "all",
                "allow.auto.create.topics": False,
            }
        )

        def deliver(event):
            inventory_delivery(producer, event)
    else:
        url = setting("RETAILFLOW_SHIPMENT_WEBHOOK_URL", "http://127.0.0.1:18090")
        if url not in {"http://127.0.0.1:18090", "http://retailflow-shipment-webhook:8090"}:
            raise ValueError("Unapproved local shipment webhook")
        client = httpx.Client(base_url=url, timeout=15, trust_env=False, follow_redirects=False)
        key = read_key(Path(setting("RETAILFLOW_SHIPMENT_KEY_FILE", str(KEY_FILE))))

        def deliver(event):
            shipment_delivery(client, key, event)

    try:
        while not stopped.is_set():
            try:
                result = source_step(
                    state,
                    kind,
                    deliver,
                    now=datetime.now(UTC),
                    seed=seed,
                    interval=interval,
                    order_interval=order_interval,
                )
                stopped.wait(1 if result is None else 0.05)
            except (httpx.HTTPError, RuntimeError, BufferError):
                # Preserve the frozen event and retry; never log response bodies or credentials.
                print(json.dumps({"status": "delivery_retry", "source": kind}), flush=True)
                stopped.wait(5)
    finally:
        if client is not None:
            client.close()
        if producer is not None:
            producer.flush(5)


if __name__ == "__main__":
    main()
