"""Opt-in real Kafka delivery/restart and webhook ingestion in disposable Linux storage."""

import os
import re
import subprocess
import time
from uuid import uuid4

import duckdb
import pytest
from test_customer_pipeline_container import disposable_warehouse

KAFKA_IMAGE = (
    "apache/kafka:4.2.0@sha256:b5556544da1dccd04f03690107a233c37dbfd01d66e28ffdfc4fafba9b6e5039"
)


def cleanup_resources(network: str) -> None:
    if not re.fullmatch(r"parallel-test-[a-f0-9]{32}", network):
        raise ValueError("Cleanup is restricted to a disposable parallel test namespace")
    failures = []
    commands = [
        ["docker", "rm", "--force", "--volumes", network + "-worker"],
        ["docker", "rm", "--force", "--volumes", network + "-broker"],
        ["docker", "network", "rm", network],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            missing = (
                "No such container:" in result.stderr
                or f"network {network} not found" in result.stderr
            )
            if result.returncode and not missing:
                failures.append(command[1])
        except subprocess.TimeoutExpired:
            failures.append(command[1])
    if failures:
        raise RuntimeError(
            "Disposable parallel cleanup failed; inspect Docker backend and test namespace"
        )


@pytest.mark.skipif(
    os.environ.get("RETAILFLOW_TEST_PARALLEL_IMAGES") != "1",
    reason="Opt in to disposable Kafka and parallel validation images",
)
def test_real_kafka_resume_and_webhook_coexist_with_batch(tmp_path):
    name = "parallel-test-" + uuid4().hex
    broker_name = name + "-broker"
    worker_name = name + "-worker"
    database = tmp_path / "retailflow.duckdb"
    duckdb.connect(str(database)).close()

    def docker(*args, timeout=180):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    script = r"""
import json, os, time
from pathlib import Path
import duckdb
from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient, NewTopic
from fastapi.testclient import TestClient
from filelock import FileLock
from retailflow.event_contracts import encode, generate
from retailflow.event_runtime import TOPIC, consume_one, inventory_delivery
from retailflow.shipment_webhook import create_app, signature

assert os.getuid()==10001
path=Path('/warehouse/retailflow.duckdb')
with FileLock(str(path)+'.lock',mode=0o660):
 with duckdb.connect(str(path)) as db:
  db.execute('CREATE SCHEMA raw; CREATE TABLE raw.batch_sentinel AS SELECT 42 AS value')
admin=AdminClient({'bootstrap.servers':'retailflow-kafka:9092'})
admin.create_topics([NewTopic(TOPIC,num_partitions=3,replication_factor=1)])[TOPIC].result(30)
producer=Producer({'bootstrap.servers':'retailflow-kafka:9092','enable.idempotence':True})
config={'bootstrap.servers':'retailflow-kafka:9092','group.id':'fixture-inventory',
        'enable.auto.commit':False,'enable.auto.offset.store':False,'auto.offset.reset':'earliest',
        'session.timeout.ms':6000,'heartbeat.interval.ms':1000}
class Store:
 def __init__(self): self.files={}
 def put(self,name,value,content_type):
  if name in self.files: assert self.files[name]==value
  self.files[name]=value
 def read(self,name): return self.files[name]
store=Store()
consumer=Consumer(config); consumer.subscribe([TOPIC])
try:
 inventory=generate('inventory-v1',1,interval=5)
 inventory_delivery(producer,inventory); inventory_delivery(producer,inventory)
 deadline=time.monotonic()+60; received=0
 while received<2 and time.monotonic()<deadline:
  received+=int(consume_one(consumer,store,path))
 assert received==2
finally: consumer.close()
# Rejoin the same group. Its committed positions must prevent old deliveries reappearing.
consumer=Consumer(config); consumer.subscribe([TOPIC])
try:
 deadline=time.monotonic()+20
 while not consumer.assignment() and time.monotonic()<deadline:
  assert not consume_one(consumer,store,path)
 assert consumer.assignment()
 inventory_delivery(producer,generate('inventory-v1',2,interval=5))
 deadline=time.monotonic()+30; received=False
 while not received and time.monotonic()<deadline:
  received=consume_one(consumer,store,path)
 assert received
finally: consumer.close()
secret=bytes(range(32)); stamp=str(int(time.time()))
body=encode(generate('shipments-v1',1,interval=5))
headers={'Content-Type':'application/json','X-RetailFlow-Timestamp':stamp,
         'X-RetailFlow-Signature':signature(secret,stamp,body)}
with TestClient(create_app(store_factory=lambda:store,database=path,key=secret)) as client:
 assert client.post('/v1/shipments/events',content=body,headers=headers).status_code==202
 assert client.post('/v1/shipments/events',content=body,headers=headers).json()['already_loaded']
with FileLock(str(path)+'.lock',mode=0o660):
 with duckdb.connect(str(path)) as db:
  assert db.execute('SELECT COUNT(*) FROM streaming.inventory_movements').fetchone()==(2,)
  assert db.execute('SELECT COUNT(*) FROM event_driven.shipment_updates').fetchone()==(1,)
  assert db.execute('SELECT COUNT(*) FROM parallel_ops.batches').fetchone()==(4,)
  assert db.execute('SELECT * FROM raw.batch_sentinel').fetchone()==(42,)
print('PASS real Kafka resume, duplicates, webhook replay and preserved batch data')
"""
    docker("network", "create", name)
    try:
        docker(
            "run",
            "-d",
            "--name",
            broker_name,
            "--network",
            name,
            "--network-alias",
            "retailflow-kafka",
            "--memory",
            "768m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            "type=volume,target=/var/lib/kafka/data",
            "-e",
            "KAFKA_NODE_ID=1",
            "-e",
            "KAFKA_PROCESS_ROLES=broker,controller",
            "-e",
            "KAFKA_CONTROLLER_QUORUM_VOTERS=1@retailflow-kafka:9093",
            "-e",
            "KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093",
            "-e",
            "KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://retailflow-kafka:9092",
            "-e",
            "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
            "-e",
            "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT",
            "-e",
            "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1",
            "-e",
            "KAFKA_LOG_DIRS=/var/lib/kafka/data",
            "-e",
            "KAFKA_HEAP_OPTS=-Xms128m -Xmx384m",
            KAFKA_IMAGE,
        )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    broker_name,
                    "/opt/kafka/bin/kafka-broker-api-versions.sh",
                    "--bootstrap-server",
                    "retailflow-kafka:9092",
                ],
                capture_output=True,
                timeout=15,
            )
            if result.returncode == 0:
                break
            time.sleep(2)
        else:
            pytest.fail("Disposable Kafka did not become ready")
        with disposable_warehouse(database) as owner:
            output = docker(
                "run",
                "--rm",
                "--name",
                worker_name,
                "--network",
                name,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--volumes-from",
                owner,
                "--tmpfs",
                "/tmp:rw,nosuid,size=64m",
                "--entrypoint",
                "python",
                "retailflow-parallel:validation",
                "-c",
                script,
            )
            assert "PASS real Kafka resume" in output
    finally:
        cleanup_resources(name)
