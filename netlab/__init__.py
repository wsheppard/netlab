import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)

class CustomFormatter(logging.Formatter):
    red = "\033[91m"
    yellow = "\033[93m"  # Added yellow color
    reset = "\033[0m"
    fmt = "%(levelname)-8s - %(asctime)s - %(name)s - %(message)s"

    FORMATS = {
        logging.ERROR: red + fmt + reset,
        logging.INFO: fmt,
        logging.DEBUG: fmt,
        logging.WARNING: yellow + fmt + reset,  # Now warnings are in yellow
        logging.CRITICAL: red + fmt + reset,
    }

    def format(self, record):
        if record is None:
            record = "[None]"
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        formatted_message = formatter.format(record)

        # Check for multi-line messages and format each line
        if record.message.count('\n') > 0:
            header, _ = formatted_message.split(record.message)
            message_lines = record.message.split('\n')
            formatted_message = header + ('\n' + ' ' * len(header)).join(message_lines)

        return formatted_message


# Now replace the handlers' formatters on the root logger
for handler in logging.root.handlers:
    handler.setFormatter(CustomFormatter())

