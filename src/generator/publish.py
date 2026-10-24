"""Replay a generated match onto the Service Bus topic, one JSON message per feed message.

Every message carries session_id = match_id, so the worker receives each match in order. Late messages and duplicates from the replay file are sent as they are: the worker must handle them. Authentication goes through DefaultAzureCredential (az login locally, managed identity in Azure); there is no connection string.

Pacing is built for a live demo: the match runs `--speed` times faster than real time, except around key moments (goals, red cards, big chances, VAR checks, corrections), which play in real time so that a card can be written, approved and aired before the feed changes it. Half-time is cut to a few seconds, and a correction never arrives sooner than `--min-correction-delay` real seconds after the action it corrects.

Usage:
    python -m generator.publish data/replay/demo-0007.jsonl --dry --speed 0
    python -m generator.publish data/replay/demo-0007.jsonl --speed 10
    python -m generator.publish data/replay/demo-0007.jsonl --from-minute 60
Needs SERVICE_BUS_FQDN and SERVICE_BUS_TOPIC: from the environment, the .env file, or the azd environment (.azure/<env>/.env).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

KEY_BEFORE_S, KEY_AFTER_S = 20.0, 60.0  # broadcast seconds played in real time around a key moment


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def is_key(m: dict) -> bool:
    if m["type"] in ("var_check", "correction"):
        return m["type"] == "var_check" or m["op"] != "update" or m["cause"].startswith("var")
    if m["type"] != "action":
        return False
    shot = m.get("shot")
    return m.get("card") in ("red", "second_yellow") or (shot is not None and (shot["outcome"] == "goal" or shot["xg"] >= 0.2))


def schedule(messages: list[dict], speed: float, halftime_s: float = 10.0, from_minute: float | None = None, min_correction_delay: float = 40.0) -> list[float]:
    """Real seconds after the start at which each message (in file order) is sent."""
    if speed <= 0:
        return [0.0] * len(messages)
    windows = [(m["emitted_at_s"] - KEY_BEFORE_S, m["emitted_at_s"] + KEY_AFTER_S) for m in messages if is_key(m)]
    start = None if from_minute is None else from_minute * 60.0
    burst = start is not None  # everything before the chosen minute goes out at once, so the consumer catches up
    sent: dict[str, float] = {}
    times: list[float] = []
    real, prev, prev_period = 0.0, None, None
    for m in messages:
        t, period = m["emitted_at_s"], m.get("period", prev_period)
        if burst:
            if m.get("clock_s") is None or m["clock_s"] < start:
                times.append(0.0)
                sent.setdefault(m["message_id"], 0.0)
                prev_period = period
                continue
            burst = False
        if prev is not None:
            gap = max(t - prev, 0.0)
            if prev_period == 1 and period == 2:
                real += halftime_s
            else:
                real += gap if any(a <= t <= b for a, b in windows) else gap / speed
        if m["type"] == "correction" and m["target_event_id"] in sent:
            real = max(real, sent[m["target_event_id"]] + min_correction_delay)
        times.append(real)
        sent.setdefault(m["message_id"], real)
        prev, prev_period = t, period
    return times


def replay(messages: list[dict], send, speed: float, **pacing) -> None:
    """Calls send(message) for each message at its scheduled time (speed 0: no wait)."""
    times = schedule(messages, speed, **pacing)
    started = time.monotonic()
    for m, at in zip(messages, times):
        delay = at - (time.monotonic() - started)
        if delay > 0:
            time.sleep(delay)
        send(m)


def service_bus_settings() -> tuple[str, str]:
    """Reads the Service Bus settings from the environment, the .env file, or the default azd environment."""
    from dotenv import dotenv_values, load_dotenv

    load_dotenv()
    if not os.environ.get("SERVICE_BUS_FQDN"):
        config = Path(".azure/config.json")
        name = json.loads(config.read_text(encoding="utf-8")).get("defaultEnvironment") if config.exists() else None
        azd_env = Path(".azure") / name / ".env" if name else None
        if azd_env and azd_env.exists():
            for key, value in dotenv_values(azd_env).items():
                os.environ.setdefault(key, value or "")
    fqdn, topic = os.environ.get("SERVICE_BUS_FQDN"), os.environ.get("SERVICE_BUS_TOPIC", "match-events")
    if not fqdn:
        sys.exit("SERVICE_BUS_FQDN is not set: run `azd provision`, or copy it from `azd env get-values` into .env")
    return fqdn, topic


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("replay", type=Path)
    parser.add_argument("--speed", type=float, default=10, help="replay speed outside key moments; 0 sends everything at once")
    parser.add_argument("--from-minute", type=float, help="send everything before this match minute at once, then pace the rest")
    parser.add_argument("--halftime-s", type=float, default=10.0, help="real seconds for the half-time break")
    parser.add_argument("--min-correction-delay", type=float, default=40.0, help="real seconds between an action and its correction, at least")
    parser.add_argument("--dry", action="store_true", help="print the messages instead of sending them")
    args = parser.parse_args()
    messages = load(args.replay)
    pacing = {"halftime_s": args.halftime_s, "from_minute": args.from_minute, "min_correction_delay": args.min_correction_delay}

    if args.dry:
        replay(messages, lambda m: print(m["emitted_at_s"], m.get("minute_label", ""), m["message_id"], m["type"], m.get("action_type", "")), args.speed, **pacing)
        return

    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient, ServiceBusMessage
    from azure.servicebus.exceptions import ServiceBusAuthenticationError, ServiceBusAuthorizationError

    fqdn, topic = service_bus_settings()
    try:
        with ServiceBusClient(fqdn, credential=DefaultAzureCredential()) as client, client.get_topic_sender(topic) as sender:

            def send(m: dict) -> None:
                sender.send_messages(
                    ServiceBusMessage(
                        json.dumps(m, ensure_ascii=False),
                        session_id=m["match_id"],
                        message_id=m["message_id"],
                        content_type="application/json",
                        application_properties={"type": m["type"], "schema_version": m["schema_version"]},
                    )
                )

            replay(messages, send, args.speed, **pacing)
    except (ClientAuthenticationError, ServiceBusAuthenticationError, ServiceBusAuthorizationError) as error:
        sys.exit(f"Service Bus refused the connection ({type(error).__name__}). Check `az login`, and that your account has the Azure Service Bus Data Sender role on {fqdn}: a new role assignment can take 5 to 10 minutes to apply.")
    print(f"sent {len(messages)} messages from {args.replay} to {topic}")


if __name__ == "__main__":
    main()
