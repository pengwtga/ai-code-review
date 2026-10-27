class Conn:
    def acquire(self):
        pass

    def release(self):
        pass


def process(conn, order):
    conn.acquire()
    order.validate()
    conn.release()
