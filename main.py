from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()

    import os
    import sys

    # GUI-subsystem executables have no attached console on Windows.
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - must stay open for the process lifetime

    from aether.desktop import main

    main()
