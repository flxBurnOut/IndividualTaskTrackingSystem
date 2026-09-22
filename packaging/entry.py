"""The frozen entrypoint intentionally uses an absolute package import."""
from management.__main__ import main
if __name__ == '__main__':
    raise SystemExit(main())
