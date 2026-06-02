"""
Date and time extraction utilities for parsing natural language dates from user input.
"""

import re
import dateparser
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple
import logging
from constants import TIME_MAPPINGS, DATE_PARSING_MAX_ATTEMPTS, DATE_PARSING_FUTURE_YEAR_LIMIT

try:
    import pytz
    _PYTZ_AVAILABLE = True
except ImportError:
    _PYTZ_AVAILABLE = False

logger = logging.getLogger(__name__)

class DateExtractor:
    """Utility class for extracting dates from natural language text."""

    def __init__(self):
        """Initialize the DateExtractor with common patterns."""
        # Use centralized time mappings
        self.time_mappings = TIME_MAPPINGS

        # Specific deadline extraction patterns
        self.deadline_patterns = [
            r"(?:deadline|due)\s+(?:is\s+)?(.+?)(?:\s+with\s|\s+and\s|$)",
            r"by\s+(.+?)(?:\s+with\s|\s+and\s|$)",
            r"before\s+(.+?)(?:\s+with\s|\s+and\s|$)",
            r"until\s+(.+?)(?:\s+with\s|\s+and\s|$)",
        ]

    def extract_date_from_text(self, text: str, timezone: Optional[str] = None) -> Tuple[Optional[str], str]:
        """
        Extract date/time from text and return cleaned text.

        Args:
            text: Input text that may contain date/time information
            timezone: Optional IANA timezone name. When provided, relative dates are
                interpreted in that timezone and the result is returned as UTC.

        Returns:
            Tuple of (extracted_datetime_string, cleaned_text)
            extracted_datetime_string is in "YYYY-mm-dd HH:mm:ss" format (UTC) or None
        """
        if not text:
            return None, text

        logger.debug(f"Extracting date from text: {text}")

        # First try specific deadline patterns
        for pattern in self.deadline_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_text = match.group(1).strip()
                logger.debug(f"Found deadline pattern: {date_text}")

                # Parse the extracted date text
                parsed_date = self._parse_date_with_fallbacks(date_text, timezone=timezone)
                if parsed_date:
                    # Remove the deadline phrase from the original text
                    cleaned_text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
                    cleaned_text = re.sub(r'\s+', ' ', cleaned_text)
                    return parsed_date, cleaned_text

        # Try to find dates within the text using broader patterns
        # Look for "next [day]", "tomorrow", specific dates, etc.
        date_patterns = [
            r"next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|midday|noon|midnight|morning|afternoon|evening|night)|\s+\d{1,2}\s*(?:am|pm))?",
            r"tomorrow(?:\s+(?:at\s+)?(?:morning|afternoon|evening|night|midday|noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}am|\d{1,2}pm))?",
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\s+\d{1,2}\s*(?:am|pm))?",
            r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?",
            r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+\d{4})?(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?",
            r"(?:in\s+)?\d{1,2}\s+(?:days?|weeks?|months?)",
            r"after\s+\d{1,2}\s+(?:days?|weeks?|months?)",
            r"\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?)?",
            r"(?:for\s+)?(?:next\s+)?\w+day\s+\d{1,2}(?:am|pm)",
        ]

        for pattern in date_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                date_candidate = match.group(0)
                parsed_date = self._parse_date_with_fallbacks(date_candidate, timezone=timezone)
                if parsed_date:
                    logger.debug(f"Found date candidate: {date_candidate} -> {parsed_date}")
                    # Remove the date from the text
                    cleaned_text = re.sub(re.escape(date_candidate), "", text, flags=re.IGNORECASE).strip()
                    cleaned_text = re.sub(r'\s+', ' ', cleaned_text)
                    return parsed_date, cleaned_text

        logger.debug("No date found in text")
        return None, text

    def _parse_date_with_fallbacks(self, date_text: str, timezone: Optional[str] = None) -> Optional[str]:
        """
        Parse a date string using multiple fallback approaches.

        Args:
            date_text: Text that potentially contains a date/time
            timezone: Optional IANA timezone name. When provided, relative dates are
                interpreted in that timezone and the result is returned as UTC.

        Returns:
            Formatted datetime string in "YYYY-mm-dd HH:mm:ss" format or None
        """
        if not date_text.strip():
            return None

        # Use centralized configuration constants
        max_parsing_attempts = DATE_PARSING_MAX_ATTEMPTS
        future_year_limit = DATE_PARSING_FUTURE_YEAR_LIMIT

        # Try manual parsing for common patterns first
        manual_result = self._manual_date_parse(date_text, timezone=timezone)
        if manual_result:
            return manual_result

        # Preprocess the text
        processed_text = self._preprocess_date_text(date_text)
        logger.debug(f"Preprocessed: '{date_text}' -> '{processed_text}'")

        # Base dateparser settings — apply timezone if provided so that relative
        # expressions are resolved in the user's locale and the result is UTC.
        base_settings: dict = {'PREFER_DATES_FROM': 'future', 'RETURN_AS_TIMEZONE_AWARE': False}
        if timezone:
            base_settings['TIMEZONE'] = timezone
            base_settings['TO_TIMEZONE'] = 'UTC'

        # Try multiple parsing approaches
        for attempt in range(max_parsing_attempts):
            try:
                if attempt == 0:
                    # First attempt: strict parsing with future preference
                    parsed_dt = dateparser.parse(
                        processed_text,
                        settings={**base_settings, 'STRICT_PARSING': True},
                    )
                elif attempt == 1:
                    # Second attempt: more flexible parsing
                    parsed_dt = dateparser.parse(
                        processed_text,
                        settings={**base_settings, 'STRICT_PARSING': False},
                    )
                else:
                    # Third attempt: try with current date as base
                    settings = {**base_settings, 'STRICT_PARSING': False}
                    settings.pop('PREFER_DATES_FROM', None)
                    parsed_dt = dateparser.parse(processed_text, settings=settings)

                if parsed_dt:
                    # Validate the parsed date makes sense
                    now = datetime.now()

                    # If the date is more than future_year_limit years in the future, it's probably wrong
                    if parsed_dt.year > now.year + future_year_limit:
                        logger.debug(f"Date too far in future: {parsed_dt}")
                        continue

                    # If no time was specified and we got midnight, set reasonable default
                    if (parsed_dt.hour == 0 and parsed_dt.minute == 0 and
                            'midnight' not in processed_text.lower() and
                            not re.search(r'\d{1,2}:\d{2}|00:00', date_text)):
                        parsed_dt = parsed_dt.replace(hour=9, minute=0, second=0)

                    result = parsed_dt.strftime("%Y-%m-%d %H:%M:%S")
                    logger.debug(f"Successfully parsed '{date_text}' -> '{result}' (attempt {attempt + 1})")
                    return result

            except Exception as e:
                logger.debug(f"Parsing attempt {attempt + 1} failed for '{date_text}': {e}")
                continue

        logger.warning(f"All parsing attempts failed for '{date_text}'")
        return None

    def _localize_and_convert_to_utc(self, naive_dt: datetime, timezone: str) -> datetime:
        """Convert a naive datetime (in user's timezone) to a naive UTC datetime."""
        if _PYTZ_AVAILABLE:
            tz = pytz.timezone(timezone)
            local_dt = tz.localize(naive_dt)
            return local_dt.astimezone(pytz.utc).replace(tzinfo=None)
        return naive_dt

    def _manual_date_parse(self, date_text: str, timezone: Optional[str] = None) -> Optional[str]:
        """
        Manually parse common date patterns that dateparser struggles with.

        Args:
            date_text: Raw date text
            timezone: Optional IANA timezone name (e.g. "Asia/Manila"). When provided,
                relative dates are computed in this timezone and the result is converted to UTC.

        Returns:
            Formatted datetime string or None
        """
        text = date_text.lower().strip()

        # Compute "now" in the user's timezone so relative dates (tomorrow, next Monday)
        # are resolved correctly for that locale.
        if timezone and _PYTZ_AVAILABLE:
            try:
                tz = pytz.timezone(timezone)
                now = datetime.now(tz).replace(tzinfo=None)
            except Exception:
                now = datetime.now()
        else:
            now = datetime.now()

        # Handle "next [weekday]" patterns
        next_weekday_match = re.search(
            r'next\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?|\s+(\d{1,2})\s*(am|pm)|\s+at\s+(midday|noon|midnight|morning|afternoon|evening|night))?',
            text)
        if next_weekday_match:
            weekday_name = next_weekday_match.group(1)
            hour_part = next_weekday_match.group(2) or next_weekday_match.group(5)
            minute_part = next_weekday_match.group(3) or "0"
            am_pm = next_weekday_match.group(4) or next_weekday_match.group(6)

            weekdays = {'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3, 'friday': 4, 'saturday': 5,
                        'sunday': 6}
            target_weekday = weekdays[weekday_name]

            # Calculate next occurrence of this weekday
            days_ahead = target_weekday - now.weekday()
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7

            target_date = now + timedelta(days=days_ahead)

            # Parse time if provided
            if hour_part:
                hour = int(hour_part)
                minute = int(minute_part) if minute_part else 0

                if am_pm:
                    if am_pm.lower() == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm.lower() == 'am' and hour == 12:
                        hour = 0

                target_date = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            elif am_pm and am_pm.lower() in ['midday', 'noon']:
                target_date = target_date.replace(hour=12, minute=0, second=0, microsecond=0)
            elif am_pm and am_pm.lower() == 'midnight':
                target_date = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            elif am_pm and am_pm.lower() in self.time_mappings:
                # Convert named time to hour
                time_str = self.time_mappings[am_pm.lower()].replace(' ', '')
                try:
                    time_obj = datetime.strptime(time_str, "%I:%M%p").time()
                    target_date = target_date.replace(hour=time_obj.hour, minute=time_obj.minute, second=0,
                                                      microsecond=0)
                except Exception:
                    target_date = target_date.replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                target_date = target_date.replace(hour=9, minute=0, second=0, microsecond=0)

            if timezone:
                target_date = self._localize_and_convert_to_utc(target_date, timezone)
            result = target_date.strftime("%Y-%m-%d %H:%M:%S")
            logger.debug(f"Manual parse: '{date_text}' -> '{result}'")
            return result

        # Handle "tomorrow" patterns
        tomorrow_match = re.search(r'tomorrow(?:\s+(morning|afternoon|evening|night|\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?',
                                   text)
        if tomorrow_match:
            target_date = now + timedelta(days=1)
            time_part = tomorrow_match.group(1) if tomorrow_match.group(1) else None

            if time_part:
                if time_part in self.time_mappings:
                    # Convert to standard time format
                    time_str = self.time_mappings[time_part].replace(' ', '')  # Remove space from "12:00 PM"
                    try:
                        time_obj = datetime.strptime(time_str, "%I:%M%p").time()
                        target_date = target_date.replace(hour=time_obj.hour, minute=time_obj.minute, second=0,
                                                          microsecond=0)
                    except Exception:
                        target_date = target_date.replace(hour=9, minute=0, second=0, microsecond=0)
                else:
                    # Try to parse time directly
                    time_match = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', time_part)
                    if time_match:
                        hour = int(time_match.group(1))
                        minute = int(time_match.group(2)) if time_match.group(2) else 0
                        am_pm = time_match.group(3)

                        if am_pm:
                            if am_pm.lower() == 'pm' and hour != 12:
                                hour += 12
                            elif am_pm.lower() == 'am' and hour == 12:
                                hour = 0

                        target_date = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    else:
                        target_date = target_date.replace(hour=9, minute=0, second=0, microsecond=0)
            else:
                target_date = target_date.replace(hour=9, minute=0, second=0, microsecond=0)

            if timezone:
                target_date = self._localize_and_convert_to_utc(target_date, timezone)
            result = target_date.strftime("%Y-%m-%d %H:%M:%S")
            logger.debug(f"Manual parse: '{date_text}' -> '{result}'")
            return result

        return None

    def _preprocess_date_text(self, date_text: str) -> str:
        """
        Preprocess date text to handle common expressions.

        Args:
            date_text: Raw date text

        Returns:
            Preprocessed date text
        """
        text = date_text.strip()

        # Handle common time expressions
        for expression, replacement in self.time_mappings.items():
            text = re.sub(r'\b' + expression + r'\b', replacement, text, flags=re.IGNORECASE)

        # Normalize time formats
        text = re.sub(r'(\d{1,2})\s*(pm|am)', r'\1 \2', text, flags=re.IGNORECASE)
        text = re.sub(r'\bat\s+(\d{1,2})\s*(pm|am)', r'at \1 \2', text, flags=re.IGNORECASE)

        # Handle "3PM" without space
        text = re.sub(r'(\d{1,2})(pm|am)', r'\1 \2', text, flags=re.IGNORECASE)

        return text
