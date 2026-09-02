from django import forms
from django.forms.utils import to_current_timezone
from django.utils.dateparse import parse_datetime


class MidnightDateTimeInput(forms.MultiWidget):
    """Keep the date blank while giving its independent time picker midnight."""

    def __init__(self):
        super().__init__([
            forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "aria-label": "Date"}),
            forms.TimeInput(format="%H:%M", attrs={"type": "time", "aria-label": "Time"}),
        ])

    def decompress(self, value):
        if isinstance(value, str):
            # A failed submission must retain the entered date/time for correction.
            if "T" in value:
                return value.split("T", 1)
            try:
                value = parse_datetime(value)
            except ValueError:
                return [value, ""]
        if value:
            value = to_current_timezone(value)
            return [value.date(), value.time()]
        return [None, "00:00"]

    def value_from_datadict(self, data, files, name):
        # Preserve existing API/form callers that submit a combined timestamp.
        if name in data:
            return data.get(name)
        date, time = super().value_from_datadict(data, files, name)
        if not date:
            return ""
        return f"{date}T{time or '00:00'}"
