"""Production exception diagnostics without request data or exception values."""
import logging
import traceback
from pathlib import Path


class SafeExceptionFormatter(logging.Formatter):
    def format(self, record):
        # Django's message, exception text, request, and locals may contain secrets.
        lines = ["ERROR django.request: unhandled request exception"]
        if record.exc_info and record.exc_info[0]:
            lines.append(f"Exception type: {record.exc_info[0].__name__}")
            for frame in traceback.extract_tb(record.exc_info[2]):
                lines.append(f"  {Path(frame.filename).name}:{frame.lineno} in {frame.name}")
        return "\n".join(lines)
