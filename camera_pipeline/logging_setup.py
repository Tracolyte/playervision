import logging
import sys


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        fmt="%(asctime)sZ level=%(levelname)s logger=%(name)s msg=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    h.setFormatter(fmt)
    root.addHandler(h)
