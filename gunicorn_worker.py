from gunicorn.workers.gthread import ThreadWorker


class StreamingThreadWorker(ThreadWorker):
    def handle(self, conn):
        result = super().handle(conn)
        if result is False:
            # Gunicorn 26 otherwise drains slow-closing clients on the accept loop.
            conn.close(graceful=True)
        return result
