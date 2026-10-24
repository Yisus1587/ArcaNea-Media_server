import time
import threading
import queue
import logging
from src.watcher.service import WatchService
from src.scanner.scanner import Scanner

logger = logging.getLogger(__name__)


def run_forever():
    q = queue.Queue()
    watcher = WatchService(out_queue=q)
    scanner = Scanner()

    watcher.start()

    def worker():
        while True:
            try:
                path = q.get()
                if path is None:
                    break
                scanner.scan_path(path)
            except Exception:
                logger.exception('Worker loop crashed')

    t = threading.Thread(target=worker, daemon=True, name='scanner-worker')
    t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        watcher.stop()
        q.put(None)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_forever()
