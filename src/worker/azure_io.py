"""Azure adapters: Cosmos DB as the sink, Service Bus as the source. Both authenticate with DefaultAzureCredential (az login locally, managed identity in Azure): no key, no connection string.

Cosmos DB containers `events`, `cards` and `audit` are partitioned by /match_id. Service Bus delivers each match on its own session (session id = match id), so one receiver gets a match in order; messages the worker cannot read go to the dead-letter queue instead of blocking the session.
"""

import json
import logging
import os

from worker.match import SchemaError
from worker.pipeline import Pipeline

log = logging.getLogger("worker")


class CosmosSink:
    def __init__(self, endpoint: str, database: str) -> None:
        from azure.cosmos import CosmosClient
        from azure.identity import DefaultAzureCredential

        db = CosmosClient(endpoint, credential=DefaultAzureCredential()).get_database_client(database)
        self.events, self.cards, self.audit = (db.get_container_client(name) for name in ("events", "cards", "audit"))

    def event(self, m: dict) -> None:
        self.events.upsert_item({**m, "id": m["message_id"]})

    def card(self, card: dict) -> None:
        self.cards.upsert_item(card)

    def action(self, action: dict, card: dict) -> None:
        self.audit.upsert_item(action)
        self.cards.upsert_item(card)


def cosmos_from_env() -> CosmosSink:
    return CosmosSink(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "watershed"))


def listen(pipeline: Pipeline, fqdn: str, topic: str, subscription: str = "worker", idle_s: float = 30.0) -> None:
    """Receives sessions one after the other until none has a message for `idle_s` seconds."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import NEXT_AVAILABLE_SESSION, ServiceBusClient
    from azure.servicebus.exceptions import OperationTimeoutError

    with ServiceBusClient(fqdn, credential=DefaultAzureCredential()) as client:
        while True:
            try:
                receiver = client.get_subscription_receiver(topic, subscription, session_id=NEXT_AVAILABLE_SESSION, max_wait_time=idle_s)
                with receiver:
                    log.info("session %s", receiver.session.session_id)
                    for msg in receiver:
                        try:
                            body = json.loads(str(msg))
                            for result in pipeline.handle(body):
                                log.info("%s %s", result.get("action", "card"), result.get("card_id", result.get("id")))
                            receiver.complete_message(msg)
                        except (SchemaError, json.JSONDecodeError, KeyError) as error:
                            receiver.dead_letter_message(msg, reason="unreadable", error_description=str(error)[:4000])
                    pipeline.flush()
            except OperationTimeoutError:
                log.info("no session with messages for %s s: stopping", idle_s)
                return
