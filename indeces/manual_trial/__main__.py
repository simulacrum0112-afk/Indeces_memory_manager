"""Default-off manual entry point."""
def main(argv=None):
    from .manual_live import main as run
    return run(argv)

if __name__ == "__main__":
    raise SystemExit(main())
