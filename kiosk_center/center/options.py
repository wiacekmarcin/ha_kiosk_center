"""Kiosk Center add-on options and local environment overrides."""
import json
import os
import re


def options() -> dict:
    opts = {"tcp_port": 47700, "udp_port": 47701, "ntp_server": "pool.ntp.org",
            "log_level": "info"}
    try:
        with open("/data/options.json", encoding="utf-8") as f:
            opts.update(json.load(f))
    except (OSError, ValueError):
        pass
    for key, env in (("tcp_port", "TCP_PORT"), ("udp_port", "UDP_PORT"),
                     ("ntp_server", "NTP_SERVER"), ("log_level", "LOG_LEVEL")):
        if os.environ.get(env):
            opts[key] = os.environ[env]
    server = str(opts["ntp_server"] or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", server):
        raise ValueError(f"Invalid NTP server configured: {server!r}")
    opts["ntp_server"] = server
    return opts
