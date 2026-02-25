import logging
import sys


def setup_logging():
    # Clear any existing handlers
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Define a custom formatter class that handles the padding internally
    class CustomFormatter(logging.Formatter):
        def formatMessage(self, record):
            # Get the name and line number
            name_lineno = f"{record.name}:{record.lineno}"

            # Right-pad or truncate to exactly 32 characters
            if len(name_lineno) > 32:
                name_lineno = name_lineno[:29] + "..."
            else:
                name_lineno = name_lineno.ljust(32)

            # Set record attributes for formatting
            record.__dict__["name_lineno_padded"] = name_lineno

            # Return the formatted string
            return super().formatMessage(record)

    # Create formatter with the custom class
    formatter = CustomFormatter(
        fmt="%(levelname).1s | %(name_lineno_padded)s | %(message)s"
    )

    # Set up the handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Add the handler to the root logger
    root.setLevel(logging.INFO)
    root.addHandler(console_handler)

    # Set level for specific loggers
    logging.getLogger("anthropic").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    logging.getLogger("blib2to3").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
