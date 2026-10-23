import sys

try:
    from .cli import main
except ValueError as error:
    print(f"nzfleet: {error}", file=sys.stderr)
    sys.exit(2)

sys.exit(main(sys.argv[1:]))
