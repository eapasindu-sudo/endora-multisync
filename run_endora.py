import multiprocessing
import os
import sys
import traceback


def selftest(out: str) -> None:
    """Build check: start the real window once and write 'ok <version>' or the error to a file."""
    try:
        from PySide6.QtWidgets import QApplication
        from endora import app
        qa = QApplication(sys.argv[:1])
        app.MainWindow()
        qa.processEvents()
        result = f"ok {app.VERSION}"
    except BaseException:  # noqa: BLE001 - the whole point is to report it
        result = traceback.format_exc()
    with open(out, "w", encoding="utf-8") as f:
        f.write(result)
    os._exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--selftest" in sys.argv:
        selftest(sys.argv[sys.argv.index("--selftest") + 1])
    from endora.app import main
    main()
