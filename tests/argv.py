"""Readers for the docker argv a job builds, so tests assert one property instead of the whole list."""


def option(argv, flag):
    """The value of the first `flag`, given as `flag value` or `flag=value`; the test fails by name when the argv never sets it."""
    for index, word in enumerate(argv):
        if word == flag and index + 1 < len(argv):
            return argv[index + 1]
        if word.startswith(flag + "="):
            return word[len(flag) + 1:]
    raise AssertionError(f"docker argv sets no {flag}")


def options(argv, flag):
    """Every value given to a repeated flag."""
    return [value for name, value in zip(argv, argv[1:]) if name == flag]


def environment(argv):
    """The -e settings as a dictionary."""
    return dict(item.split("=", 1) for item in options(argv, "-e") if "=" in item)


def shell_script(argv):
    """The shell script `sh -c` runs inside the container."""
    return option(argv, "-c")
