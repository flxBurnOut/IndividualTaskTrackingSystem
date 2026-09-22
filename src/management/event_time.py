"""Deterministic local event times: never silently choose a DST fold."""
import datetime as dt


def local_datetime(day, minutes, zone):
    naive = dt.datetime.combine(day, dt.time(minutes // 60, minutes % 60))
    local = naive.replace(tzinfo=zone)
    if local.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError('nonexistent local time')
    if local.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
        raise ValueError('ambiguous local time')
    return local
