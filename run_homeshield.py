"""
HomeShield entry point.

Examples
--------
# Default: starts on http://localhost:5000/. Cameras you add via the UI
# are persisted in homeshield.db and reloaded next launch.
python run_homeshield.py

# Custom port and DB
python run_homeshield.py --port 8080 --db custom.db

# Bind only to localhost (default is 0.0.0.0 so phones can hit it)
python run_homeshield.py --host 127.0.0.1

# ESP32 nodes talk to HomeShield's own Mosquitto broker on port 1884
python run_homeshield.py --mqtt-port 1885     # if 1884 is taken
python run_homeshield.py --no-mqtt            # cameras only, no smart devices
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from homeshield.notify import load_dotenv


def start_tunnel(app, args) -> None:
    """Open remote access, unless the default admin password still works."""
    from homeshield.tunnel import Tunnel

    users = app.extensions["homeshield_users"]
    if users.default_admin_active():
        # Over the internet, admin/admin would be the first thing anyone tries.
        app.extensions["homeshield_tunnel"] = Tunnel.blocked(
            "default_admin", args.port, args.tunnel_token)
        print("[HomeShield] Remote access: NOT opened. The default admin password still works.")
        print("[HomeShield]   Sign in on this computer, change it, then restart with --tunnel.")
        return
    tunnel = Tunnel(args.port, token=args.tunnel_token, public_url=args.public_url)
    app.extensions["homeshield_tunnel"] = tunnel
    kind = "named tunnel" if tunnel.mode == "named" else "Quick Tunnel"
    print(f"[HomeShield] Remote access: starting Cloudflare {kind}...")
    if tunnel.start():
        print(f"[HomeShield] Remote access: {tunnel.url or 'the hostname set for this tunnel in Cloudflare'}")
        if tunnel.mode == "quick":
            print("[HomeShield]   The address changes every time HomeShield starts. "
                  "Share it only with your household.")
    elif tunnel.error == "not_installed":
        print("[HomeShield] Remote access: cloudflared is not installed. Install it with")
        print("[HomeShield]   winget install --id Cloudflare.cloudflared")
    else:
        print(f"[HomeShield] Remote access: not connected yet "
              f"({tunnel.error or 'still connecting'}); see Settings > About.")


def main():
    # Secrets live in .env next to this file (git-ignored): Twilio details for
    # WhatsApp alerts and the Cloudflare tunnel token. Read it first so the
    # options below can default to its values.
    load_dotenv(Path(__file__).resolve().parent / ".env")

    p = argparse.ArgumentParser(description="HomeShield dashboard")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--db", default="homeshield.db")
    p.add_argument("--snapshots", default="snapshots")
    p.add_argument("--person-photos", default="person_photos")
    p.add_argument("--intruder-photos", default="intruder_photos")
    p.add_argument("--clips", default="clips", help="Folder for incident video clips")
    p.add_argument("--no-autostart", action="store_true",
                   help="Don't auto-start cameras on boot (useful for debugging)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--tunnel", action="store_true",
                   help="Also open remote access through a Cloudflare Quick Tunnel "
                        "(random https://*.trycloudflare.com address, no account)")
    p.add_argument("--tunnel-token", metavar="TOKEN",
                   default=os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", ""),
                   help="Run a named Cloudflare Tunnel with this token instead "
                        "(or set CLOUDFLARE_TUNNEL_TOKEN, e.g. in .env)")
    p.add_argument("--public-url", metavar="URL",
                   default=os.environ.get("HOMESHIELD_PUBLIC_URL", ""),
                   help="The named tunnel's public address, shown to admins "
                        "(or set HOMESHIELD_PUBLIC_URL)")
    p.add_argument("--mqtt-port", type=int, default=1884,
                   help="Port of HomeShield's MQTT broker for ESP32 nodes (default 1884)")
    p.add_argument("--no-mqtt", action="store_true",
                   help="Don't start the MQTT broker (no Devices or ESP32 nodes)")
    p.add_argument("--reset-password", metavar="USERNAME",
                   help="Give USERNAME a temporary password and exit "
                        "(for when the only admin has forgotten theirs)")
    args = p.parse_args()

    if args.reset_password:
        # Console recovery needs access to this computer, so it is the safe
        # way back in when no admin can sign in. It doesn't start the server.
        from homeshield.auth import UserStore
        from homeshield.db import init_db
        init_db(args.db)
        temp = UserStore(args.db).set_temporary_password(args.reset_password)
        if temp is None:
            print(f"No user called '{args.reset_password}' in {Path(args.db).resolve()}")
            raise SystemExit(1)
        print(f"Temporary password for {args.reset_password}: {temp}")
        print("Sign in with it on the HomeShield page; you'll then choose a new password.")
        return

    # Imported only now, so --reset-password above works (and answers at
    # once) even when the camera and model libraries can't load.
    from homeshield.alarm import serial_available
    from homeshield.server import create_app, lan_ip

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Per-request access logs and third-party chatter stay quiet unless --debug.
    for noisy in ("werkzeug", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.debug else logging.WARNING)


    app = create_app(
        db_path=args.db,
        snapshot_dir=args.snapshots,
        person_photos_dir=args.person_photos,
        intruder_photos_dir=args.intruder_photos,
        clips_dir=args.clips,
        auto_start=not args.no_autostart,
        iot=False if args.no_mqtt else None,
        mqtt_port=args.mqtt_port,
    )
    app.config["HOMESHIELD_PORT"] = args.port      # for the Wi-Fi buzzer's SERVER_URL

    print(f"\n[HomeShield] This computer : http://localhost:{args.port}/")
    ip = lan_ip()
    if args.host in ("0.0.0.0", "::"):
        if ip:
            print(f"[HomeShield] Other devices : http://{ip}:{args.port}/")
            print("[HomeShield]   ^ open THIS url on your phone/laptop "
                  "(NOT 0.0.0.0).")
        else:
            print("[HomeShield] Other devices : could not detect LAN IP; "
                  "run `ipconfig` and use this PC's IPv4 address.")
        print("[HomeShield]   If it still won't load, allow Python through "
              "the Windows Firewall (see notes).")
    else:
        print(f"[HomeShield] Bound to {args.host} only "
              "(other devices cannot reach it).")
    print(f"[HomeShield] Database : {Path(args.db).resolve()}")
    print(f"[HomeShield] Snapshots: {Path(args.snapshots).resolve()}")
    print(f"[HomeShield] Clips    : {Path(args.clips).resolve()}")
    if not app.extensions["homeshield_alarm"].enabled():
        print("[HomeShield] Alarm buzzer: off (Settings > Alarm buzzer)\n")
    else:
        usb = ("looking for the ESP32 on USB (CP2102 / CH340)" if serial_available()
               else "USB unavailable until you run: pip install pyserial")
        print(f"[HomeShield] Alarm buzzer: {usb}")
        if args.host in ("0.0.0.0", "::") and ip:
            print(f"[HomeShield]   Wi-Fi boards use SERVER_URL http://{ip}:{args.port}\n")
        else:
            print()

    broker = app.extensions["homeshield_iot"].broker
    if broker is None:
        print("[HomeShield] ESP32 nodes: off (started with --no-mqtt or HOMESHIELD_IOT=0)\n")
    elif broker.running:
        print(f"[HomeShield] ESP32 nodes: MQTT broker on port {broker.port}")
        if args.host in ("0.0.0.0", "::") and ip:
            print(f"[HomeShield]   Boards connect to {ip}. "
                  "Allow mosquitto.exe if Windows Firewall asks.\n")
        else:
            print()
    else:
        why = {"not_installed": "Mosquitto is not installed. Install it with\n"
                                "[HomeShield]   winget install EclipseFoundation.Mosquitto",
               "port_in_use": f"port {broker.port} is used by another program; try --mqtt-port",
               "no_key": "there is no device key yet (ESP32 nodes > Make a new key), then restart"}
        print(f"[HomeShield] ESP32 nodes: broker not running: "
              f"{why.get(broker.error, broker.error)}\n")

    if args.tunnel or args.tunnel_token:
        start_tunnel(app, args)
        print()

    app.run(host=args.host, port=args.port, threaded=True,
            debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
