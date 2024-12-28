# Parseable Server (C) 2022 - 2024 Parseable, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import json
import logging
from datetime import datetime, timezone
from random import choice, randint
from uuid import uuid4

from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)  # Log to stdout
    ]
)

logger = logging.getLogger(__name__)

KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "localhost:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "local-logs-stream")
NUM_PARTITIONS = int(os.getenv("NUM_PARTITIONS", "6"))  # Default partitions
REPLICATION_FACTOR = int(os.getenv("REPLICATION_FACTOR", "1"))  # Default RF
TOTAL_LOGS = int(os.getenv("TOTAL_LOGS", "100"))  # Total logs to produce
LOG_RATE = int(os.getenv("LOG_RATE", "50"))  # Logs per second
REPORT_EVERY = 5_000  # Progress report frequency

producer_conf = {
    "bootstrap.servers": KAFKA_BROKERS,
    "queue.buffering.max.messages": 200_000,
    "queue.buffering.max.ms": 100,  # Up to 100ms linger
    "batch.num.messages": 10_000,
    "compression.type": "lz4",  # Compression (lz4, snappy, zstd, gzip)
    "message.send.max.retries": 3,
    "reconnect.backoff.ms": 100,
    "reconnect.backoff.max.ms": 3600000,
    # "acks": "all",  # Safer but can reduce throughput if replication is slow
}

admin_client = AdminClient({"bootstrap.servers": KAFKA_BROKERS})
producer = Producer(producer_conf)

LOG_TEMPLATE = {
    "timestamp": "",
    "correlation_id": "",
    "level": "INFO",
    "message": "",
    "pod": {"name": "", "namespace": "", "node": ""},
    "request": {"method": "", "path": "", "remote_address": ""},
    "response": {"status_code": 200, "latency_ms": 0},
    "metadata": {"container_id": "", "image": "", "environment": ""},
}


def create_topic(topic_name, num_partitions, replication_factor):
    new_topic = NewTopic(
        topic=topic_name,
        num_partitions=num_partitions,
        replication_factor=replication_factor
    )

    logger.info(f"Creating topic '{topic_name}' with {num_partitions} partitions and RF {replication_factor}...")
    fs = admin_client.create_topics([new_topic])

    for topic, f in fs.items():
        try:
            f.result()
            logger.info(f"Topic '{topic}' created successfully.")
        except Exception as e:
            if "TopicExistsError" in str(e):
                logger.warning(f"Topic '{topic}' already exists.")
            else:
                logger.error(f"Failed to create topic '{topic}': {e}")


def delivery_report(err, msg):
    if err:
        logger.error(f"Delivery failed for message {msg.key()}: {err}")
    else:
        logger.debug(f"Message delivered to {msg.topic()} [{msg.partition()}]")


def generate_log():
    log = LOG_TEMPLATE.copy()

    # Timestamp & correlation
    log["timestamp"] = datetime.now(timezone.utc).isoformat()
    log["correlation_id"] = str(uuid4())

    # Random level/message
    levels = ["INFO", "WARNING", "ERROR", "DEBUG"]
    messages = [
        "Received incoming HTTP request",
        "Processed request successfully",
        "Failed to process request",
        "Request timeout encountered",
        "Service unavailable",
    ]
    log["level"] = choice(levels)
    log["message"] = choice(messages)

    # Populate request fields
    methods = ["GET", "POST", "PUT", "DELETE"]
    paths = ["/api/resource", "/api/login", "/api/logout", "/api/data"]
    log["request"] = {
        "method": choice(methods),
        "path": choice(paths),
        "remote_address": f"192.168.1.{randint(1, 255)}",
    }

    # Populate response fields
    log["response"] = {
        "status_code": choice([200, 201, 400, 401, 403, 404, 500]),
        "latency_ms": randint(10, 1000),
    }

    # Populate pod and metadata fields
    log["pod"] = {
        "name": f"pod-{randint(1, 100)}",
        "namespace": choice(["default", "kube-system", "production", "staging"]),
        "node": f"node-{randint(1, 10)}",
    }

    log["metadata"] = {
        "container_id": f"container-{randint(1000, 9999)}",
        "image": f"example/image:{randint(1, 5)}.0",
        "environment": choice(["dev", "staging", "prod"]),
    }

    return log


def main():
    logger.info("Starting continuous log producer...")
    create_topic(KAFKA_TOPIC, NUM_PARTITIONS, REPLICATION_FACTOR)
    logger.info(f"Broker: {KAFKA_BROKERS}, Topic: {KAFKA_TOPIC}, Rate: {LOG_RATE} logs/sec")

    message_count = 0
    start_time = time.time()
    batch_start_time = time.time()
    limit_reached = False

    try:
        while True:
            current_time = time.time()

            if not limit_reached:
                if message_count < TOTAL_LOGS:
                    log_data = generate_log()
                    log_str = json.dumps(log_data)

                    # Send to Kafka
                    producer.produce(
                        topic=KAFKA_TOPIC,
                        value=log_str,
                        callback=delivery_report
                    )

                    message_count += 1

                    if message_count % REPORT_EVERY == 0:
                        batch_elapsed = current_time - batch_start_time
                        total_elapsed = current_time - start_time

                        logger.info(f"Batch of {REPORT_EVERY} messages produced in {batch_elapsed:.2f}s")
                        logger.info(f"Total messages: {message_count}, Running time: {total_elapsed:.2f}s")
                        logger.info(f"Current rate: ~{REPORT_EVERY / batch_elapsed:,.0f} logs/sec")

                        producer.flush()
                        batch_start_time = current_time

                elif not limit_reached:
                    logger.info(
                        f"Reached TOTAL_LOGS limit of {TOTAL_LOGS}. Continuing to run without producing messages...")
                    producer.flush()
                    limit_reached = True

            if limit_reached:
                time.sleep(5)
                total_elapsed = current_time - start_time
                if total_elapsed % 60 < 5:
                    logger.info(f"Total messages sent: {message_count}")
            else:
                # Sleep to maintain the logs/second rate
                time.sleep(1 / LOG_RATE)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user! Flushing remaining messages...")
        producer.flush()

    except Exception as e:
        logger.error(f"An error occurred: {e}")

    finally:
        logger.info("Flushing producer...")
        producer.flush()


if __name__ == "__main__":
    main()
