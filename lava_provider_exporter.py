#!/usr/bin/env python3
"""Prometheus exporter for the on-chain state of a Lava Network provider.

Polls the Lava REST API in the background and serves the last result on /metrics.
Standard library only.
"""
import argparse
import json
import logging
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ULAVA = 1_000_000
log = logging.getLogger("lava-provider-exporter")


def fetch_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "lava-provider-exporter/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Collector:
    def __init__(self, rest, provider, timeout):
        self.rest = rest.rstrip("/")
        self.provider = provider
        self.timeout = timeout
        self.lock = threading.Lock()
        self.entries = []
        self.self_delegation = None
        self.up = 0
        self.last_success = 0.0
        self.scrape_seconds = 0.0

    def poll(self):
        started = time.time()
        try:
            stake = fetch_json(f"{self.rest}/lavanet/lava/pairing/provider/{self.provider}/", self.timeout)
            entries = stake.get("stakeEntries") or []
            delegations = fetch_json(
                f"{self.rest}/lavanet/lava/dualstaking/delegator_providers/{self.provider}", self.timeout)
            self_del = sum(
                int(d["amount"]["amount"]) for d in delegations.get("delegations", [])
                if d.get("provider") == self.provider)
        except Exception as exc:  # network errors, bad JSON, API changes
            log.warning("poll failed: %s", exc)
            with self.lock:
                self.up = 0
                self.scrape_seconds = time.time() - started
            return
        with self.lock:
            self.entries = entries
            self.self_delegation = self_del
            self.up = 1
            self.last_success = time.time()
            self.scrape_seconds = self.last_success - started
        log.info("polled %d stake entries in %.2fs", len(entries), self.scrape_seconds)

    def render(self):
        with self.lock:
            entries, self_del = list(self.entries), self.self_delegation
            up, last_success, scrape_seconds = self.up, self.last_success, self.scrape_seconds

        out = []

        def metric(name, help_text, mtype, samples):
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {mtype}")
            for labels, value in samples:
                label_str = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")

        metric("lava_provider_up", "1 if the last poll of the Lava REST API succeeded.", "gauge", [({}, up)])
        metric("lava_provider_last_success_timestamp_seconds", "Unix time of the last successful poll.",
               "gauge", [({}, f"{last_success:.0f}")])
        metric("lava_provider_poll_duration_seconds", "Duration of the last poll.", "gauge",
               [({}, f"{scrape_seconds:.3f}")])

        if entries:
            first = entries[0]
            metric("lava_provider_info", "Provider identity.", "gauge",
                   [({"address": first.get("address", self.provider), "moniker": first.get("moniker", ""),
                      "website": (first.get("description") or {}).get("website", "")}, 1)])
        metric("lava_provider_specs_total", "Number of specs the provider is staked on.", "gauge",
               [({}, len(entries))])
        if self_del is not None:
            metric("lava_provider_self_delegation_lava", "Provider's own delegation, in LAVA.", "gauge",
                   [({}, self_del / ULAVA)])

        now = time.time()
        per_chain = [
            ("lava_provider_stake_lava", "Stake on the spec, in LAVA.",
             lambda e: int(e["stake"]["amount"]) / ULAVA),
            ("lava_provider_delegation_lava", "Delegations to the spec, in LAVA.",
             lambda e: int((e.get("delegate_total") or {}).get("amount", 0)) / ULAVA),
            ("lava_provider_commission_percent", "Delegator commission, percent.",
             lambda e: int(e.get("delegate_commission", 0))),
            ("lava_provider_jailed", "1 while the provider is jailed on the spec.",
             lambda e: int(int(e.get("jail_end_time", 0)) > now)),
            ("lava_provider_jails_total", "Times the provider was jailed on the spec.",
             lambda e: int(e.get("jails", 0))),
            ("lava_provider_jail_end_timestamp_seconds", "End of the current or last jail, 0 if never.",
             lambda e: int(e.get("jail_end_time", 0))),
            ("lava_provider_latest_block", "Latest block of the served chain, as last reported to Lava.",
             lambda e: int((e.get("block_report") or {}).get("latest_block", 0))),
            ("lava_provider_report_epoch", "Lava epoch of the last block report.",
             lambda e: int((e.get("block_report") or {}).get("epoch", 0))),
            ("lava_provider_stake_applied_block", "Lava block at which the current stake was applied.",
             lambda e: int(e.get("stake_applied_block", 0))),
            ("lava_provider_endpoints", "Endpoints registered for the spec.",
             lambda e: len(e.get("endpoints") or [])),
        ]
        for name, help_text, fn in per_chain:
            samples = []
            for e in entries:
                try:
                    samples.append(({"chain": e["chain"]}, fn(e)))
                except (KeyError, TypeError, ValueError):
                    continue
            metric(name, help_text, "gauge", samples)
        return "\n".join(out) + "\n"


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/metrics":
                body = collector.render().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            elif self.path == "/":
                body = b'<a href="/metrics">metrics</a>\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
            else:
                body = b"not found\n"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default="lava@15ngms6me8r486xs3nda0y6p2d56gym687etymg")
    p.add_argument("--rest", default="https://lava.rest.lava.build", help="Lava REST (LCD) endpoint")
    p.add_argument("--listen", default="0.0.0.0:9796", help="host:port to serve /metrics on")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument("--timeout", type=int, default=15, help="HTTP timeout per request")
    p.add_argument("--once", action="store_true", help="poll once, print metrics and exit")
    p.add_argument("--log-level", default="info")
    args = p.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    collector = Collector(args.rest, args.provider, args.timeout)
    collector.poll()
    if args.once:
        print(collector.render(), end="")
        return

    def loop():
        while True:
            time.sleep(args.interval)
            collector.poll()

    threading.Thread(target=loop, daemon=True).start()
    host, port = args.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), make_handler(collector))
    log.info("serving on http://%s/metrics for %s", args.listen, args.provider)
    server.serve_forever()


if __name__ == "__main__":
    main()
