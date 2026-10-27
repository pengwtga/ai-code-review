class Conn:
    def acquire(self):
        pass

    def release(self):
        pass


def process(conn, order):
    conn.acquire()
    try:
        if not order.valid:
            return
        order.validate()
    finally:
        conn.release()
