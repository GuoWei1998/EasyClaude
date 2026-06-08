import random
import time

from anthropic import APIError

from .compact import compact_history


MAX_RECOVERY_ATTEMPTS = 3
BACKOFF_BASE_DELAY = 1.0
BACKOFF_MAX_DELAY = 30.0
CONTINUATION_MESSAGE = (
    "Output limit hit. Continue directly from where you stopped -- "
    "no recap, no repetition. Pick up mid-sentence if needed."
)


def backoff_delay(attempt: int) -> float:
    delay = min(BACKOFF_BASE_DELAY * (2 ** attempt), BACKOFF_MAX_DELAY)
    return delay + random.uniform(0, 1)


def is_prompt_too_long_error(error: Exception) -> bool:
    text = str(error).lower()
    return "overlong_prompt" in text or ("prompt" in text and "long" in text)


def is_transient_error(error: Exception) -> bool:
    text = str(error).lower()
    transient_markers = (
        "rate",
        "timeout",
        "temporarily",
        "connection",
        "network",
        "overloaded",
        "503",
        "529",
    )
    return isinstance(error, (ConnectionError, TimeoutError, OSError)) or any(
        marker in text for marker in transient_markers
    )


def call_with_recovery(create_response, messages: list, compact_state) -> object | None:
    """
    Call the model with recovery for prompt-too-long and transient transport errors.

    create_response is a zero-argument callable so the caller can close over
    model/system/tools without this module knowing those details.
    """
    for attempt in range(MAX_RECOVERY_ATTEMPTS + 1):
        try:
            return create_response()
        except APIError as error:
            if is_prompt_too_long_error(error):
                print(f"[Recovery] Prompt too long. Compacting... (attempt {attempt + 1})")
                try:
                    messages[:] = compact_history(messages, compact_state)
                except Exception as compact_error:
                    print(f"[Error] Compact failed during recovery: {compact_error}")
                    return None
                continue
            if attempt < MAX_RECOVERY_ATTEMPTS and is_transient_error(error):
                delay = backoff_delay(attempt)
                print(
                    f"[Recovery] API error: {error}. "
                    f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RECOVERY_ATTEMPTS})"
                )
                time.sleep(delay)
                continue
            print(f"[Error] API call failed after {attempt + 1} attempts: {error}")
            return None
        except (ConnectionError, TimeoutError, OSError) as error:
            if attempt < MAX_RECOVERY_ATTEMPTS:
                delay = backoff_delay(attempt)
                print(
                    f"[Recovery] Connection error: {error}. "
                    f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{MAX_RECOVERY_ATTEMPTS})"
                )
                time.sleep(delay)
                continue
            print(f"[Error] Connection failed after {MAX_RECOVERY_ATTEMPTS} retries: {error}")
            return None
    print("[Error] No response received.")
    return None
