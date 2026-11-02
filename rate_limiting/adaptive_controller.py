import random, time
def retry_delay(attempt, retry_after=None, base=1.0, maximum=20.0):
    if retry_after is not None:
        try: return min(maximum,max(0,float(retry_after)))
        except (TypeError,ValueError): pass
    return min(maximum,base*(2**attempt))+random.uniform(0,0.25)
