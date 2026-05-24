from logging.handlers import SocketHandler


class FluentBitHandler(SocketHandler):
    def emit(self, record) -> None:
        try:
            msg = self.format(record)
            # иногда FluentBit/Fluentd любят \n в конце
            if not msg.endswith("\n"):
                msg = msg + "\n"
            self.send(msg.encode("utf-8"))
        except Exception:
            self.handleError(record)
