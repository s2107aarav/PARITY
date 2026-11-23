"""HTTP field and JSON value handling shared by the relay and adapter worker."""

import math

HOP_HEADERS = {
    "host",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}
DECODED_BODY_HEADERS = HOP_HEADERS | {"content-length", "content-encoding"}


def connection_tokens(headers):
    return {value.strip().lower() for value in headers.get("connection", "").split(",")}


def media_type(value):
    return value.split(";", 1)[0].strip().lower()


def is_json_media_type(value):
    media = media_type(value)
    return media == "application/json" or media.endswith("+json")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is out of range")
    return number
