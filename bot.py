"""Repository entrypoint kept stable for systemd and Windows launchers."""

from whale.bot import main


if __name__ == "__main__":
    raise SystemExit(main())
